from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import threading
import time
from typing import Any, Callable

from execution_policy import ExecutionPolicy, WRITE_ROLES
from orchestration_engine import CallRecord, RunContext


def extract_claude_usage(payload: dict[str, Any]) -> dict[str, int]:
    """Extract token usage from a Claude Code CLI JSON payload."""
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        usage = {}
    return {
        "input_tokens": int(usage.get("input_tokens", 0) or 0),
        "cached_input_tokens": int(usage.get("cache_read_input_tokens", 0) or 0),
        "output_tokens": int(usage.get("output_tokens", 0) or 0),
        "reasoning_output_tokens": int(usage.get("reasoning_tokens", 0) or 0),
    }


def extract_result_text(payload: dict[str, Any]) -> str | None:
    """Return the result field from a Claude Code CLI JSON payload, or None."""
    result = payload.get("result")
    if isinstance(result, str) and result:
        return result
    return None


def parse_claude_json_output(raw: str) -> dict[str, Any]:
    """Tolerantly parse Claude Code CLI stdout, extracting the JSON result object."""
    cleaned = raw.strip()
    try:
        value = json.loads(cleaned)
        if isinstance(value, dict):
            return value
    except json.JSONDecodeError:
        pass
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start >= 0 and end > start:
        try:
            value = json.loads(cleaned[start : end + 1])
            if isinstance(value, dict):
                return value
        except json.JSONDecodeError:
            pass
    raise ValueError("No JSON object found in Claude CLI output")


class ClaudeCliAdapter:
    """Claude Code CLI backend implementation of ExecutionAdapter."""

    def __init__(
        self,
        timeout_seconds: int = 600,
        effort_override: str | None = None,
        role_efforts: dict[str, str] | None = None,
        workdir: pathlib.Path = pathlib.Path.cwd(),
        execution_mode: bool = False,
        max_turns: int = 30,
        allowed_tools: tuple[str, ...] = ("Read", "Edit", "Write", "Bash"),
        model_map: dict[str, str] | None = None,
        total_timeout_seconds: int | None = None,
        max_model_calls: int | None = None,
        max_total_tokens: int | None = None,
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        if total_timeout_seconds is not None and total_timeout_seconds < 1:
            raise ValueError("total_timeout_seconds must be at least 1")
        if max_model_calls is not None and max_model_calls < 1:
            raise ValueError("max_model_calls must be at least 1")
        if max_total_tokens is not None and max_total_tokens < 1:
            raise ValueError("max_total_tokens must be at least 1")
        self.timeout_seconds = timeout_seconds
        self.effort_override = effort_override
        self.role_efforts = role_efforts or {}
        self.workdir = workdir.resolve()
        self.execution_mode = execution_mode
        self.max_turns = max_turns
        self.allowed_tools = allowed_tools
        self.model_map = model_map if model_map is not None else {}
        self.total_timeout_seconds = total_timeout_seconds
        self.max_model_calls = max_model_calls
        self.max_total_tokens = max_total_tokens
        self.progress_callback = progress_callback
        self.policy = ExecutionPolicy(execution_mode, write_sandbox="workspace-write")
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
        self.claude_command = self._resolve_claude_command()

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
    def _resolve_claude_command() -> list[str]:
        checked: set[str] = set()
        for name in ("claude", "claude.exe", "claude.cmd", "claude.bat", "claude.ps1"):
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
        raise RuntimeError("Claude Code CLI executable or wrapper was not found on PATH")

    def _resolve_model(self, model: str) -> str:
        from model_registry import strip_backend_prefix
        if ":" in model:
            name = strip_backend_prefix(model, "claude")
        else:
            name = model
        return self.model_map.get(name, name)

    def _finalize_argv(self, argv: list[str]) -> list[str]:
        """Work around Windows cmd.exe wrapper argument re-parsing.

        When the resolved command is a .cmd/.bat wrapper executed through
        ``cmd.exe /d /c <wrapper> <args...>``, cmd re-parses the arguments and
        splits whitespace inside quoted values, which corrupts multi-word
        values such as the prompt. Pass the wrapper plus all arguments as ONE
        command-line string to ``/c`` (quoted by list2cmdline) so cmd cannot
        re-split them.
        """
        if (
            os.name == "nt"
            and len(argv) >= 4
            and argv[1:3] == ["/d", "/c"]
            and pathlib.Path(argv[0]).name.lower() in {"cmd.exe"}
        ):
            return [argv[0], "/d", "/c", subprocess.list2cmdline(argv[3:])]
        return argv

    def _normalize_effort(self, effort: str) -> str:
        return "low" if effort == "none" else effort

    def effective_effort(self, role: str, requested: str) -> str:
        role_key = "worker" if role.startswith("worker:") else role
        return (
            self.effort_override
            or self.role_efforts.get(role_key)
            or ("high" if requested == "max" else requested)
        )

    def _allowed_tools_for_role(self, role: str) -> list[str]:
        if self.execution_mode and role in WRITE_ROLES:
            return list(self.allowed_tools)
        return ["Read"]

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
        claude_model = self._resolve_model(model)
        prompt = (
            f"{self.policy.preamble_for_role(role)}\n\nWorkspace: {self.workdir}\n\n"
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
            "backend": "claude-code",
        })
        tools_for_role = self._allowed_tools_for_role(role)
        normalized_effort = self._normalize_effort(effective_effort)
        argv = [
            *self.claude_command,
            "-p",
            "--model", claude_model,
            "--effort", normalized_effort,
            "--output-format", "json",
            "--max-turns", str(self.max_turns),
            "--allowedTools", " ".join(tools_for_role),
        ]
        # -p without a value reads the task from stdin. Passing the full
        # prompt via stdin (instead of an argv value) keeps multi-line prompts
        # intact even when the resolved command is a .cmd/.bat wrapper executed
        # through cmd.exe /d /c, whose line-based parsing would truncate a
        # prompt containing newlines.
        argv = self._finalize_argv(argv)
        environment = os.environ.copy()
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        started = time.perf_counter()
        try:
            try:
                completed = subprocess.run(
                    argv,
                    cwd=self.workdir,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=self.remaining_timeout(),
                    check=False,
                    env=environment,
                    input=prompt,
                )
            except subprocess.TimeoutExpired as exc:
                self.emit_progress({
                    "event": "role_failed",
                    "call": call_index,
                    "role": role,
                    "error": "timeout",
                })
                raise
            latency = time.perf_counter() - started
        finally:
            self.release_call_reservation(call_index)
        try:
            payload = parse_claude_json_output(completed.stdout)
        except ValueError:
            self.emit_progress({
                "event": "role_failed",
                "call": call_index,
                "role": role,
                "error": "invalid_json",
            })
            raise RuntimeError(
                f"Claude Code CLI returned invalid JSON for role={role}\n"
                f"STDERR:\n{completed.stderr[-2000:].strip()}\n"
                f"STDOUT:\n{completed.stdout[-2000:].strip()}"
            )
        if completed.returncode != 0:
            self.emit_progress({
                "event": "role_failed",
                "call": call_index,
                "role": role,
                "error": f"exit={completed.returncode}",
            })
            raise RuntimeError(
                f"Claude Code CLI failed for role={role}, model={claude_model}, "
                f"exit={completed.returncode}\nSTDERR:\n{completed.stderr[-2000:].strip()}\n"
                f"STDOUT:\n{completed.stdout[-2000:].strip()}"
            )
        result_text = extract_result_text(payload)
        if result_text is None:
            self.emit_progress({
                "event": "role_failed",
                "call": call_index,
                "role": role,
                "error": "missing_output",
            })
            raise RuntimeError(
                f"Claude Code CLI produced no result for role={role}"
            )
        usage = extract_claude_usage(payload)
        with self._token_lock:
            self.usage_events_observed += 1
            self.observed_input_tokens += usage["input_tokens"]
            self.observed_cached_input_tokens += usage["cached_input_tokens"]
            self.observed_output_tokens += usage["output_tokens"]
            self.observed_reasoning_output_tokens += usage["reasoning_output_tokens"]
            observed_total = self.observed_input_tokens + self.observed_output_tokens
        cost_usd = payload.get("total_cost_usd")
        cost_usd = cost_usd if isinstance(cost_usd, (int, float)) else None
        context.records.append(CallRecord(
            role=role,
            model=claude_model,
            effort=effective_effort,
            latency_seconds=latency,
            input_tokens=usage["input_tokens"],
            output_tokens=usage["output_tokens"],
            estimated_cost_usd=cost_usd,
            response_id="",
            cached_input_tokens=usage["cached_input_tokens"],
            reasoning_output_tokens=usage["reasoning_output_tokens"],
        ))
        self.emit_progress({
            "event": "role_completed",
            "call": call_index,
            "role": role,
            "latency_seconds": latency,
            "input_tokens": usage["input_tokens"],
            "cached_input_tokens": usage["cached_input_tokens"],
            "output_tokens": usage["output_tokens"],
            "reasoning_output_tokens": usage["reasoning_output_tokens"],
            "observed_total_tokens": observed_total,
            "backend": "claude-code",
            "cost_usd": cost_usd,
        })
        return result_text, {"payload": payload}
