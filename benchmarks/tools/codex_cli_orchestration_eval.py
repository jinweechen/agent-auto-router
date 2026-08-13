from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import sys
import time
from typing import Any

TOOLS_ROOT = pathlib.Path(__file__).resolve().parent
BENCHMARKS_ROOT = TOOLS_ROOT.parent
PROJECT_ROOT = BENCHMARKS_ROOT.parent
SKILL_SCRIPTS = PROJECT_ROOT / "skills" / "agent-auto-router" / "scripts"
sys.path.insert(0, str(SKILL_SCRIPTS))
sys.path.insert(0, str(TOOLS_ROOT))

from auto_router import route_case
from artifact_layout import (
    create_run_directory,
    default_evaluations_root,
    prepare_explicit_run_directory,
    write_manifest,
)
from cli_arguments import positive_int
from codex_cli_adapter import CodexCliAdapter
from orchestration_engine import run_variant


DEFAULT_CASES = BENCHMARKS_ROOT / "cases" / "eval_cases.json"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Evaluate deterministic routes through the signed-in Codex CLI"
    )
    parser.add_argument("--cases", type=pathlib.Path, default=DEFAULT_CASES)
    parser.add_argument("--variants", default="B,C")
    parser.add_argument("--limit", type=int, default=1)
    parser.add_argument("--max-workers", type=positive_int, default=2)
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--workdir", type=pathlib.Path, default=pathlib.Path.cwd())
    parser.add_argument("--results-dir", type=pathlib.Path, default=None)
    parser.add_argument(
        "--artifacts-root",
        type=pathlib.Path,
        default=None,
        help=(
            "Root for a generated per-run directory. Defaults to "
            "AGENT_AUTO_ROUTER_EVALUATIONS_DIR or the user-local state directory."
        ),
    )
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
        help="Select a deterministic routing policy",
    )
    parser.add_argument(
        "--orchestration-policy",
        choices=("direct", "recommend", "auto"),
        default="direct",
        help="Explicitly enable orchestration recommendation or Auto topology selection",
    )
    parser.add_argument(
        "--shadow-auto",
        action="store_true",
        help="Record the Auto recommendation without changing selected variants",
    )
    parser.add_argument(
        "--explain-route",
        action="store_true",
        help="Print the selected route and its feature-based reasons",
    )
    parser.add_argument(
        "--route-only",
        action="store_true",
        help="Write route decisions without launching Codex model calls",
    )
    return parser.parse_args()


def prepare_run(args: argparse.Namespace, kind: str, timestamp: str) -> tuple[str, pathlib.Path]:
    if args.results_dir is not None and args.artifacts_root is not None:
        raise ValueError("Use either --results-dir or --artifacts-root, not both")
    if args.results_dir is not None:
        return prepare_explicit_run_directory(args.results_dir)
    root = args.artifacts_root or default_evaluations_root()
    return create_run_directory(kind, root=root, timestamp=timestamp)


def manifest_payload(
    args: argparse.Namespace,
    *,
    run_id: str,
    created_at: str,
    kind: str,
    status: str,
    model_calls: int,
) -> dict[str, Any]:
    return {
        "runId": run_id,
        "kind": kind,
        "createdAt": created_at,
        "status": status,
        "modelCalls": model_calls,
        "cases": str(args.cases.expanduser().resolve()),
        "workdir": str(args.workdir.expanduser().resolve()),
        "routingMode": args.routing_mode,
        "orchestrationPolicy": args.orchestration_policy,
        "modelAffinity": "off",
        "routeOnly": args.route_only,
    }


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
        run_id, results_dir = prepare_run(args, "route-only", timestamp)
        write_manifest(
            results_dir,
            manifest_payload(
                args,
                run_id=run_id,
                created_at=timestamp,
                kind="route-only",
                status="running",
                model_calls=0,
            ),
        )
        route_results = []
        for case in cases[: args.limit]:
            decision = route_case(
                case,
                route_mode,
                routing_effort,
                orchestration_policy=args.orchestration_policy,
                model_affinity_mode="off",
            )
            route_results.append({"case_id": case["id"], "routing": decision})
            if args.explain_route:
                print(
                    f"case={case['id']} mode={route_mode} route={decision['route']} "
                    f"variant={decision['variant']} reasons="
                    + "; ".join(decision["reasons"]),
                    flush=True,
                )
        output_path = results_dir / "route-report.json"
        output_path.write_text(
            json.dumps(
                {
                    "created_at": timestamp,
                    "router_version": route_results[0]["routing"]["router_version"],
                    "mode": route_mode,
                    "orchestration_policy": args.orchestration_policy,
                    "model_affinity": "off",
                    "model_calls": 0,
                    "results": route_results,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        write_manifest(
            results_dir,
            manifest_payload(
                args,
                run_id=run_id,
                created_at=timestamp,
                kind="route-only",
                status="completed",
                model_calls=0,
            ),
        )
        print(f"Route report: {output_path}", flush=True)
        return 0

    role_efforts = {
        role: value
        for role in ("planner", "dispatcher", "worker", "reviewer", "grader")
        if (value := getattr(args, f"{role}_effort"))
    }
    client = CodexCliAdapter(
        timeout_seconds=args.timeout,
        effort_override=args.effort_override,
        role_efforts=role_efforts,
        workdir=args.workdir,
    )

    checkpoint_timestamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_id, results_dir = prepare_run(args, "codex-cli", checkpoint_timestamp)
    write_manifest(
        results_dir,
        manifest_payload(
            args,
            run_id=run_id,
            created_at=checkpoint_timestamp,
            kind="codex-cli",
            status="running",
            model_calls=0,
        ),
    )
    checkpoint_path = results_dir / "checkpoint.partial.json"

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
            route_case(
                case,
                route_mode,
                routing_effort,
                orchestration_policy=args.orchestration_policy,
                model_affinity_mode="off",
            )
            if route_mode != "off"
            else None
        )
        case_variants = variants
        if route_decision and not args.shadow_auto:
            case_variants = [route_decision["variant"]]
        if route_decision and args.explain_route:
            print(
                f"Auto mode={route_mode} route={route_decision['route']} "
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

    timestamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output_path = results_dir / "results.json"
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
    model_calls = sum(len(result.get("calls", [])) for result in results)
    write_manifest(
        results_dir,
        manifest_payload(
            args,
            run_id=run_id,
            created_at=checkpoint_timestamp,
            kind="codex-cli",
            status="completed",
            model_calls=model_calls,
        ),
    )
    print(f"Results: {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

