#!/usr/bin/env python3
"""Normalize host-provided permissions and derive a non-escalating child policy."""

from __future__ import annotations

import json
import pathlib
from dataclasses import dataclass
from typing import Any, Mapping

from protocol_schemas import HOST_PERMISSIONS_SCHEMA

SCHEMA = HOST_PERMISSIONS_SCHEMA
SANDBOX_ORDER = {
    "read-only": 0,
    "workspace-write": 1,
    "danger-full-access": 2,
}
SANDBOX_TYPES = {
    "readOnly": "read-only",
    "workspaceWrite": "workspace-write",
    "dangerFullAccess": "danger-full-access",
    "externalSandbox": "external-sandbox",
}
APPROVAL_POLICIES = frozenset({"untrusted", "on-failure", "on-request", "never"})


@dataclass(frozen=True)
class HostPermissions:
    source: str
    sandbox: str
    approval_policy: str
    network_access: bool | None
    writable_roots: tuple[str, ...]
    can_request_permissions: bool
    profile_id: str | None = None

    @property
    def codex_approval_policy(self) -> str:
        """Return a current Codex CLI approval value for legacy host snapshots."""
        return "on-request" if self.approval_policy == "on-failure" else self.approval_policy

    def effective_sandbox(self, requested: str = "inherit") -> str:
        """Return a child sandbox that can never be broader than the host sandbox."""
        if self.sandbox == "external-sandbox":
            if requested not in {"inherit", "external-sandbox"}:
                raise ValueError("external-sandbox permissions cannot be remapped by the router")
            return "external-sandbox"
        if requested == "inherit":
            return self.sandbox
        if requested not in SANDBOX_ORDER:
            raise ValueError(f"unsupported requested sandbox: {requested}")
        return min((self.sandbox, requested), key=SANDBOX_ORDER.__getitem__)

    def as_plan(self, requested: str = "inherit") -> dict[str, Any]:
        effective = self.effective_sandbox(requested)
        return {
            "schema": SCHEMA,
            "source": self.source,
            "profileId": self.profile_id,
            "inheritance": "automatic",
            "hostSandbox": self.sandbox,
            "requestedSandbox": requested,
            "effectiveSandbox": effective,
            "approvalPolicy": self.approval_policy,
            "networkAccess": self.network_access,
            "writableRoots": list(self.writable_roots),
            "canRequestPermissions": self.can_request_permissions,
            "noPrivilegeEscalation": True,
        }

    def as_snapshot(self) -> dict[str, Any]:
        """Return the canonical privacy-safe snapshot for a downstream adapter."""
        return {
            "schema": SCHEMA,
            "source": self.source,
            "sandbox": self.sandbox,
            "approvalPolicy": self.approval_policy,
            "networkAccess": self.network_access,
            "writableRoots": list(self.writable_roots),
            "canRequestPermissions": self.can_request_permissions,
            "profileId": self.profile_id,
        }


def _absolute_roots(values: Any) -> tuple[str, ...]:
    if values is None:
        return ()
    if not isinstance(values, list) or not all(isinstance(item, str) for item in values):
        raise ValueError("writableRoots must be an array of absolute paths")
    normalized: list[str] = []
    for value in values:
        path = pathlib.Path(value).expanduser()
        if not path.is_absolute():
            raise ValueError(f"writable root must be absolute: {value}")
        resolved = str(path.resolve(strict=False))
        if resolved not in normalized:
            normalized.append(resolved)
    return tuple(normalized)


def parse_host_permissions(value: str | Mapping[str, Any]) -> HostPermissions:
    """Parse the trusted permission snapshot supplied by the current host runtime."""
    if isinstance(value, str):
        try:
            raw = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError(f"host permissions must be valid JSON: {exc.msg}") from exc
    else:
        raw = dict(value)
    if raw.get("schema") != SCHEMA:
        raise ValueError(f"host permissions schema must be {SCHEMA}")
    source = raw.get("source")
    if not isinstance(source, str) or not source.strip():
        raise ValueError("host permissions source must be a non-empty string")

    sandbox = raw.get("sandbox")
    sandbox_policy = raw.get("sandboxPolicy")
    if sandbox is None and isinstance(sandbox_policy, Mapping):
        sandbox = SANDBOX_TYPES.get(sandbox_policy.get("type"))
    if sandbox not in {*SANDBOX_ORDER, "external-sandbox"}:
        raise ValueError(f"unsupported host sandbox: {sandbox}")

    approval = raw.get("approvalPolicy")
    if approval not in APPROVAL_POLICIES:
        raise ValueError(f"unsupported host approval policy: {approval}")

    network = raw.get("networkAccess")
    if network is None and isinstance(sandbox_policy, Mapping):
        network = sandbox_policy.get("networkAccess")
    if isinstance(network, str):
        if network not in {"enabled", "disabled"}:
            raise ValueError("networkAccess must be enabled, disabled, true, false, or null")
        network = network == "enabled"
    if network is not None and not isinstance(network, bool):
        raise ValueError("networkAccess must be enabled, disabled, true, false, or null")

    roots = raw.get("writableRoots")
    if roots is None and isinstance(sandbox_policy, Mapping):
        roots = sandbox_policy.get("writableRoots")
    writable_roots = _absolute_roots(roots)
    if sandbox == "read-only" and writable_roots:
        raise ValueError("read-only host permissions cannot declare writable roots")
    if sandbox == "workspace-write" and not writable_roots:
        raise ValueError("workspace-write host permissions require at least one writable root")

    can_request = raw.get("canRequestPermissions", False)
    if not isinstance(can_request, bool):
        raise ValueError("canRequestPermissions must be boolean")
    profile_id = raw.get("profileId")
    if profile_id is not None and not isinstance(profile_id, str):
        raise ValueError("profileId must be a string or null")
    return HostPermissions(
        source=source.strip(),
        sandbox=sandbox,
        approval_policy=approval,
        network_access=network,
        writable_roots=writable_roots,
        can_request_permissions=can_request,
        profile_id=profile_id,
    )


def workdir_is_writable(workdir: pathlib.Path, permissions: HostPermissions) -> bool:
    sandbox = permissions.sandbox
    if sandbox in {"danger-full-access", "external-sandbox"}:
        return True
    if sandbox == "read-only":
        return False
    resolved = workdir.resolve(strict=True)
    for root_value in permissions.writable_roots:
        root = pathlib.Path(root_value)
        try:
            resolved.relative_to(root)
            return True
        except ValueError:
            continue
    return False


def cli_permission_issue(
    permissions: HostPermissions, effective_sandbox: str | None = None
) -> str | None:
    """Explain permission combinations a child CLI cannot safely reproduce."""
    effective = effective_sandbox or permissions.sandbox
    if effective == "external-sandbox":
        return "external-sandbox is enforced by the host and cannot be recreated by a child CLI"
    if effective == "danger-full-access" and permissions.network_access is not True:
        return "danger-full-access without explicitly enabled network cannot be represented by a child CLI"
    return None
