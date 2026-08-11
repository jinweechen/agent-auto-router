from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from execution_types import RunContext


@runtime_checkable
class ExecutionAdapter(Protocol):
    """Backend-agnostic execution contract: one create() call is one model-role invocation.

    Implementations wrap a concrete execution backend (a coding-agent CLI such as
    Codex CLI, Claude Code CLI, OpenCode, or a generic API client). The orchestrator
    and single-task runner depend only on this protocol, never on backend details.
    """

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
        """Run one model-role call and return (final_message, metadata)."""
        ...

    def observed_usage(self) -> dict[str, int] | None:
        """Return aggregated token usage observed so far, or None when unavailable."""
        ...

    def emit_progress(self, event: dict[str, Any]) -> None:
        """Emit a structured progress event (no-op when no callback is set)."""
        ...
