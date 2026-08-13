#!/usr/bin/env python3
"""Validate the current Desktop host's per-child spawn argument support."""

from __future__ import annotations

import json
import pathlib
from dataclasses import dataclass
from typing import Any, Mapping

from protocol_schemas import DESKTOP_SPAWN_CAPABILITIES_SCHEMA


SCHEMA = DESKTOP_SPAWN_CAPABILITIES_SCHEMA
_TOP_LEVEL_FIELDS = frozenset({"schema", "source", "currentWorkdir", "arguments"})
_ARGUMENT_FIELDS = frozenset({
    "model",
    "reasoningEffort",
    "forkTurns",
    "workdir",
    "sandbox",
})


@dataclass(frozen=True)
class DesktopSpawnCapabilities:
    source: str
    current_workdir: pathlib.Path
    arguments: Mapping[str, bool]

    def as_plan(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA,
            "source": self.source,
            "currentWorkdir": str(self.current_workdir),
            "arguments": dict(self.arguments),
        }

    def basic_issues(self, workdir: pathlib.Path) -> list[str]:
        issues = [
            name
            for name in ("model", "reasoningEffort", "forkTurns")
            if not self.arguments[name]
        ]
        if workdir != self.current_workdir and not self.arguments["workdir"]:
            issues.append("workdir")
        return issues

    def sandbox_issue(
        self,
        *,
        host_sandbox: str,
        effective_sandbox: str,
        requires_read_only_roles: bool,
    ) -> bool:
        if self.arguments["sandbox"]:
            return False
        if effective_sandbox != host_sandbox:
            return True
        return requires_read_only_roles and host_sandbox != "read-only"


def parse_desktop_spawn_capabilities(
    value: str | Mapping[str, Any],
) -> DesktopSpawnCapabilities:
    """Parse trusted capability metadata derived from the live spawn tool schema."""
    if isinstance(value, str):
        try:
            raw = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"Desktop spawn capabilities must be valid JSON: {exc.msg}"
            ) from exc
    else:
        raw = dict(value)
    if set(raw) != _TOP_LEVEL_FIELDS:
        missing = sorted(_TOP_LEVEL_FIELDS - set(raw))
        unknown = sorted(set(raw) - _TOP_LEVEL_FIELDS)
        raise ValueError(
            "Desktop spawn capabilities must use the closed field set; "
            f"missing={missing}, unknown={unknown}"
        )
    if raw.get("schema") != SCHEMA:
        raise ValueError(f"Desktop spawn capabilities schema must be {SCHEMA}")
    source = raw.get("source")
    if not isinstance(source, str) or not source.strip():
        raise ValueError("Desktop spawn capabilities source must be a non-empty string")
    current_workdir = raw.get("currentWorkdir")
    if not isinstance(current_workdir, str) or not current_workdir.strip():
        raise ValueError("Desktop spawn capabilities currentWorkdir must be an absolute path")
    path = pathlib.Path(current_workdir).expanduser()
    if not path.is_absolute():
        raise ValueError("Desktop spawn capabilities currentWorkdir must be an absolute path")
    resolved_workdir = path.resolve(strict=True)
    if not resolved_workdir.is_dir():
        raise ValueError("Desktop spawn capabilities currentWorkdir must be a directory")
    arguments = raw.get("arguments")
    if not isinstance(arguments, Mapping) or set(arguments) != _ARGUMENT_FIELDS:
        keys = set(arguments) if isinstance(arguments, Mapping) else set()
        missing = sorted(_ARGUMENT_FIELDS - keys)
        unknown = sorted(keys - _ARGUMENT_FIELDS)
        raise ValueError(
            "Desktop spawn capability arguments must use the closed field set; "
            f"missing={missing}, unknown={unknown}"
        )
    normalized_arguments: dict[str, bool] = {}
    for name in sorted(_ARGUMENT_FIELDS):
        supported = arguments[name]
        if not isinstance(supported, bool):
            raise ValueError(f"Desktop spawn capability argument {name} must be boolean")
        normalized_arguments[name] = supported
    return DesktopSpawnCapabilities(
        source=source.strip(),
        current_workdir=resolved_workdir,
        arguments=normalized_arguments,
    )
