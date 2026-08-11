from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import time
from typing import Any, Callable

from cli_adapter_base import BaseCliAdapter
from execution_policy import WRITE_ROLES
from execution_types import CallRecord, RunContext
from host_permissions import HostPermissions


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


class ClaudeCliAdapter(BaseCliAdapter):
    """Claude Code CLI backend implementation of ExecutionAdapter."""

    def __init__(
        self,
        timeout_seconds: int = 600,
        effort_override: str | None = None,
        role_efforts: dict[str, str] | None = None,
        workdir: pathlib.Path = pathlib.Path.cwd(),
        execution_mode: bool = False,
        write_sandbox: str = "workspace-write",
        max_turns: int = 30,
        allowed_tools: tuple[str, ...] = ("Read", "Edit", "Write", "Bash"),
        model_map: dict[str, str] | None = None,
        total_timeout_seconds: int | None = None,
        max_model_calls: int | None = None,
        max_total_tokens: int | None = None,
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
        host_permissions: HostPermissions | None = None,
    ) -> None:
        super().__init__(
            timeout_seconds=timeout_seconds,
            effort_override=effort_override,
            role_efforts=role_efforts,
            workdir=workdir,
            execution_mode=execution_mode,
            write_sandbox=write_sandbox,
            total_timeout_seconds=total_timeout_seconds,
            max_model_calls=max_model_calls,
            max_total_tokens=max_total_tokens,
            progress_callback=progress_callback,
            host_permissions=host_permissions,
        )
        self.max_turns = max_turns
        self.allowed_tools = allowed_tools
        self.model_map = model_map if model_map is not None else {}
        self.claude_command = self._resolve_claude_command()

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

    def _allowed_tools_for_role(self, role: str) -> list[str]:
        if self.execution_mode and role in WRITE_ROLES:
            return list(self.allowed_tools)
        return ["Read"]

    def permission_argv_for_role(self, role: str) -> list[str]:
        """Build a bounded Claude tool surface without treating allow rules as a sandbox."""
        tools = self._allowed_tools_for_role(role)
        argv = ["--tools", " ".join(tools)]
        host_permissions = getattr(self, "host_permissions", None)
        if not self.execution_mode or role not in WRITE_ROLES:
            return [*argv, "--allowedTools", "Read", "--permission-mode", "dontAsk"]
        if host_permissions is None:
            return [*argv, "--permission-mode", "acceptEdits"]
        if (
            self.policy.write_sandbox == "danger-full-access"
            and host_permissions.approval_policy == "never"
        ):
            return [
                *argv,
                "--allow-dangerously-skip-permissions",
                "--permission-mode",
                "bypassPermissions",
            ]
        if host_permissions.approval_policy == "never":
            preapproved = [tool for tool in tools if tool != "Bash"]
            return [
                *argv,
                "--allowedTools",
                " ".join(preapproved),
                "--permission-mode",
                "dontAsk",
            ]
        return [*argv, "--permission-mode", "default"]

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
        prompt = self.build_prompt(
            role=role,
            instructions=instructions,
            input_text=input_text,
            max_output_tokens=max_output_tokens,
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
        normalized_effort = self._normalize_effort(effective_effort)
        host_permissions = getattr(self, "host_permissions", None)
        argv = [
            *self.claude_command,
            "-p",
            "--model", claude_model,
            "--effort", normalized_effort,
            "--output-format", "json",
            "--max-turns", str(self.max_turns),
        ]
        argv.extend(self.permission_argv_for_role(role))
        if host_permissions is not None:
            for root in host_permissions.writable_roots:
                argv.extend(["--add-dir", root])
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
        observed_total = self.record_usage(usage)
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
