"""Durable storage primitives for router control-plane state."""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from state_lock import append_lock


TRANSACTION_SCHEMA_VERSION = 1
REVISION_SCHEMA_VERSION = 1
SAFE_OPERATION_PATTERN = re.compile(r"[A-Za-z0-9._:-]{1,80}")
SAFE_TRANSACTION_ID_PATTERN = re.compile(r"[a-f0-9]{32}")
FORBIDDEN_JOURNAL_KEYS = frozenset(
    {
        "task",
        "tasktext",
        "prompt",
        "input",
        "output",
        "modeloutput",
        "tooloutput",
        "content",
        "credential",
        "credentials",
        "secret",
        "token",
        "api_key",
        "apikey",
    }
)


class ControlPlaneRecoveryRequired(ValueError):
    """Raised when routing state has an unfinished durable transaction."""


@dataclass(frozen=True)
class ControlPlanePaths:
    """Canonical paths for one isolated router control plane."""

    state_dir: Path
    feedback_file: Path | None = None

    @property
    def active_policy(self) -> Path:
        return self.state_dir / "active-policy.json"

    @property
    def audit(self) -> Path:
        return self.state_dir / "audit.jsonl"

    @property
    def candidates(self) -> Path:
        return self.state_dir / "candidates"

    @property
    def config(self) -> Path:
        return self.state_dir / "guarded-auto-config.json"

    @property
    def feedback(self) -> Path:
        return self.feedback_file or self.state_dir / "feedback.jsonl"

    @property
    def history(self) -> Path:
        return self.state_dir / "history"

    @property
    def lifecycle(self) -> Path:
        return self.state_dir / "guarded-auto-state.json"

    @property
    def pending_transaction(self) -> Path:
        return self.state_dir / ".control-plane-transaction.json"

    @property
    def reports(self) -> Path:
        return self.state_dir / "reports"

    @property
    def revision(self) -> Path:
        return self.state_dir / ".control-plane-revision.json"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical_digest(payload: Mapping[str, Any]) -> str:
    data = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    """Replace one JSON file after flushing its complete temporary file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(payload, ensure_ascii=True, indent=2) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def append_jsonl(path: Path, payload: Mapping[str, Any]) -> None:
    """Append and flush one JSONL object under the stream's OS lock."""
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    with append_lock(path) as acquired:
        if not acquired:
            raise RuntimeError(f"timed out waiting to append router state: {path.name}")
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(line + "\n")
            handle.flush()
            os.fsync(handle.fileno())


def _reject_sensitive_journal_keys(value: Any) -> None:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            if str(key).lower() in FORBIDDEN_JOURNAL_KEYS:
                raise ValueError(f"control-plane transaction may not store field: {key}")
            _reject_sensitive_journal_keys(nested)
    elif isinstance(value, list):
        for nested in value:
            _reject_sensitive_journal_keys(nested)


def resolve_control_plane_path(state_dir: Path, target: Path) -> Path:
    """Resolve a control-plane path and reject links that escape its state root."""
    root = state_dir.resolve(strict=False)
    resolved = target.resolve(strict=False)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError("control-plane path is outside the state directory") from exc
    return resolved


def _relative_target(state_dir: Path, target: Path) -> str:
    root = state_dir.resolve(strict=False)
    resolved = resolve_control_plane_path(state_dir, target)
    relative = resolved.relative_to(root)
    if not relative.parts:
        raise ValueError("control-plane transaction target must be a file")
    return relative.as_posix()


def _target_from_relative(state_dir: Path, relative_value: Any) -> Path:
    if not isinstance(relative_value, str) or not relative_value:
        raise ValueError("control-plane transaction target path is invalid")
    relative = Path(relative_value)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("control-plane transaction target path is invalid")
    target = state_dir / relative
    _relative_target(state_dir, target)
    return target


def _validate_transaction(
    state_dir: Path, payload: Any
) -> tuple[str, list[tuple[Path, dict[str, Any]]], list[dict[str, Any]], dict[str, Any]]:
    if not isinstance(payload, dict) or payload.get("schemaVersion") != TRANSACTION_SCHEMA_VERSION:
        raise ValueError("unsupported control-plane transaction")
    allowed = {
        "schemaVersion",
        "transactionId",
        "operation",
        "createdAt",
        "writes",
        "auditEvents",
        "revision",
        "storesTaskText",
    }
    if set(payload) - allowed:
        raise ValueError("control-plane transaction contains unsupported fields")
    transaction_id = payload.get("transactionId")
    if not isinstance(transaction_id, str) or not SAFE_TRANSACTION_ID_PATTERN.fullmatch(
        transaction_id
    ):
        raise ValueError("invalid control-plane transaction ID")
    operation = payload.get("operation")
    if not isinstance(operation, str) or not SAFE_OPERATION_PATTERN.fullmatch(operation):
        raise ValueError("invalid control-plane transaction operation")
    if payload.get("storesTaskText") is not False:
        raise ValueError("control-plane transaction privacy marker is missing")
    raw_writes = payload.get("writes")
    raw_events = payload.get("auditEvents")
    revision = payload.get("revision")
    if not isinstance(raw_writes, list) or not raw_writes:
        raise ValueError("control-plane transaction must contain at least one write")
    if not isinstance(raw_events, list) or not isinstance(revision, dict):
        raise ValueError("control-plane transaction audit or revision is invalid")
    if revision.get("schemaVersion") != REVISION_SCHEMA_VERSION:
        raise ValueError("unsupported control-plane revision")
    if revision.get("transactionId") != transaction_id:
        raise ValueError("control-plane revision does not match its transaction")

    paths = ControlPlanePaths(state_dir)
    reserved = {
        paths.audit.resolve(strict=False),
        paths.pending_transaction.resolve(strict=False),
        paths.revision.resolve(strict=False),
        (state_dir / ".guarded-auto.lock").resolve(strict=False),
    }
    writes: list[tuple[Path, dict[str, Any]]] = []
    seen_targets: set[Path] = set()
    for entry in raw_writes:
        if not isinstance(entry, dict) or set(entry) != {"path", "payload"}:
            raise ValueError("invalid control-plane transaction write")
        target = _target_from_relative(state_dir, entry["path"])
        resolved = target.resolve(strict=False)
        if resolved in reserved or resolved in seen_targets:
            raise ValueError("control-plane transaction contains a reserved or duplicate target")
        write_payload = entry["payload"]
        if not isinstance(write_payload, dict):
            raise ValueError("control-plane transaction JSON payload must be an object")
        seen_targets.add(resolved)
        writes.append((target, write_payload))

    events: list[dict[str, Any]] = []
    for event in raw_events:
        if not isinstance(event, dict) or not isinstance(event.get("eventType"), str):
            raise ValueError("invalid control-plane transaction audit event")
        if event.get("transactionId") != transaction_id:
            raise ValueError("control-plane audit event does not match its transaction")
        events.append(event)
    _reject_sensitive_journal_keys(payload)
    return transaction_id, writes, events, revision


def _append_audit_once(path: Path, event: Mapping[str, Any]) -> None:
    transaction_id = event["transactionId"]
    path.parent.mkdir(parents=True, exist_ok=True)
    with append_lock(path) as acquired:
        if not acquired:
            raise RuntimeError(f"timed out waiting to append router state: {path.name}")
        if path.is_file():
            for line_number, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), start=1
            ):
                if not line.strip():
                    continue
                try:
                    existing = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ValueError(f"invalid audit JSON at line {line_number}") from exc
                if not isinstance(existing, dict):
                    raise ValueError(f"audit line {line_number} must be an object")
                if existing.get("transactionId") == transaction_id:
                    return
        line = json.dumps(event, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(line + "\n")
            handle.flush()
            os.fsync(handle.fileno())


def _apply_transaction(state_dir: Path, payload: dict[str, Any]) -> str:
    transaction_id, writes, events, revision = _validate_transaction(state_dir, payload)
    paths = ControlPlanePaths(state_dir)
    for target, write_payload in writes:
        atomic_write_json(target, write_payload)
    for event in events:
        _append_audit_once(resolve_control_plane_path(state_dir, paths.audit), event)
    atomic_write_json(resolve_control_plane_path(state_dir, paths.revision), revision)
    return transaction_id


def recover_pending_transaction(state_dir: Path) -> str | None:
    """Replay one prepared transaction. The caller must hold the control-plane lock."""
    paths = ControlPlanePaths(state_dir)
    pending = resolve_control_plane_path(state_dir, paths.pending_transaction)
    if not pending.is_file():
        return None
    try:
        payload = json.loads(pending.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError("control-plane transaction journal is corrupted") from exc
    transaction_id = _apply_transaction(state_dir, payload)
    pending.unlink()
    return transaction_id


def commit_control_plane_transaction(
    state_dir: Path,
    *,
    operation: str,
    writes: Iterable[tuple[Path, Mapping[str, Any]]],
    audit_events: Iterable[Mapping[str, Any]] = (),
) -> str:
    """Durably commit related JSON writes and idempotent audit events."""
    if not SAFE_OPERATION_PATTERN.fullmatch(operation):
        raise ValueError("invalid control-plane transaction operation")
    recover_pending_transaction(state_dir)
    transaction_id = uuid.uuid4().hex
    created_at = utc_now()
    normalized_writes = [
        {"path": _relative_target(state_dir, target), "payload": dict(payload)}
        for target, payload in writes
    ]
    normalized_events = []
    for event in audit_events:
        normalized = dict(event)
        existing_id = normalized.get("transactionId")
        if existing_id is not None and existing_id != transaction_id:
            raise ValueError("audit event already belongs to another transaction")
        normalized["transactionId"] = transaction_id
        normalized_events.append(normalized)
    journal = {
        "schemaVersion": TRANSACTION_SCHEMA_VERSION,
        "transactionId": transaction_id,
        "operation": operation,
        "createdAt": created_at,
        "writes": normalized_writes,
        "auditEvents": normalized_events,
        "revision": {
            "schemaVersion": REVISION_SCHEMA_VERSION,
            "transactionId": transaction_id,
            "operation": operation,
            "committedAt": created_at,
        },
        "storesTaskText": False,
    }
    _validate_transaction(state_dir, journal)
    paths = ControlPlanePaths(state_dir)
    pending = resolve_control_plane_path(state_dir, paths.pending_transaction)
    atomic_write_json(pending, journal)
    _apply_transaction(state_dir, journal)
    pending.unlink()
    return transaction_id


def control_plane_revision(state_dir: Path) -> str:
    """Return a stable revision or fail closed while recovery is required."""
    paths = ControlPlanePaths(state_dir)
    pending = resolve_control_plane_path(state_dir, paths.pending_transaction)
    revision = resolve_control_plane_path(state_dir, paths.revision)
    if pending.is_file():
        raise ControlPlaneRecoveryRequired("control-plane transaction recovery is required")
    if not revision.is_file():
        return "legacy"
    try:
        payload = json.loads(revision.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError("control-plane revision is corrupted") from exc
    if not isinstance(payload, dict) or payload.get("schemaVersion") != REVISION_SCHEMA_VERSION:
        raise ValueError("unsupported control-plane revision")
    transaction_id = payload.get("transactionId")
    if not isinstance(transaction_id, str) or not SAFE_TRANSACTION_ID_PATTERN.fullmatch(
        transaction_id
    ):
        raise ValueError("invalid control-plane revision transaction ID")
    if pending.is_file():
        raise ControlPlaneRecoveryRequired("control-plane transaction recovery is required")
    return transaction_id
