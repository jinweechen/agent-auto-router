from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import pathlib
import shutil
import subprocess
import tempfile
import time
from typing import Any

from auto_router import route_case
from orchestration_engine import (
    CallRecord,
    DEFAULT_CASES,
    RunContext,
    run_variant,
)


ROOT = pathlib.Path(__file__).resolve().parent


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("value must be at least 1")
    return parsed


def extract_usage(events: list[dict[str, Any]]) -> tuple[int, int]:
    input_tokens = 0
    output_tokens = 0
    for event in events:
        usage = event.get("usage")
        if not isinstance(usage, dict):
            continue
        input_tokens = max(input_tokens, int(usage.get("input_tokens", 0)))
        output_tokens = max(output_tokens, int(usage.get("output_tokens", 0)))
    return input_tokens, output_tokens


def extract_thread_id(events: list[dict[str, Any]]) -> str:
    for event in events:
        if event.get("type") == "thread.started":
            return str(event.get("thread_id", ""))
    return ""


class CodexCliClient:
    def __init__(
        self,
        timeout_seconds: int = 600,
        effort_override: str | None = None,
        role_efforts: dict[str, str] | None = None,
        workdir: pathlib.Path = ROOT,
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self.effort_override = effort_override
        self.role_efforts = role_efforts or {}
        self.workdir = workdir.resolve()
        self.codex_command = self._resolve_codex_command()

    @staticmethod
    def _resolve_codex_command() -> list[str]:
        checked: set[str] = set()
        for name in ("codex", "codex.exe", "codex.cmd", "codex.bat", "codex.ps1"):
            executable = shutil.which(name)
            if not executable or executable.lower() in checked:
                continue
            checked.add(executable.lower())
            suffix = pathlib.Path(executable).suffix.lower()
            if suffix == ".ps1":
                powershell = shutil.which("pwsh") or shutil.which("pwsh.exe")
                powershell = powershell or shutil.which("powershell.exe")
                if not powershell:
                    continue
                return [
                    powershell,
                    "-NoProfile",
                    "-NonInteractive",
                    "-File",
                    executable,
                ]
            if suffix in {".cmd", ".bat"}:
                command_shell = shutil.which("cmd.exe") or os.environ.get("COMSPEC")
                if not command_shell:
                    continue
                return [command_shell, "/d", "/c", executable]
            return [executable]
        raise RuntimeError("Codex CLI executable or wrapper was not found on PATH")
    def create(
        self,
        *,
        context: RunContext,
        role: str,
        model: str,
        effort: str,
        instructions: str,
        input_text: str,
        max_output_tokens: int = 4000,
    ) -> tuple[str, dict[str, Any]]:
        role_key = "worker" if role.startswith("worker:") else role
        effective_effort = (
            self.effort_override
            or self.role_efforts.get(role_key)
            or ("high" if effort == "max" else effort)
        )
        prompt = (
            "You are a bounded evaluation worker. Do not call tools, inspect files, or modify "
            "anything. Respond directly using only the supplied task.\n\n"
            f"Keep the response within {max_output_tokens} tokens.\n\n"
            f"INSTRUCTIONS:\n{instructions}\n\nINPUT:\n{input_text}"
        )

        with tempfile.TemporaryDirectory(prefix="codex-cli-eval-") as temp_dir:
            output_path = pathlib.Path(temp_dir) / "last-message.txt"
            command = [
                *self.codex_command,
                "exec",
                "--ephemeral",
                "--ignore-user-config",
                "--ignore-rules",
                "--skip-git-repo-check",
                "--sandbox",
                "read-only",
                "--color",
                "never",
                "--model",
                model,
                "--config",
                f'model_reasoning_effort="{effective_effort}"',
                "--json",
                "--output-last-message",
                str(output_path),
                "--cd",
                str(self.workdir),
                "-",
            ]

            started = time.perf_counter()
            completed = subprocess.run(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                input=prompt,
                timeout=self.timeout_seconds,
                check=False,
            )
            latency = time.perf_counter() - started

            events: list[dict[str, Any]] = []
            for line in completed.stdout.splitlines():
                try:
                    value = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(value, dict):
                    events.append(value)

            if completed.returncode != 0:
                stderr_tail = completed.stderr[-2000:].strip()
                stdout_tail = completed.stdout[-2000:].strip()
                raise RuntimeError(
                    f"Codex CLI failed for role={role}, model={model}, "
                    f"exit={completed.returncode}\nSTDERR:\n{stderr_tail}\nSTDOUT:\n{stdout_tail}"
                )
            if not output_path.exists():
                raise RuntimeError(f"Codex CLI produced no final message for role={role}")

            output_text = output_path.read_text(encoding="utf-8").strip()
            if not output_text:
                raise RuntimeError(f"Codex CLI returned an empty message for role={role}")

            input_tokens, output_tokens = extract_usage(events)
            thread_id = extract_thread_id(events)
            context.records.append(
                CallRecord(
                    role=role,
                    model=model,
                    effort=effective_effort,
                    latency_seconds=latency,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    estimated_cost_usd=None,
                    response_id=thread_id,
                )
            )
            return output_text, {"events": events, "thread_id": thread_id}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Evaluate Sol/Terra/Luna through the signed-in Codex CLI"
    )
    parser.add_argument("--cases", type=pathlib.Path, default=DEFAULT_CASES)
    parser.add_argument("--variants", default="B,C")
    parser.add_argument("--limit", type=int, default=1)
    parser.add_argument("--max-workers", type=positive_int, default=2)
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--workdir", type=pathlib.Path, default=pathlib.Path.cwd())
    parser.add_argument("--results-dir", type=pathlib.Path, default=None)
    parser.add_argument(
        "--effort-override",
        choices=("low", "medium", "high", "xhigh", "max"),
        default=None,
        help="Force every role to use one reasoning effort for controlled comparisons",
    )
    for role in ("planner", "dispatcher", "worker", "reviewer", "grader"):
        parser.add_argument(
            f"--{role}-effort",
            choices=("low", "medium", "high", "xhigh", "max"),
            default=None,
            help=f"Override reasoning effort for the {role} role",
        )
    parser.add_argument(
        "--routing-mode",
        choices=("off", "intelligence", "balance", "cost"),
        default="off",
        help="Select a deterministic Auto Lite routing policy",
    )
    parser.add_argument(
        "--shadow-auto",
        action="store_true",
        help="Record the Auto Lite recommendation without changing selected variants",
    )
    parser.add_argument(
        "--explain-route",
        action="store_true",
        help="Print the selected route and its feature-based reasons",
    )
    parser.add_argument(
        "--route-only",
        action="store_true",
        help="Write Auto Lite route decisions without launching Codex model calls",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    cases = json.loads(args.cases.read_text(encoding="utf-8"))
    if not isinstance(cases, list) or not cases:
        raise ValueError("Cases file must contain a non-empty JSON array")
    variants = [item.strip().upper() for item in args.variants.split(",") if item.strip()]
    routing_effort = args.effort_override or args.planner_effort or "medium"

    if args.route_only:
        route_mode = "balance" if args.routing_mode == "off" else args.routing_mode
        timestamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        route_results = []
        for case in cases[: args.limit]:
            decision = route_case(case, route_mode, routing_effort)
            route_results.append({"case_id": case["id"], "routing": decision})
            if args.explain_route:
                print(
                    f"case={case['id']} mode={route_mode} route={decision['route']} "
                    f"variant={decision['variant']} reasons="
                    + "; ".join(decision["reasons"]),
                    flush=True,
                )
        results_dir = args.results_dir or (args.workdir / "route-results")
        results_dir.mkdir(parents=True, exist_ok=True)
        output_path = results_dir / f"auto-route-report-{route_mode}-{timestamp}.json"
        output_path.write_text(
            json.dumps(
                {
                    "created_at": timestamp,
                    "router_version": "auto-lite-v1",
                    "mode": route_mode,
                    "model_calls": 0,
                    "results": route_results,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"Route report: {output_path}", flush=True)
        return 0

    role_efforts = {
        role: value
        for role in ("planner", "dispatcher", "worker", "reviewer", "grader")
        if (value := getattr(args, f"{role}_effort"))
    }
    client = CodexCliClient(
        timeout_seconds=args.timeout,
        effort_override=args.effort_override,
        role_efforts=role_efforts,
        workdir=args.workdir,
    )

    results_dir = args.results_dir or (args.workdir / "eval-results")
    results_dir.mkdir(parents=True, exist_ok=True)
    checkpoint_timestamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    checkpoint_path = results_dir / f"codex-cli-eval-{checkpoint_timestamp}.partial.json"

    def persist_checkpoint() -> None:
        checkpoint_path.write_text(
            json.dumps(
                {
                    "created_at": checkpoint_timestamp,
                    "backend": "signed-in-codex-cli",
                    "status": "partial",
                    "cost_note": "Codex CLI does not expose per-call billing; cost fields are null and cost mode is a model-tier proxy.",
                    "effort_override": args.effort_override,
                    "role_efforts": role_efforts,
                    "results": results,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    results: list[dict[str, Any]] = []
    for case in cases[: args.limit]:
        route_mode = (
            "balance" if args.shadow_auto and args.routing_mode == "off" else args.routing_mode
        )
        route_decision = (
            route_case(case, route_mode, routing_effort) if route_mode != "off" else None
        )
        case_variants = variants
        if route_decision and not args.shadow_auto:
            case_variants = [route_decision["variant"]]
        if route_decision and args.explain_route:
            print(
                f"Auto Lite mode={route_mode} route={route_decision['route']} "
                f"variant={route_decision['variant']} reasons="
                + "; ".join(route_decision["reasons"]),
                flush=True,
            )
        for variant in case_variants:
            print(f"Running case={case['id']} variant={variant} via Codex CLI...", flush=True)
            variant_started = time.perf_counter()
            try:
                result = run_variant(client, case, variant, args.max_workers)
                result["status"] = "completed"
                result["routing"] = route_decision
            except Exception as exc:
                result = {
                    "case_id": case["id"],
                    "variant": variant,
                    "status": "failed",
                    "routing": route_decision,
                    "grade": {
                        "score": 0,
                        "passed": False,
                        "unmet_criteria": case.get("acceptance_criteria", []),
                        "critical_errors": [str(exc)[:2000]],
                        "rationale": "The orchestration variant did not complete.",
                    },
                    "final_output": "",
                    "wall_seconds": getattr(exc, "orchestration_wall_seconds", time.perf_counter() - variant_started),
                    "summed_call_latency_seconds": 0.0,
                    "estimated_cost_usd": None,
                    "calls": getattr(exc, "orchestration_records", []),
                    "error": {
                        "type": getattr(exc, "orchestration_error_type", type(exc).__name__),
                        "message": str(exc)[:2000],
                    },
                }
                results.append(result)
                persist_checkpoint()
                print(
                    f"  failed={type(exc).__name__} "
                    f"wall={result['wall_seconds']:.1f}s",
                    flush=True,
                )
                continue
            results.append(result)
            persist_checkpoint()
            input_tokens = sum(call["input_tokens"] for call in result["calls"])
            output_tokens = sum(call["output_tokens"] for call in result["calls"])
            print(
                f"  score={result['grade'].get('score')} "
                f"passed={result['grade'].get('passed')} "
                f"wall={result['wall_seconds']:.1f}s "
                f"calls={len(result['calls'])} "
                f"tokens={input_tokens}+{output_tokens}",
                flush=True,
            )

    results_dir = (args.results_dir or (args.workdir / "eval-results")).resolve()
    results_dir.mkdir(parents=True, exist_ok=True)
    timestamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output_path = results_dir / f"codex-cli-eval-{timestamp}.json"
    output_path.write_text(
        json.dumps(
            {
                "created_at": timestamp,
                "backend": "signed-in-codex-cli",
                "cost_note": "Codex CLI does not expose per-call billing; cost fields are null and cost mode is a model-tier proxy.",
                "effort_override": args.effort_override,
                "role_efforts": role_efforts,
                "results": results,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"Results: {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
