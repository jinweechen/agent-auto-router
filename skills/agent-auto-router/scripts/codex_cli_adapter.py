from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import tempfile
import threading
import time
from typing import Any, Callable

from execution_policy import ExecutionPolicy, WRITE_ROLES
from orchestration_engine import CallRecord, RunContext


def extract_usage_details(events: list[dict[str, Any]]) -> dict[str, int]:
    totals = {
        "input_tokens": 0,
        "cached_input_tokens": 0,
        "output_tokens": 0,
        "reasoning_output_tokens": 0,
    }
    for event in events:
        usage = event.get("usage")
        if not isinstance(usage, dict):
            continue
        for key in totals:
            totals[key] = max(totals[key], int(usage.get(key, 0) or 0))
        input_details = usage.get("input_tokens_details")
        if isinstance(input_details, dict):
            totals["cached_input_tokens"] = max(
                totals["cached_input_tokens"], int(input_details.get("cached_tokens", 0) or 0)
            )
        output_details = usage.get("output_tokens_details")
        if isinstance(output_details, dict):
            totals["reasoning_output_tokens"] = max(
                totals["reasoning_output_tokens"], int(output_details.get("reasoning_tokens", 0) or 0)
            )
    return totals


def extract_thread_id(events: list[dict[str, Any]]) -> str:
    for event in events:
        if event.get("type") == "thread.started":
            return str(event.get("thread_id", ""))
    return ""


class CodexCliAdapter:
    """Codex CLI backend implementation of ExecutionAdapter."""
    def __init__(
        self,
        timeout_seconds: int = 600,
        effort_override: str | None = None,
        role_efforts: dict[str, str] | None = None,
        workdir: pathlib.Path = pathlib.Path.cwd(),
        execution_mode: bool = False,
        write_sandbox: str = "workspace-write",
        total_timeout_seconds: int | None = None,
        max_model_calls: int | None = None,
        max_total_tokens: int | None = None,
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
        context_mode: str = "full",
    ) -> None:
        if total_timeout_seconds is not None and total_timeout_seconds < 1:
            raise ValueError("total_timeout_seconds must be at least 1")
        if max_model_calls is not None and max_model_calls < 1:
            raise ValueError("max_model_calls must be at least 1")
        if max_total_tokens is not None and max_total_tokens < 1:
            raise ValueError("max_total_tokens must be at least 1")
        if context_mode not in {"lean", "full"}:
            raise ValueError(f"Unsupported context mode: {context_mode}")
        self.timeout_seconds = timeout_seconds
        self.effort_override = effort_override
        self.role_efforts = role_efforts or {}
        self.workdir = workdir.resolve()
        self.policy = ExecutionPolicy(execution_mode, write_sandbox)
        self.total_timeout_seconds = total_timeout_seconds
        self.max_model_calls = max_model_calls
        self.max_total_tokens = max_total_tokens
        self.progress_callback = progress_callback
        self.context_mode = context_mode
        self.started_at = time.monotonic()
        self.calls_started = 0
        self._call_lock = threading.Lock()
        self._call_reservations: dict[int, int] = {}
        self._token_lock = threading.Lock()
        self.usage_events_observed = 0
        self.observed_input_tokens = 0
        self.observed_cached_input_tokens = 0
        self.observed_output_tokens = 0
        self.observed_reasoning_output_tokens = 0
        self.codex_command = self._resolve_codex_command()

    def observed_usage(self) -> dict[str, int] | None:
        with self._token_lock:
            if self.usage_events_observed == 0:
                return None
            input_tokens = self.observed_input_tokens
            cached_input_tokens = self.observed_cached_input_tokens
            output_tokens = self.observed_output_tokens
            reasoning_output_tokens = self.observed_reasoning_output_tokens
        return {
            "input_tokens": input_tokens,
            "cached_input_tokens": cached_input_tokens,
            "output_tokens": output_tokens,
            "reasoning_output_tokens": reasoning_output_tokens,
            "total_tokens": input_tokens + output_tokens,
        }

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
                if powershell:
                    return [powershell, "-NoProfile", "-NonInteractive", "-File", executable]
                continue
            if suffix in {".cmd", ".bat"}:
                command_shell = shutil.which("cmd.exe") or os.environ.get("COMSPEC")
                if command_shell:
                    return [command_shell, "/d", "/c", executable]
                continue
            return [executable]
        raise RuntimeError("Codex CLI executable or wrapper was not found on PATH")

    def sandbox_for_role(self, role: str) -> str:
        return self.policy.sandbox_for_role(role)

    def preamble_for_role(self, role: str) -> str:
        return self.policy.preamble_for_role(role)

    def effective_effort(self, role: str, requested: str) -> str:
        role_key = "worker" if role.startswith("worker:") else role
        return (
            self.effort_override
            or self.role_efforts.get(role_key)
            or ("high" if requested == "max" else requested)
        )

    def reserve_call(self, role: str, projected_tokens: int = 0) -> int:
        with self._call_lock:
            with self._token_lock:
                observed_total = self.observed_input_tokens + self.observed_output_tokens
            reserved_total = sum(self._call_reservations.values())
            if (
                self.max_total_tokens is not None
                and observed_total >= self.max_total_tokens
                and role not in WRITE_ROLES
            ):
                raise RuntimeError(
                    f"Observed token budget exhausted before role={role}: "
                    f"observed={observed_total}, max_total_tokens={self.max_total_tokens}"
                )
            if (
                self.max_total_tokens is not None
                and observed_total + reserved_total + max(0, projected_tokens)
                > self.max_total_tokens
                and role not in WRITE_ROLES
            ):
                raise RuntimeError(
                    f"Projected token budget would be exceeded before role={role}: "
                    f"observed={observed_total}, reserved={reserved_total}, "
                    f"projected={projected_tokens}, "
                    f"max_total_tokens={self.max_total_tokens}"
                )
            if self.max_model_calls is not None and self.calls_started >= self.max_model_calls:
                raise RuntimeError(
                    f"Model call budget exhausted before role={role}: "
                    f"max_model_calls={self.max_model_calls}"
                )
            self.calls_started += 1
            call_index = self.calls_started
            self._call_reservations[call_index] = max(0, projected_tokens)
            return call_index

    def release_call_reservation(self, call_index: int) -> None:
        with self._call_lock:
            self._call_reservations.pop(call_index, None)

    def remaining_timeout(self) -> float:
        if self.total_timeout_seconds is None:
            return float(self.timeout_seconds)
        remaining = self.total_timeout_seconds - (time.monotonic() - self.started_at)
        if remaining <= 0:
            raise TimeoutError("Total orchestration timeout exhausted")
        return min(float(self.timeout_seconds), remaining)

    def emit_progress(self, event: dict[str, Any]) -> None:
        if self.progress_callback is not None:
            self.progress_callback(event)

    def configuration_flags(self, role: str) -> list[str]:
        flags: list[str] = []
        if not self.policy.execution_mode or (
            self.context_mode == "lean" and role not in WRITE_ROLES
        ):
            flags.append("--ignore-user-config")
        if not self.policy.execution_mode:
            flags.append("--ignore-rules")
        return flags

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
        effective_effort = self.effective_effort(role, effort)
        from model_registry import strip_backend_prefix
        execution_model = strip_backend_prefix(model, "codex")
        prompt = (
            f"{self.preamble_for_role(role)}\n\nWorkspace: {self.workdir}\n\n"
            f"Keep the response within {max_output_tokens} tokens.\n\n"
            f"INSTRUCTIONS:\n{instructions}\n\nINPUT:\n{input_text}"
        )
        projected_tokens = max_output_tokens + max(1, len(prompt) // 4)
        call_index = self.reserve_call(role, projected_tokens)
        self.emit_progress({
            "event": "role_started",
            "call": call_index,
            "role": role,
            "model": model,
            "effort": effective_effort,
            "sandbox": self.sandbox_for_role(role),
        })
        with tempfile.TemporaryDirectory(prefix="codex-cli-orchestration-") as temp_dir:
            output_path = pathlib.Path(temp_dir) / "last-message.txt"
            command = [*self.codex_command, "exec", "--ephemeral"]
            command.extend(self.configuration_flags(role))
            command.extend([
                "--skip-git-repo-check", "--sandbox", self.sandbox_for_role(role),
                "--color", "never", "--model", execution_model, "--config",
                f'model_reasoning_effort="{effective_effort}"', "--json",
                "--output-last-message", str(output_path), "--cd", str(self.workdir), "-",
            ])
            started = time.perf_counter()
            environment = os.environ.copy()
            environment["PYTHONDONTWRITEBYTECODE"] = "1"

            def observe_output(raw_output: str | bytes | None) -> tuple[list[dict[str, Any]], dict[str, int], int]:
                if isinstance(raw_output, bytes):
                    output = raw_output.decode("utf-8", errors="replace")
                else:
                    output = raw_output or ""
                parsed_events: list[dict[str, Any]] = []
                for line in output.splitlines():
                    try:
                        value = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(value, dict):
                        parsed_events.append(value)
                parsed_usage = extract_usage_details(parsed_events)
                usage_events = sum(
                    isinstance(event.get("usage"), dict) for event in parsed_events
                )
                with self._token_lock:
                    self.usage_events_observed += usage_events
                    self.observed_input_tokens += parsed_usage["input_tokens"]
                    self.observed_cached_input_tokens += parsed_usage["cached_input_tokens"]
                    self.observed_output_tokens += parsed_usage["output_tokens"]
                    self.observed_reasoning_output_tokens += parsed_usage["reasoning_output_tokens"]
                    total = self.observed_input_tokens + self.observed_output_tokens
                return parsed_events, parsed_usage, total

            try:
                try:
                    completed = subprocess.run(
                        command,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        input=prompt,
                        timeout=self.remaining_timeout(),
                        check=False,
                        env=environment,
                    )
                except subprocess.TimeoutExpired as exc:
                    _, usage, observed_total = observe_output(exc.stdout)
                    self.emit_progress({
                        "event": "role_failed",
                        "call": call_index,
                        "role": role,
                        "error": "timeout",
                        "input_tokens": usage["input_tokens"],
                        "output_tokens": usage["output_tokens"],
                        "observed_total_tokens": observed_total,
                    })
                    raise
                latency = time.perf_counter() - started
                events, usage, observed_total = observe_output(completed.stdout)
            finally:
                self.release_call_reservation(call_index)
            thread_id = extract_thread_id(events)
            if completed.returncode != 0:
                self.emit_progress({
                    "event": "role_failed",
                    "call": call_index,
                    "role": role,
                    "error": f"exit={completed.returncode}",
                    "input_tokens": usage["input_tokens"],
                    "output_tokens": usage["output_tokens"],
                    "observed_total_tokens": observed_total,
                })
                raise RuntimeError(
                    f"Codex CLI failed for role={role}, model={execution_model}, "
                    f"exit={completed.returncode}\nSTDERR:\n{completed.stderr[-2000:].strip()}\n"
                    f"STDOUT:\n{completed.stdout[-2000:].strip()}"
                )
            if not output_path.exists():
                self.emit_progress({"event": "role_failed", "call": call_index, "role": role, "error": "missing_output"})
                raise RuntimeError(f"Codex CLI produced no final message for role={role}")
            output_text = output_path.read_text(encoding="utf-8").strip()
            if not output_text:
                self.emit_progress({"event": "role_failed", "call": call_index, "role": role, "error": "empty_output"})
                raise RuntimeError(f"Codex CLI returned an empty message for role={role}")
            input_tokens = usage["input_tokens"]
            output_tokens = usage["output_tokens"]
            context.records.append(CallRecord(
                role=role,
                model=execution_model,
                effort=effective_effort,
                latency_seconds=latency,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                estimated_cost_usd=None,
                response_id=thread_id,
                cached_input_tokens=usage["cached_input_tokens"],
                reasoning_output_tokens=usage["reasoning_output_tokens"],
            ))
            self.emit_progress({
                "event": "role_completed",
                "call": call_index,
                "role": role,
                "latency_seconds": latency,
                "thread_id": thread_id,
                "input_tokens": input_tokens,
                "cached_input_tokens": usage["cached_input_tokens"],
                "output_tokens": output_tokens,
                "reasoning_output_tokens": usage["reasoning_output_tokens"],
                "observed_total_tokens": observed_total,
            })
            return output_text, {"events": events, "thread_id": thread_id}
