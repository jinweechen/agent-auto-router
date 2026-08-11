from __future__ import annotations

import pathlib
import threading
import time
from typing import Any, Callable

from execution_policy import ExecutionPolicy, WRITE_ROLES
from host_permissions import HostPermissions


class BaseCliAdapter:
    """Shared budgets, telemetry, and role policy for signed-in CLI adapters."""

    def __init__(
        self,
        *,
        timeout_seconds: int,
        effort_override: str | None,
        role_efforts: dict[str, str] | None,
        workdir: pathlib.Path,
        execution_mode: bool,
        write_sandbox: str,
        total_timeout_seconds: int | None,
        max_model_calls: int | None,
        max_total_tokens: int | None,
        progress_callback: Callable[[dict[str, Any]], None] | None,
        host_permissions: HostPermissions | None,
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
        self.policy = ExecutionPolicy(execution_mode, write_sandbox)
        self.total_timeout_seconds = total_timeout_seconds
        self.max_model_calls = max_model_calls
        self.max_total_tokens = max_total_tokens
        self.progress_callback = progress_callback
        self.host_permissions = host_permissions
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

    def record_usage(self, usage: dict[str, int], *, event_count: int = 1) -> int:
        with self._token_lock:
            self.usage_events_observed += max(0, event_count)
            self.observed_input_tokens += int(usage.get("input_tokens", 0))
            self.observed_cached_input_tokens += int(
                usage.get("cached_input_tokens", 0)
            )
            self.observed_output_tokens += int(usage.get("output_tokens", 0))
            self.observed_reasoning_output_tokens += int(
                usage.get("reasoning_output_tokens", 0)
            )
            return self.observed_input_tokens + self.observed_output_tokens

    def sandbox_for_role(self, role: str) -> str:
        return self.policy.sandbox_for_role(role)

    def preamble_for_role(self, role: str) -> str:
        return self.policy.preamble_for_role(role)

    def build_prompt(
        self,
        *,
        role: str,
        instructions: str,
        input_text: str,
        max_output_tokens: int,
    ) -> str:
        return (
            f"{self.preamble_for_role(role)}\n\nWorkspace: {self.workdir}\n\n"
            f"Keep the response within {max_output_tokens} tokens.\n\n"
            f"INSTRUCTIONS:\n{instructions}\n\nINPUT:\n{input_text}"
        )

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
