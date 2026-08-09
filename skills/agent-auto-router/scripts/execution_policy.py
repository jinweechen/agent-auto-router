from __future__ import annotations

from dataclasses import dataclass

WRITE_ROLES = frozenset({"direct", "reviewer"})
VALID_WRITE_SANDBOXES = frozenset(
    {"read-only", "workspace-write", "danger-full-access"}
)


@dataclass(frozen=True)
class ExecutionPolicy:
    execution_mode: bool = False
    write_sandbox: str = "workspace-write"

    def __post_init__(self) -> None:
        if self.write_sandbox not in VALID_WRITE_SANDBOXES:
            raise ValueError(f"Unsupported write sandbox: {self.write_sandbox}")

    def sandbox_for_role(self, role: str) -> str:
        if self.execution_mode and role in WRITE_ROLES:
            return self.write_sandbox
        return "read-only"

    def preamble_for_role(self, role: str) -> str:
        if not self.execution_mode:
            return (
                "You are a bounded evaluation worker. Do not call tools, inspect files, or "
                "modify anything. Respond directly using only the supplied task."
            )
        if role in WRITE_ROLES:
            return (
                "You are the single implementation role. Use available tools, inspect the "
                "workspace, follow repository instructions, and make the requested changes. "
                "Only modify files required by the task and report concrete changes and validation. "
                "Use a token-efficient workflow: avoid broad exploration, batch required reads, "
                "make one edit pass, run one combined validation command, and do not re-read files. "
                "Prefer python -B for tests and do not run py_compile unless explicitly required."
            )
        return (
            "You are a read-only orchestration role. You may inspect the workspace and run "
            "read-only commands to ground your response. Do not modify files. Return concrete, "
            "implementation-ready findings for the next role."
        )
