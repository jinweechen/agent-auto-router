#!/usr/bin/env python3
"""Guarded automatic routing calibration with canary promotion and rollback."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from benchmark_priors import benchmark_priors_digest, load_benchmark_priors
from control_plane_store import (
    ControlPlaneRecoveryRequired,
    ControlPlanePaths,
    atomic_write_json,
    canonical_digest,
    commit_control_plane_transaction,
    control_plane_revision,
    recover_pending_transaction,
    resolve_control_plane_path,
    utc_now,
)
from host_permissions import HostPermissions, parse_host_permissions
from model_registry import TIER_RANK, ModelRegistry, load_model_registry, registry_digest
from model_affinity import MODEL_AFFINITY_MODES
from protocol_schemas import EXECUTION_REPORT_SCHEMA, POLICY_SHADOW_SCHEMA
from policy_learning import (
    DEFAULT_FEEDBACK_RETENTION_DAYS,
    DEFAULT_MAX_FEEDBACK_ROUTES,
    SAFE_ID_PATTERN,
    append_label_event,
    append_route_event,
    build_candidate,
    default_feedback_path,
    labeled_samples,
    load_maintained_feedback,
    maintain_feedback,
    prepare_policy_archive,
    read_feedback,
    shadow_policy_comparison,
)
from routing_policy import (
    DEFAULT_STATE_DIR,
    FEATURE_SCHEMA_VERSION,
    RoutingPolicy,
    load_active_policy,
    policy_digest,
    policy_from_dict,
    policy_to_dict,
)
from state_lock import append_lock, control_plane_lock


CONFIG_SCHEMA_VERSION = 2
STATE_SCHEMA_VERSION = 2
MODES = frozenset({"off", "observe", "guarded"})
REPORT_STATUSES = frozenset({"succeeded", "failed", "blocked", "cancelled", "timed_out"})
VERIFICATION_STATUSES = frozenset({"passed", "failed", "not-run"})
SAFE_HOST_PATTERN = re.compile(r"[A-Za-z0-9._:-]{1,80}")
DEFAULT_EXECUTION_REPORT_RETENTION_DAYS = 90
DEFAULT_MAX_EXECUTION_REPORT_MARKERS = 5000


DEFAULT_CONFIG: dict[str, Any] = {
    "schemaVersion": CONFIG_SCHEMA_VERSION,
    "mode": "observe",
    "minimumSignals": 12,
    "minimumValidationAccuracyGain": 0.05,
    "maximumThresholdStep": 1,
    "canaryPercent": 20,
    "minimumCanaryReports": 6,
    "minimumBaselineReports": 6,
    "minimumProbationReports": 12,
    "maximumFailureRateIncrease": 0.05,
}


def _config_path(state_dir: Path) -> Path:
    return ControlPlanePaths(state_dir).config


def _state_path(state_dir: Path) -> Path:
    return ControlPlanePaths(state_dir).lifecycle


def _bounded_int(value: Any, name: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be an integer between {minimum} and {maximum}")
    return value


def _bounded_rate(value: Any, name: str, minimum: float, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be numeric")
    normalized = float(value)
    if not minimum <= normalized <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return normalized


def validate_config(payload: dict[str, Any]) -> dict[str, Any]:
    if payload.get("schemaVersion") != CONFIG_SCHEMA_VERSION:
        raise ValueError("unsupported guarded-auto config schemaVersion")
    mode = str(payload.get("mode", ""))
    if mode not in MODES:
        raise ValueError("learning mode must be off, observe, or guarded")
    return {
        "schemaVersion": CONFIG_SCHEMA_VERSION,
        "mode": mode,
        "minimumSignals": _bounded_int(payload.get("minimumSignals"), "minimumSignals", 4, 10000),
        "minimumValidationAccuracyGain": _bounded_rate(
            payload.get("minimumValidationAccuracyGain"),
            "minimumValidationAccuracyGain",
            0.0,
            1.0,
        ),
        "maximumThresholdStep": _bounded_int(
            payload.get("maximumThresholdStep"), "maximumThresholdStep", 1, 1
        ),
        "canaryPercent": _bounded_int(payload.get("canaryPercent"), "canaryPercent", 1, 50),
        "minimumCanaryReports": _bounded_int(
            payload.get("minimumCanaryReports"), "minimumCanaryReports", 2, 10000
        ),
        "minimumBaselineReports": _bounded_int(
            payload.get("minimumBaselineReports"), "minimumBaselineReports", 2, 10000
        ),
        "minimumProbationReports": _bounded_int(
            payload.get("minimumProbationReports"), "minimumProbationReports", 2, 10000
        ),
        "maximumFailureRateIncrease": _bounded_rate(
            payload.get("maximumFailureRateIncrease"),
            "maximumFailureRateIncrease",
            0.0,
            0.5,
        ),
    }


def load_config(state_dir: Path) -> dict[str, Any]:
    revision_before = control_plane_revision(state_dir)
    path = resolve_control_plane_path(state_dir, _config_path(state_dir))
    if not path.is_file():
        result = dict(DEFAULT_CONFIG)
    else:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("guarded-auto config must be an object")
        result = validate_config(payload)
    if control_plane_revision(state_dir) != revision_before:
        raise ControlPlaneRecoveryRequired(
            "routing control plane changed while reading guarded-auto config"
        )
    return result


def learning_mode(state_dir: Path) -> str:
    """Return the canonical persisted-learning mode."""
    return str(load_config(state_dir)["mode"])


def feedback_recording_enabled(state_dir: Path) -> bool:
    """Return whether automatic route outcomes may be persisted locally."""
    return learning_mode(state_dir) in {"observe", "guarded"}


def _path_is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
        return True
    except ValueError:
        return False


def learning_boundary_issue(
    state_dir: Path,
    feedback_path: Path | None,
    permissions: HostPermissions,
    requested_sandbox: str = "inherit",
    model_affinity_mode: str = "off",
) -> str | None:
    """Return why routing evidence is writable by the child, or None when protected."""
    if model_affinity_mode not in MODEL_AFFINITY_MODES:
        raise ValueError("model affinity mode must be session, auto, or off")
    learning = load_config(state_dir)["mode"]
    if learning != "guarded" and model_affinity_mode != "auto":
        return None
    effective = permissions.effective_sandbox(requested_sandbox)
    if effective in {"danger-full-access", "external-sandbox"}:
        return f"adaptive routing requires protected state; child sandbox is {effective}"
    if effective == "read-only":
        return None
    protected_paths = [state_dir, feedback_path or default_feedback_path(state_dir)]
    for protected_path in protected_paths:
        for root_value in permissions.writable_roots:
            if _path_is_within(protected_path, Path(root_value)):
                return (
                    "adaptive routing state and feedback must be outside child writable roots: "
                    f"{protected_path}"
                )
    return None


def _configure_unlocked(
    state_dir: Path,
    *,
    mode: str,
    minimum_signals: int = 12,
    minimum_validation_accuracy_gain: float = 0.05,
    canary_percent: int = 20,
    minimum_canary_reports: int = 6,
    minimum_baseline_reports: int = 6,
    minimum_probation_reports: int = 12,
    maximum_failure_rate_increase: float = 0.05,
) -> dict[str, Any]:
    recover_pending_transaction(state_dir)
    payload = validate_config({
        "schemaVersion": CONFIG_SCHEMA_VERSION,
        "mode": mode,
        "minimumSignals": minimum_signals,
        "minimumValidationAccuracyGain": minimum_validation_accuracy_gain,
        "maximumThresholdStep": 1,
        "canaryPercent": canary_percent,
        "minimumCanaryReports": minimum_canary_reports,
        "minimumBaselineReports": minimum_baseline_reports,
        "minimumProbationReports": minimum_probation_reports,
        "maximumFailureRateIncrease": maximum_failure_rate_increase,
    })
    if payload["mode"] != "guarded":
        state = load_state(state_dir)
        if state.get("status") == "probation":
            _restore_snapshot(
                state_dir,
                state,
                "guarded-auto-disabled-during-probation",
                next_state=_idle_state("guarded-auto-disabled"),
                extra_writes=((_config_path(state_dir), payload),),
            )
            return payload
        elif state.get("status") == "canary":
            event = {
                "eventType": "policy_auto_cancelled",
                "recordedAt": utc_now(),
                "reason": "guarded-auto-disabled",
                "candidateId": state.get("candidateId"),
            }
            commit_control_plane_transaction(
                state_dir,
                operation="guarded-disable-canary",
                writes=(
                    (_state_path(state_dir), _idle_state("guarded-auto-disabled")),
                    (_config_path(state_dir), payload),
                ),
                audit_events=(event,),
            )
            return payload
    commit_control_plane_transaction(
        state_dir,
        operation="guarded-configure",
        writes=((_config_path(state_dir), payload),),
    )
    return payload


def configure(
    state_dir: Path,
    *,
    mode: str,
    minimum_signals: int = 12,
    minimum_validation_accuracy_gain: float = 0.05,
    canary_percent: int = 20,
    minimum_canary_reports: int = 6,
    minimum_baseline_reports: int = 6,
    minimum_probation_reports: int = 12,
    maximum_failure_rate_increase: float = 0.05,
) -> dict[str, Any]:
    with control_plane_lock(state_dir, timeout_seconds=5) as acquired:
        if not acquired:
            raise RuntimeError("routing control plane is busy")
        return _configure_unlocked(
            state_dir,
            mode=mode,
            minimum_signals=minimum_signals,
            minimum_validation_accuracy_gain=minimum_validation_accuracy_gain,
            canary_percent=canary_percent,
            minimum_canary_reports=minimum_canary_reports,
            minimum_baseline_reports=minimum_baseline_reports,
            minimum_probation_reports=minimum_probation_reports,
            maximum_failure_rate_increase=maximum_failure_rate_increase,
        )


def _idle_state(
    reason: str | None = None,
    *,
    last_evaluated_at: str | None = None,
    last_candidate_id: str | None = None,
) -> dict[str, Any]:
    state: dict[str, Any] = {
        "schemaVersion": STATE_SCHEMA_VERSION,
        "status": "idle",
        "updatedAt": utc_now(),
    }
    if reason:
        state["reason"] = reason
    if last_evaluated_at is not None:
        state["lastEvaluatedAt"] = last_evaluated_at
    if last_candidate_id:
        state["lastCandidateId"] = last_candidate_id
    return state


def load_state(state_dir: Path) -> dict[str, Any]:
    revision_before = control_plane_revision(state_dir)
    path = resolve_control_plane_path(state_dir, _state_path(state_dir))
    if not path.is_file():
        result = _idle_state()
    else:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or payload.get("schemaVersion") != STATE_SCHEMA_VERSION:
            raise ValueError("unsupported guarded-auto state")
        if payload.get("status") not in {"idle", "canary", "probation"}:
            raise ValueError("invalid guarded-auto lifecycle state")
        result = payload
    if control_plane_revision(state_dir) != revision_before:
        raise ControlPlaneRecoveryRequired(
            "routing control plane changed while reading guarded-auto state"
        )
    return result


def _timestamp(value: Any) -> datetime:
    if not isinstance(value, str):
        return datetime.min.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _sample_evidence_time(sample: dict[str, Any]) -> datetime:
    return _timestamp(sample.get("evidenceRecordedAt") or sample.get("recordedAt"))


def _latest_evidence_at(samples: Iterable[dict[str, Any]]) -> str | None:
    values = [_sample_evidence_time(sample) for sample in samples]
    if not values:
        return None
    return max(values).astimezone(timezone.utc).isoformat()


def _event_after(event: dict[str, Any], started_at: Any) -> bool:
    return _timestamp(event.get("recordedAt")) >= _timestamp(started_at)


def _verified_stats(
    events: Iterable[dict[str, Any]], policy_digest_value: str, started_at: Any
) -> dict[str, Any]:
    verified = [
        event
        for event in events
        if event.get("eventType") == "route_outcome"
        and event.get("featureSchemaVersion") == FEATURE_SCHEMA_VERSION
        and event.get("policyDigest") == policy_digest_value
        and _event_after(event, started_at)
        and event.get("validationConfigured") is True
        and isinstance(event.get("validationPassed"), bool)
        and not event.get("explicitOverride")
        and not bool(event.get("features", {}).get("high_risk"))
    ]
    failed = sum(event.get("validationPassed") is False for event in verified)
    total = len(verified)
    return {
        "reports": total,
        "passed": total - failed,
        "failed": failed,
        "failureRate": (failed / total) if total else None,
    }


def inferred_samples(
    events: Iterable[dict[str, Any]], registry: ModelRegistry | None = None
) -> list[dict[str, Any]]:
    active_registry = registry or load_model_registry()
    samples: list[dict[str, Any]] = []
    for event in events:
        if event.get("eventType") != "route_outcome":
            continue
        if event.get("featureSchemaVersion") != FEATURE_SCHEMA_VERSION:
            continue
        features = event.get("features") if isinstance(event.get("features"), dict) else {}
        if event.get("explicitOverride") or bool(features.get("high_risk")):
            continue
        if not (
            event.get("escalated") is True
            and event.get("validationConfigured") is True
            and event.get("validationPassed") is True
            and int(event.get("attemptCount", 1)) >= 2
        ):
            continue
        try:
            selector = active_registry.get(str(event.get("selectorModel")), role="direct")
            selected = active_registry.get(str(event.get("selectedModel")), role="direct")
        except ValueError:
            continue
        if not selector.auto_eligible or not selected.auto_eligible:
            continue
        if TIER_RANK[selected.tier] != TIER_RANK[selector.tier] + 1:
            continue
        sample = dict(event)
        sample["preferredModel"] = selected.model_id
        sample["preferredTier"] = selected.tier
        sample["outcome"] = "pass"
        sample["labelSource"] = "verified-tier-escalation"
        samples.append(sample)
    return samples


def learning_samples(
    events: Iterable[dict[str, Any]], registry: ModelRegistry | None = None
) -> list[dict[str, Any]]:
    active_registry = registry or load_model_registry()
    event_list = list(events)
    inferred = {str(sample["routeId"]): sample for sample in inferred_samples(event_list, active_registry)}
    human = {str(sample["routeId"]): sample for sample in labeled_samples(event_list, active_registry)}
    inferred.update(human)
    return [inferred[key] for key in sorted(inferred)]


def _policy_changed(base: RoutingPolicy, candidate: RoutingPolicy) -> bool:
    return policy_digest(base) != policy_digest(candidate)


def _candidate_policy(candidate: dict[str, Any]) -> RoutingPolicy:
    policy = candidate.get("policy")
    if not isinstance(policy, dict):
        raise ValueError("guarded candidate policy is missing")
    return policy_from_dict(policy)


def _candidate_is_intact(candidate: dict[str, Any]) -> bool:
    candidate_id = candidate.get("candidateId")
    unsigned = dict(candidate)
    unsigned.pop("candidateId", None)
    return isinstance(candidate_id, str) and candidate_id == canonical_digest(unsigned)


def _write_guarded_candidate(
    state_dir: Path,
    samples: list[dict[str, Any]],
    base_policy: RoutingPolicy,
    config: dict[str, Any],
    registry: ModelRegistry,
) -> tuple[dict[str, Any], Path]:
    candidate = build_candidate(
        samples,
        base_policy,
        min_labels=int(config["minimumSignals"]),
        min_validation_accuracy_gain=float(config["minimumValidationAccuracyGain"]),
        max_threshold_step=int(config["maximumThresholdStep"]),
        conservative_only=True,
        requires_human_approval=False,
        registry=registry,
    )
    policy = _candidate_policy(candidate)
    candidate["activationMode"] = "guarded-auto"
    candidate["eligibleForAutoCanary"] = bool(
        candidate.get("eligibleForApproval") and _policy_changed(base_policy, policy)
    )
    candidate["evidence"] = {
        "humanLabels": sum(sample.get("labelSource") != "verified-tier-escalation" for sample in samples),
        "verifiedTierEscalations": sum(
            sample.get("labelSource") == "verified-tier-escalation" for sample in samples
        ),
        "storesTaskText": False,
    }
    candidate.pop("candidateId", None)
    candidate["candidateId"] = canonical_digest(candidate)
    path = state_dir / "candidates" / f"guarded-{candidate['candidateId'][:16]}.json"
    return candidate, path


def _start_canary(
    state_dir: Path,
    base_policy: RoutingPolicy,
    candidate: dict[str, Any],
    candidate_path: Path,
    config: dict[str, Any],
) -> dict[str, Any]:
    candidate_policy = _candidate_policy(candidate)
    started_at = utc_now()
    state = {
        "schemaVersion": STATE_SCHEMA_VERSION,
        "status": "canary",
        "updatedAt": started_at,
        "startedAt": started_at,
        "candidateId": candidate["candidateId"],
        "candidatePath": str(candidate_path.resolve()),
        "candidatePolicy": policy_to_dict(candidate_policy),
        "candidatePolicyDigest": policy_digest(candidate_policy),
        "basePolicyDigest": policy_digest(base_policy),
        "canaryPercent": int(config["canaryPercent"]),
    }
    audit = {
        "eventType": "policy_auto_canary_started",
        "recordedAt": started_at,
        "candidateId": candidate["candidateId"],
        "basePolicyDigest": state["basePolicyDigest"],
        "candidatePolicyDigest": state["candidatePolicyDigest"],
        "canaryPercent": state["canaryPercent"],
    }
    commit_control_plane_transaction(
        state_dir,
        operation="guarded-canary-start",
        writes=((candidate_path, candidate), (_state_path(state_dir), state)),
        audit_events=(audit,),
    )
    return state


def _load_state_candidate(state_dir: Path, state: dict[str, Any]) -> dict[str, Any]:
    candidate_root = resolve_control_plane_path(
        state_dir, ControlPlanePaths(state_dir).candidates
    )
    path = resolve_control_plane_path(
        state_dir, Path(str(state.get("candidatePath", "")))
    )
    if path.parent != candidate_root or not path.is_file():
        raise ValueError("guarded-auto candidate path is outside the candidate directory")
    candidate = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(candidate, dict) or not _candidate_is_intact(candidate):
        raise ValueError("guarded-auto candidate integrity check failed")
    if candidate.get("candidateId") != state.get("candidateId"):
        raise ValueError("guarded-auto candidate identity changed")
    if candidate.get("featureSchemaVersion") != FEATURE_SCHEMA_VERSION:
        raise ValueError(
            "guarded-auto candidate is stale because the routing feature schema changed"
        )
    return candidate


def _activate_candidate(
    state_dir: Path,
    state: dict[str, Any],
    candidate: dict[str, Any],
    base_policy: RoutingPolicy,
    baseline_stats: dict[str, Any],
) -> dict[str, Any]:
    candidate_policy = _candidate_policy(candidate)
    archived, archived_payload = prepare_policy_archive(
        state_dir, base_policy, "guarded-auto-promotion"
    )
    started_at = utc_now()
    active_payload = policy_to_dict(candidate_policy)
    active_payload["activation"] = {
        "activatedAt": started_at,
        "activationMode": "guarded-auto",
        "candidateId": candidate["candidateId"],
    }
    next_state = {
        "schemaVersion": STATE_SCHEMA_VERSION,
        "status": "probation",
        "updatedAt": started_at,
        "startedAt": started_at,
        "candidateId": candidate["candidateId"],
        "candidatePath": state["candidatePath"],
        "candidatePolicyDigest": policy_digest(candidate_policy),
        "basePolicyDigest": policy_digest(base_policy),
        "basePolicySnapshot": str(archived.resolve()),
        "baselineFailureRate": baseline_stats.get("failureRate"),
    }
    audit = {
        "eventType": "policy_auto_promoted",
        "recordedAt": started_at,
        "candidateId": candidate["candidateId"],
        "fromDigest": policy_digest(base_policy),
        "toDigest": policy_digest(candidate_policy),
        "rollbackSnapshot": str(archived.resolve()),
    }
    commit_control_plane_transaction(
        state_dir,
        operation="guarded-promotion",
        writes=(
            (archived, archived_payload),
            (ControlPlanePaths(state_dir).active_policy, active_payload),
            (_state_path(state_dir), next_state),
        ),
        audit_events=(audit,),
    )
    return next_state


def _restore_snapshot(
    state_dir: Path,
    state: dict[str, Any],
    reason: str,
    *,
    next_state: dict[str, Any] | None = None,
    extra_writes: Iterable[tuple[Path, dict[str, Any]]] = (),
) -> dict[str, Any]:
    recover_pending_transaction(state_dir)
    paths = ControlPlanePaths(state_dir)
    current, _ = load_active_policy(state_dir)
    history_root = resolve_control_plane_path(state_dir, paths.history)
    snapshot_path = resolve_control_plane_path(
        state_dir, Path(str(state.get("basePolicySnapshot", "")))
    )
    if snapshot_path.parent != history_root or not snapshot_path.is_file():
        raise ValueError("guarded-auto rollback snapshot is outside the policy history")
    snapshot_payload = json.loads(snapshot_path.read_text(encoding="utf-8"))
    if not isinstance(snapshot_payload, dict):
        raise ValueError("guarded-auto rollback snapshot is invalid")
    previous = policy_from_dict(snapshot_payload)
    archived, archived_payload = prepare_policy_archive(
        state_dir, current, "guarded-auto-rollback"
    )
    recorded_at = utc_now()
    active_payload = policy_to_dict(previous)
    active_payload["activation"] = {
        "activatedAt": recorded_at,
        "activationMode": "guarded-auto-rollback",
        "candidateId": state.get("candidateId"),
        "reason": reason,
    }
    event = {
        "eventType": "policy_auto_rolled_back",
        "recordedAt": recorded_at,
        "candidateId": state.get("candidateId"),
        "fromDigest": policy_digest(current),
        "toDigest": policy_digest(previous),
        "reason": reason,
    }
    writes: list[tuple[Path, dict[str, Any]]] = [
        (archived, archived_payload),
        (paths.active_policy, active_payload),
        (_state_path(state_dir), next_state or _idle_state(reason)),
    ]
    writes.extend(extra_writes)
    transaction_id = commit_control_plane_transaction(
        state_dir,
        operation="guarded-rollback",
        writes=writes,
        audit_events=(event,),
    )
    event["transactionId"] = transaction_id
    return event


def _cycle_unlocked(
    state_dir: Path,
    feedback_path: Path,
    *,
    dry_run: bool,
    registry: ModelRegistry,
    config: dict[str, Any],
    events: list[dict[str, Any]],
) -> dict[str, Any]:
    state = load_state(state_dir)
    if config["mode"] != "guarded":
        return {"status": config["mode"], "action": "none", "modelCalls": 0}
    active_policy, active_source = load_active_policy(state_dir)

    if state["status"] == "idle":
        samples = learning_samples(events, registry)
        if len(samples) < int(config["minimumSignals"]):
            return {
                "status": "waiting-for-signals",
                "action": "none",
                "signals": len(samples),
                "minimumSignals": int(config["minimumSignals"]),
                "modelCalls": 0,
            }
        last_evaluated_at = state.get("lastEvaluatedAt")
        reevaluation_interval = max(4, int(config["minimumSignals"]) // 4)
        new_signal_count = (
            sum(
                _sample_evidence_time(sample) > _timestamp(last_evaluated_at)
                for sample in samples
            )
            if last_evaluated_at
            else len(samples)
        )
        if last_evaluated_at and new_signal_count < reevaluation_interval:
            return {
                "status": "waiting-for-new-signals",
                "action": "none",
                "signals": len(samples),
                "newSignals": new_signal_count,
                "minimumNewSignals": reevaluation_interval,
                "lastEvaluatedAt": last_evaluated_at,
                "modelCalls": 0,
            }
        candidate, path = _write_guarded_candidate(
            state_dir,
            samples,
            active_policy,
            config,
            registry,
        )
        if not candidate.get("eligibleForAutoCanary"):
            if not dry_run:
                rejected_state = _idle_state(
                    "candidate-rejected",
                    last_evaluated_at=_latest_evidence_at(samples),
                    last_candidate_id=str(candidate["candidateId"]),
                )
                audit = {
                    "eventType": "policy_auto_candidate_rejected",
                    "recordedAt": utc_now(),
                    "candidateId": candidate["candidateId"],
                    "signals": len(samples),
                }
                commit_control_plane_transaction(
                    state_dir,
                    operation="guarded-candidate-reject",
                    writes=((path, candidate), (_state_path(state_dir), rejected_state)),
                    audit_events=(audit,),
                )
            return {
                "status": "candidate-rejected",
                "action": "would-reject" if dry_run else "rejected",
                "candidateId": candidate["candidateId"],
                "candidatePath": str(path),
                "eligibleForAutoCanary": False,
                "modelCalls": 0,
            }
        if dry_run:
            return {
                "status": "dry-run",
                "action": "would-start-canary",
                "candidateId": candidate["candidateId"],
                "candidatePath": str(path),
                "modelCalls": 0,
            }
        next_state = _start_canary(state_dir, active_policy, candidate, path, config)
        return {"status": "canary", "action": "started", "state": next_state, "modelCalls": 0}

    candidate = _load_state_candidate(state_dir, state)
    priors = load_benchmark_priors(registry=registry)
    if candidate.get("modelRegistryDigest") != registry_digest(registry):
        raise ValueError("guarded-auto candidate is stale because the registry changed")
    if candidate.get("benchmarkPriorsDigest") != benchmark_priors_digest(priors):
        raise ValueError("guarded-auto candidate is stale because benchmark priors changed")

    if state["status"] == "canary":
        if policy_digest(active_policy) != state.get("basePolicyDigest"):
            raise ValueError("active policy changed during guarded-auto canary")
        baseline = _verified_stats(events, str(state["basePolicyDigest"]), state["startedAt"])
        canary = _verified_stats(events, str(state["candidatePolicyDigest"]), state["startedAt"])
        ready = (
            canary["reports"] >= int(config["minimumCanaryReports"])
            and baseline["reports"] >= int(config["minimumBaselineReports"])
        )
        summary = {"baseline": baseline, "canary": canary}
        if not ready:
            return {"status": "canary", "action": "collecting", "evaluation": summary, "modelCalls": 0}
        regression = (
            canary["failed"] >= 2
            and float(canary["failureRate"]) > float(baseline["failureRate"]) + float(config["maximumFailureRateIncrease"])
        )
        if regression:
            event = {
                "eventType": "policy_auto_canary_rejected",
                "recordedAt": utc_now(),
                "candidateId": candidate["candidateId"],
                "reason": "verified-failure-rate-regression",
                "evaluation": summary,
            }
            if not dry_run:
                commit_control_plane_transaction(
                    state_dir,
                    operation="guarded-canary-reject",
                    writes=((
                        _state_path(state_dir),
                        _idle_state(
                            event["reason"],
                            last_evaluated_at=_latest_evidence_at(
                                learning_samples(events, registry)
                            ),
                            last_candidate_id=str(candidate["candidateId"]),
                        ),
                    ),),
                    audit_events=(event,),
                )
            return {"status": "canary-rejected", "action": "would-reject" if dry_run else "rejected", "evaluation": summary, "modelCalls": 0}
        if dry_run:
            return {"status": "dry-run", "action": "would-promote", "evaluation": summary, "modelCalls": 0}
        next_state = _activate_candidate(state_dir, state, candidate, active_policy, baseline)
        return {"status": "probation", "action": "promoted", "state": next_state, "evaluation": summary, "modelCalls": 0}

    if policy_digest(active_policy) != state.get("candidatePolicyDigest"):
        raise ValueError("active policy changed during guarded-auto probation")
    probation = _verified_stats(events, str(state["candidatePolicyDigest"]), state["startedAt"])
    if probation["reports"] < int(config["minimumProbationReports"]):
        return {"status": "probation", "action": "collecting", "evaluation": probation, "modelCalls": 0}
    baseline_rate = state.get("baselineFailureRate")
    regression = (
        isinstance(baseline_rate, (int, float))
        and probation["failed"] >= 2
        and float(probation["failureRate"]) > float(baseline_rate) + float(config["maximumFailureRateIncrease"])
    )
    if regression:
        if dry_run:
            return {"status": "dry-run", "action": "would-rollback", "evaluation": probation, "modelCalls": 0}
        event = _restore_snapshot(
            state_dir,
            state,
            "probation-failure-rate-regression",
            next_state=_idle_state(
                "probation-failure-rate-regression",
                last_evaluated_at=_latest_evidence_at(
                    learning_samples(events, registry)
                ),
                last_candidate_id=str(candidate["candidateId"]),
            ),
        )
        return {"status": "rolled-back", "action": "rolled-back", "event": event, "evaluation": probation, "modelCalls": 0}
    event = {
        "eventType": "policy_auto_stabilized",
        "recordedAt": utc_now(),
        "candidateId": candidate["candidateId"],
        "policyDigest": state["candidatePolicyDigest"],
        "evaluation": probation,
    }
    if not dry_run:
        commit_control_plane_transaction(
            state_dir,
            operation="guarded-stabilize",
            writes=((
                _state_path(state_dir),
                _idle_state(
                    "probation-passed",
                    last_evaluated_at=_latest_evidence_at(
                        learning_samples(events, registry)
                    ),
                    last_candidate_id=str(candidate["candidateId"]),
                ),
            ),),
            audit_events=(event,),
        )
    return {"status": "stable", "action": "would-stabilize" if dry_run else "stabilized", "evaluation": probation, "modelCalls": 0}


def run_cycle(
    state_dir: Path,
    feedback_path: Path | None = None,
    *,
    dry_run: bool = False,
    registry: ModelRegistry | None = None,
) -> dict[str, Any]:
    active_registry = registry or load_model_registry()
    with control_plane_lock(state_dir) as acquired:
        if not acquired:
            return {"status": "busy", "action": "none", "modelCalls": 0}
        recover_pending_transaction(state_dir)
        config = load_config(state_dir)
        events, maintenance = load_maintained_feedback(
            feedback_path or default_feedback_path(state_dir),
            maximum_routes=DEFAULT_MAX_FEEDBACK_ROUTES,
            retention_days=DEFAULT_FEEDBACK_RETENTION_DAYS,
            apply=not dry_run and config["mode"] != "off",
        )
        result = _cycle_unlocked(
            state_dir,
            feedback_path or default_feedback_path(state_dir),
            dry_run=dry_run,
            registry=active_registry,
            config=config,
            events=events,
        )
        result["feedbackMaintenance"] = maintenance
        return result


def process_recorded_outcome(
    state_dir: Path,
    feedback_path: Path | None = None,
    *,
    registry: ModelRegistry | None = None,
) -> dict[str, Any]:
    try:
        return run_cycle(state_dir, feedback_path, registry=registry)
    except Exception as exc:
        return {
            "status": "error",
            "action": "none",
            "errorType": type(exc).__name__,
            "modelCalls": 0,
        }


def _execution_report_to_route_payload(
    payload: dict[str, Any], registry: ModelRegistry
) -> tuple[str, str, dict[str, Any], str | None, str | None]:
    if payload.get("schema") != EXECUTION_REPORT_SCHEMA:
        raise ValueError("unsupported execution report schema")
    report_id = str(payload.get("reportId", ""))
    if not SAFE_ID_PATTERN.fullmatch(report_id):
        raise ValueError("invalid execution report ID")
    host = str(payload.get("host", ""))
    if not SAFE_HOST_PATTERN.fullmatch(host):
        raise ValueError("invalid execution report host")
    route = payload.get("route")
    result = payload.get("result")
    if not isinstance(route, dict) or not isinstance(result, dict):
        raise ValueError("execution report route and result must be objects")
    allowed_route = {
        "routeId", "strategy", "effort", "selectorModel", "selectedModel", "targetTier",
        "reason", "features", "policyVersion", "policyDigest", "modelRegistryDigest",
        "featureSchemaVersion", "explicitOverride", "workspaceKey", "topology", "variant",
        "roleModelPolicy", "estimatedRoleTierSwitches",
    }
    allowed_result = {
        "status", "durationMs", "verification", "validationConfigured", "escalated",
        "attemptCount", "observedTokens", "userPreferredModel", "preferenceSource",
    }
    if set(route) - allowed_route:
        raise ValueError("execution report route contains unsupported fields")
    if set(result) - allowed_result:
        raise ValueError("execution report result contains unsupported fields")
    status = str(result.get("status", ""))
    verification = str(result.get("verification", ""))
    if status not in REPORT_STATUSES:
        raise ValueError("invalid execution report status")
    if verification not in VERIFICATION_STATUSES:
        raise ValueError("invalid execution report verification status")
    validation_configured = bool(result.get("validationConfigured", False))
    if verification != "not-run" and not validation_configured:
        raise ValueError("verification result requires validationConfigured=true")
    observed = result.get("observedTokens")
    route_payload = {
        "route_id": route.get("routeId"),
        "strategy": route.get("strategy"),
        "effort": route.get("effort"),
        "selector_model": route.get("selectorModel"),
        "selected_model": route.get("selectedModel"),
        "target_tier": route.get("targetTier"),
        "reason": route.get("reason"),
        "features": route.get("features"),
        "policy_version": route.get("policyVersion"),
        "policy_digest": route.get("policyDigest"),
        "registry_digest": route.get("modelRegistryDigest"),
        "feature_schema_version": route.get("featureSchemaVersion", 1),
        "explicit_override": bool(route.get("explicitOverride", False)),
        "workspace_key": route.get("workspaceKey"),
        "topology": route.get("topology"),
        "variant": route.get("variant"),
        "role_model_policy": route.get("roleModelPolicy"),
        "estimated_role_tier_switches": route.get("estimatedRoleTierSwitches", 0),
        "exit_code": 0 if status == "succeeded" else 1,
        "duration_ms": result.get("durationMs", 0),
        "observed_tokens": observed,
        "validation_configured": validation_configured,
        "validation_passed": True if verification == "passed" else False if verification == "failed" else None,
        "escalated": bool(result.get("escalated", False)),
        "attempt_count": result.get("attemptCount", 1),
    }
    preferred = result.get("userPreferredModel")
    preference_source = result.get("preferenceSource")
    if preferred is not None:
        if preference_source != "explicit-user-selection":
            raise ValueError("userPreferredModel requires explicit-user-selection source")
        registry.get(str(preferred), role="direct")
    return report_id, host, route_payload, str(preferred) if preferred is not None else None, status


def _execution_report_lock_path(reports: Path) -> Path:
    return reports / "execution-reports"


def _execution_report_operation_lock_path(reports: Path, report_id: str) -> Path:
    digest = hashlib.sha256(report_id.encode("utf-8")).hexdigest()
    return reports / "operations" / digest


def _strict_marker_timestamp(value: Any, field: str, path: Path) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"execution report marker {field} is missing: {path.name}")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(
            f"execution report marker {field} is invalid: {path.name}"
        ) from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _validate_execution_report_marker(path: Path, payload: Any) -> dict[str, Any]:
    allowed = {
        "schema", "reportId", "recordedAt", "completedAt", "host", "routeId",
        "state", "storesTaskText", "errorType", "labelExpected", "routeRecorded",
        "labelRecorded", "cycleProcessed", "cycleDeferred", "resolvedAt", "resolvedBy",
        "resolution",
    }
    if not isinstance(payload, dict) or set(payload) - allowed:
        raise ValueError(f"invalid execution report marker: {path.name}")
    if payload.get("schema") != EXECUTION_REPORT_SCHEMA:
        raise ValueError(f"unsupported execution report marker: {path.name}")
    if not SAFE_ID_PATTERN.fullmatch(str(payload.get("reportId", ""))):
        raise ValueError(f"invalid execution report marker ID: {path.name}")
    expected_name = (
        hashlib.sha256(str(payload["reportId"]).encode("utf-8")).hexdigest() + ".json"
    )
    if path.name != expected_name:
        raise ValueError(f"execution report marker filename is invalid: {path.name}")
    if not SAFE_ID_PATTERN.fullmatch(str(payload.get("routeId", ""))):
        raise ValueError(f"invalid execution report marker route ID: {path.name}")
    if not SAFE_HOST_PATTERN.fullmatch(str(payload.get("host", ""))):
        raise ValueError(f"invalid execution report marker host: {path.name}")
    if payload.get("state") not in {"pending", "recorded", "incomplete"}:
        raise ValueError(f"invalid execution report marker state: {path.name}")
    if payload.get("storesTaskText") is not False:
        raise ValueError(f"execution report marker privacy flag is invalid: {path.name}")
    _strict_marker_timestamp(payload.get("recordedAt"), "recordedAt", path)
    if payload.get("completedAt") is not None:
        _strict_marker_timestamp(payload.get("completedAt"), "completedAt", path)
    for field in (
        "labelExpected", "routeRecorded", "labelRecorded", "cycleProcessed", "cycleDeferred"
    ):
        if not isinstance(payload.get(field), bool):
            raise ValueError(f"execution report marker {field} is invalid: {path.name}")
    if payload["state"] == "recorded":
        if payload.get("completedAt") is None:
            raise ValueError(f"execution report marker completedAt is missing: {path.name}")
        if not payload["routeRecorded"]:
            raise ValueError(f"recorded execution report marker is incomplete: {path.name}")
        if payload["cycleProcessed"] == payload["cycleDeferred"]:
            raise ValueError(f"recorded execution report marker cycle state is invalid: {path.name}")
        if payload["labelExpected"] and not payload["labelRecorded"]:
            raise ValueError(f"recorded execution report marker label is incomplete: {path.name}")
    resolution_fields = ("resolvedAt", "resolvedBy", "resolution")
    present_resolution_fields = [field for field in resolution_fields if payload.get(field) is not None]
    if present_resolution_fields and len(present_resolution_fields) != len(resolution_fields):
        raise ValueError(f"execution report marker resolution is incomplete: {path.name}")
    if present_resolution_fields:
        _strict_marker_timestamp(payload["resolvedAt"], "resolvedAt", path)
        if not SAFE_HOST_PATTERN.fullmatch(str(payload["resolvedBy"])):
            raise ValueError(f"execution report marker resolver is invalid: {path.name}")
        if payload["resolution"] != "acknowledged-recorded":
            raise ValueError(f"execution report marker resolution is invalid: {path.name}")
    return payload


def _execution_report_storage_unlocked(
    reports: Path,
    *,
    maximum_markers: int,
    retention_days: int,
    apply: bool,
    now: datetime | None = None,
) -> dict[str, Any]:
    marker_paths = sorted(reports.glob("*.json")) if reports.is_dir() else []
    markers: list[tuple[Path, dict[str, Any], datetime]] = []
    for path in marker_paths:
        payload = _validate_execution_report_marker(
            path, json.loads(path.read_text(encoding="utf-8"))
        )
        marker_time = _strict_marker_timestamp(
            payload.get("completedAt") or payload.get("recordedAt"),
            "completedAt" if payload.get("completedAt") else "recordedAt",
            path,
        ).astimezone(timezone.utc)
        markers.append((path, payload, marker_time))
    cutoff = (now or datetime.now(timezone.utc)).astimezone(timezone.utc) - timedelta(
        days=retention_days
    )
    protected = [item for item in markers if item[1]["state"] != "recorded"]
    recorded = [item for item in markers if item[1]["state"] == "recorded"]
    age_eligible = [item for item in recorded if item[2] >= cutoff]
    retained_recorded = sorted(
        age_eligible,
        key=lambda item: (item[2], item[1]["reportId"], item[0].name),
    )[-maximum_markers:]
    retained_paths = {item[0] for item in protected + retained_recorded}
    removed = [item for item in markers if item[0] not in retained_paths]
    if apply:
        for path, _, _ in removed:
            path.unlink()
    return {
        "path": str(reports),
        "exists": reports.is_dir(),
        "maximumRecordedMarkers": maximum_markers,
        "retentionDays": retention_days,
        "beforeMarkers": len(markers),
        "afterMarkers": len(markers) - len(removed),
        "recordedMarkers": len(recorded),
        "pendingMarkers": sum(item[1]["state"] == "pending" for item in protected),
        "incompleteMarkers": sum(item[1]["state"] == "incomplete" for item in protected),
        "nonterminalMarkersRequireReview": bool(protected),
        "markersRemovedByAge": len(recorded) - len(age_eligible),
        "markersRemovedByLimit": max(0, len(age_eligible) - len(retained_recorded)),
        "wouldChange": bool(removed),
        "applied": bool(apply and removed),
        "idempotencyWindowBounded": True,
        "storesTaskText": False,
        "modelCalls": 0,
    }


def maintain_execution_reports(
    state_dir: Path,
    *,
    maximum_markers: int = DEFAULT_MAX_EXECUTION_REPORT_MARKERS,
    retention_days: int = DEFAULT_EXECUTION_REPORT_RETENTION_DAYS,
    apply: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Inspect or prune completed execution-report idempotency markers."""
    if isinstance(maximum_markers, bool) or not 1 <= maximum_markers <= 100000:
        raise ValueError("maximum execution report markers must be between 1 and 100000")
    if isinstance(retention_days, bool) or not 1 <= retention_days <= 3650:
        raise ValueError("execution report retention days must be between 1 and 3650")
    reports = resolve_control_plane_path(state_dir, ControlPlanePaths(state_dir).reports)
    if not reports.is_dir():
        return _execution_report_storage_unlocked(
            reports,
            maximum_markers=maximum_markers,
            retention_days=retention_days,
            apply=False,
            now=now,
        )
    with append_lock(_execution_report_lock_path(reports)) as acquired:
        if not acquired:
            raise RuntimeError("execution report marker store is busy")
        return _execution_report_storage_unlocked(
            reports,
            maximum_markers=maximum_markers,
            retention_days=retention_days,
            apply=apply,
            now=now,
        )


def _write_execution_report_marker(reports: Path, marker: Path, payload: dict[str, Any]) -> None:
    with append_lock(_execution_report_lock_path(reports)) as acquired:
        if not acquired:
            raise RuntimeError("execution report marker store is busy")
        atomic_write_json(marker, payload)


def _ingest_execution_report_locked(
    *,
    report_id: str,
    host: str,
    route_payload: dict[str, Any],
    preferred: str | None,
    status: str,
    state_dir: Path,
    target_feedback: Path,
    reports: Path,
    active_registry: ModelRegistry,
) -> dict[str, Any]:
    marker = reports / f"{hashlib.sha256(report_id.encode('utf-8')).hexdigest()}.json"
    normalized_marker = {
        "schema": EXECUTION_REPORT_SCHEMA,
        "reportId": report_id,
        "recordedAt": utc_now(),
        "host": host,
        "routeId": route_payload["route_id"],
        "state": "pending",
        "storesTaskText": False,
        "labelExpected": preferred is not None,
        "routeRecorded": False,
        "labelRecorded": False,
        "cycleProcessed": False,
        "cycleDeferred": False,
    }
    with append_lock(_execution_report_lock_path(reports)) as acquired:
        if not acquired:
            raise RuntimeError("execution report marker store is busy")
        try:
            descriptor = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            existing = _validate_execution_report_marker(
                marker, json.loads(marker.read_text(encoding="utf-8"))
            )
            if existing.get("reportId") != report_id:
                raise ValueError("execution report marker collision")
            if existing.get("state") != "recorded":
                raise ValueError("execution report has an incomplete prior recording")
            return {"status": "duplicate", "reportId": report_id, "modelCalls": 0}
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as marker_stream:
                marker_stream.write(json.dumps(normalized_marker, ensure_ascii=True) + "\n")
                marker_stream.flush()
                os.fsync(marker_stream.fileno())
        except Exception:
            try:
                marker.unlink()
            except FileNotFoundError:
                pass
            raise
    route_recorded = False
    try:
        event = append_route_event(route_payload, target_feedback, active_registry)
        route_recorded = True
        normalized_marker["routeRecorded"] = True
        _write_execution_report_marker(reports, marker, normalized_marker)
        if preferred is not None:
            append_label_event(
                str(route_payload["route_id"]),
                preferred,
                "pass" if status == "succeeded" else "fail",
                target_feedback,
                active_registry,
            )
            normalized_marker["labelRecorded"] = True
            _write_execution_report_marker(reports, marker, normalized_marker)
        cycle = process_recorded_outcome(state_dir, target_feedback, registry=active_registry)
        normalized_marker["cycleProcessed"] = True
        normalized_marker["state"] = "recorded"
        normalized_marker["completedAt"] = utc_now()
        with append_lock(_execution_report_lock_path(reports)) as acquired:
            if not acquired:
                raise RuntimeError("execution report marker store is busy")
            atomic_write_json(marker, normalized_marker)
            try:
                report_storage = _execution_report_storage_unlocked(
                    reports,
                    maximum_markers=DEFAULT_MAX_EXECUTION_REPORT_MARKERS,
                    retention_days=DEFAULT_EXECUTION_REPORT_RETENTION_DAYS,
                    apply=True,
                )
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                report_storage = {
                    "status": "error",
                    "errorType": type(exc).__name__,
                    "storesTaskText": False,
                    "modelCalls": 0,
                }
        return {
            "status": "recorded",
            "reportId": report_id,
            "event": event,
            "guardedAuto": cycle,
            "executionReportStorage": report_storage,
            "modelCalls": 0,
        }
    except Exception as exc:
        with append_lock(_execution_report_lock_path(reports)) as acquired:
            if acquired:
                if route_recorded:
                    normalized_marker["state"] = "incomplete"
                    normalized_marker["errorType"] = type(exc).__name__
                    atomic_write_json(marker, normalized_marker)
                else:
                    try:
                        marker.unlink()
                    except FileNotFoundError:
                        pass
        raise


def ingest_execution_report(
    payload: dict[str, Any],
    state_dir: Path,
    feedback_path: Path | None = None,
    registry: ModelRegistry | None = None,
) -> dict[str, Any]:
    active_registry = registry or load_model_registry()
    report_id, host, route_payload, preferred, status = _execution_report_to_route_payload(
        payload, active_registry
    )
    mode = learning_mode(state_dir)
    if mode == "off":
        return {
            "status": "ignored",
            "reason": "feedback-disabled",
            "reportId": report_id,
            "learningMode": mode,
            "modelCalls": 0,
        }
    reports = resolve_control_plane_path(state_dir, ControlPlanePaths(state_dir).reports)
    reports.mkdir(parents=True, exist_ok=True)
    with append_lock(_execution_report_operation_lock_path(reports, report_id)) as acquired:
        if not acquired:
            raise RuntimeError("execution report is busy")
        return _ingest_execution_report_locked(
            report_id=report_id,
            host=host,
            route_payload=route_payload,
            preferred=preferred,
            status=status,
            state_dir=state_dir,
            target_feedback=feedback_path or default_feedback_path(state_dir),
            reports=reports,
            active_registry=active_registry,
        )


def maintain_feedback_state(
    state_dir: Path,
    feedback_path: Path | None = None,
    *,
    maximum_routes: int = DEFAULT_MAX_FEEDBACK_ROUTES,
    retention_days: int = DEFAULT_FEEDBACK_RETENTION_DAYS,
    apply: bool = False,
) -> dict[str, Any]:
    """Inspect or compact privacy-minimized feedback under control-plane locks."""
    with control_plane_lock(state_dir, timeout_seconds=5) as acquired:
        if not acquired:
            raise RuntimeError("routing control plane is busy")
        recover_pending_transaction(state_dir)
        result = maintain_feedback(
            feedback_path or default_feedback_path(state_dir),
            maximum_routes=maximum_routes,
            retention_days=retention_days,
            apply=apply,
        )
        result["operation"] = "applied" if result["applied"] else "inspection"
        return result


def maintain_execution_report_state(
    state_dir: Path,
    *,
    maximum_markers: int = DEFAULT_MAX_EXECUTION_REPORT_MARKERS,
    retention_days: int = DEFAULT_EXECUTION_REPORT_RETENTION_DAYS,
    apply: bool = False,
) -> dict[str, Any]:
    """Inspect or prune completed idempotency markers under control-plane locks."""
    with control_plane_lock(state_dir, timeout_seconds=5) as acquired:
        if not acquired:
            raise RuntimeError("routing control plane is busy")
        recover_pending_transaction(state_dir)
        result = maintain_execution_reports(
            state_dir,
            maximum_markers=maximum_markers,
            retention_days=retention_days,
            apply=apply,
        )
        result["operation"] = "applied" if result["applied"] else "inspection"
        return result


def recover_execution_report(
    state_dir: Path,
    report_id: str,
    feedback_path: Path | None = None,
    *,
    action: str = "inspect",
    confirm_report_id: str | None = None,
    resolved_by: str | None = None,
) -> dict[str, Any]:
    """Inspect or explicitly reconcile one nonterminal execution-report marker."""
    if not SAFE_ID_PATTERN.fullmatch(report_id):
        raise ValueError("invalid execution report ID")
    if action not in {"inspect", "release-for-retry", "acknowledge-recorded"}:
        raise ValueError("unsupported execution report recovery action")
    if action != "inspect":
        if confirm_report_id != report_id:
            raise ValueError("report recovery requires an exact --confirm-report-id match")
        if resolved_by is None or not SAFE_HOST_PATTERN.fullmatch(resolved_by):
            raise ValueError("report recovery requires a safe --resolved-by value")
    reports = resolve_control_plane_path(state_dir, ControlPlanePaths(state_dir).reports)
    marker_name = hashlib.sha256(report_id.encode("utf-8")).hexdigest() + ".json"
    marker = reports / marker_name
    resolved_dir = resolve_control_plane_path(state_dir, reports / "resolved")
    archive = resolved_dir / marker_name
    reports.mkdir(parents=True, exist_ok=True)
    with append_lock(_execution_report_operation_lock_path(reports, report_id)) as acquired:
        if not acquired:
            raise RuntimeError("execution report is busy")
        with control_plane_lock(state_dir, timeout_seconds=5) as control_acquired:
            if not control_acquired:
                raise RuntimeError("routing control plane is busy")
            recover_pending_transaction(state_dir)
            if not marker.is_file():
                if archive.is_file():
                    archived = json.loads(archive.read_text(encoding="utf-8"))
                    if archived.get("reportId") != report_id:
                        raise ValueError("execution report recovery archive is invalid")
                    return {
                        "status": "released-for-retry",
                        "reportId": report_id,
                        "archiveRetained": True,
                        "storesTaskText": False,
                        "policyMutationAuthorized": False,
                        "modelCalls": 0,
                    }
                raise ValueError("execution report marker does not exist")
            with append_lock(_execution_report_lock_path(reports)) as marker_acquired:
                if not marker_acquired:
                    raise RuntimeError("execution report marker store is busy")
                stored = _validate_execution_report_marker(
                    marker, json.loads(marker.read_text(encoding="utf-8"))
                )
            target_feedback = feedback_path or default_feedback_path(state_dir)
            events = read_feedback(target_feedback)
            matching_outcomes = [
                event for event in events
                if event.get("eventType") == "route_outcome"
                and event.get("routeId") == stored["routeId"]
            ]
            matching_labels = [
                event for event in events
                if event.get("eventType") == "human_label"
                and event.get("routeId") == stored["routeId"]
            ]
            nonterminal = stored["state"] in {"pending", "incomplete"}
            no_phase_progress = not any(
                stored[field]
                for field in ("routeRecorded", "labelRecorded", "cycleProcessed", "cycleDeferred")
            )
            can_release = (
                nonterminal
                and no_phase_progress
                and not matching_outcomes
                and not matching_labels
            )
            label_evidence_satisfied = (
                len(matching_labels) == 1 if stored["labelExpected"] else len(matching_labels) <= 1
            )
            can_acknowledge = (
                nonterminal
                and len(matching_outcomes) == 1
                and label_evidence_satisfied
            )
            inspection = {
                "status": "review-required" if nonterminal else "already-recorded",
                "reportId": report_id,
                "markerState": stored["state"],
                "progress": {
                    "labelExpected": stored["labelExpected"],
                    "routeRecorded": stored["routeRecorded"],
                    "labelRecorded": stored["labelRecorded"],
                    "cycleProcessed": stored["cycleProcessed"],
                    "cycleDeferred": stored["cycleDeferred"],
                },
                "evidence": {
                    "routeOutcomeEvents": len(matching_outcomes),
                    "humanLabelEvents": len(matching_labels),
                },
                "allowedActions": {
                    "releaseForRetry": can_release,
                    "acknowledgeRecorded": can_acknowledge,
                },
                "storesTaskText": False,
                "policyMutationAuthorized": False,
                "modelCalls": 0,
            }
            if action == "inspect":
                return inspection
            if action == "release-for-retry":
                if not can_release:
                    raise ValueError("execution report cannot be released because progress or evidence exists")
                resolved_at = utc_now()
                archived = dict(stored)
                archived.update(
                    {
                        "state": "released",
                        "resolvedAt": resolved_at,
                        "resolvedBy": resolved_by,
                        "resolution": "released-for-retry",
                        "routeOutcomeEvents": 0,
                        "humanLabelEvents": 0,
                    }
                )
                with append_lock(_execution_report_lock_path(reports)) as marker_acquired:
                    if not marker_acquired:
                        raise RuntimeError("execution report marker store is busy")
                    atomic_write_json(archive, archived)
                    marker.unlink()
                return {
                    "status": "released-for-retry",
                    "reportId": report_id,
                    "archiveRetained": True,
                    "retryAuthorized": True,
                    "storesTaskText": False,
                    "policyMutationAuthorized": False,
                    "modelCalls": 0,
                }
            if not can_acknowledge:
                raise ValueError("execution report evidence is not safe to acknowledge")
            resolved_at = utc_now()
            reconciled = dict(stored)
            reconciled.update(
                {
                    "state": "recorded",
                    "completedAt": resolved_at,
                    "routeRecorded": True,
                    "labelRecorded": len(matching_labels) == 1,
                    "cycleDeferred": not stored["cycleProcessed"],
                    "resolvedAt": resolved_at,
                    "resolvedBy": resolved_by,
                    "resolution": "acknowledged-recorded",
                }
            )
            reconciled.pop("errorType", None)
            _write_execution_report_marker(reports, marker, reconciled)
            return {
                "status": "acknowledged-recorded",
                "reportId": report_id,
                "learningCycleRequired": reconciled["cycleDeferred"],
                "nextCommand": "cycle" if reconciled["cycleDeferred"] else None,
                "storesTaskText": False,
                "policyMutationAuthorized": False,
                "modelCalls": 0,
            }


def policy_shadow(
    state_dir: Path,
    feedback_path: Path | None = None,
    candidate_path: Path | None = None,
    *,
    registry: ModelRegistry | None = None,
) -> dict[str, Any]:
    """Compare a candidate against its baseline without activating or routing it."""
    active_registry = registry or load_model_registry()
    with control_plane_lock(state_dir, timeout_seconds=5) as acquired:
        if not acquired:
            raise RuntimeError("routing control plane is busy")
        recover_pending_transaction(state_dir)
        lifecycle = load_state(state_dir)
        if candidate_path is None:
            if lifecycle.get("status") not in {"canary", "probation"}:
                return {
                    "schema": POLICY_SHADOW_SCHEMA,
                    "assessment": "no-candidate",
                    "activationAuthorized": False,
                    "storesTaskText": False,
                    "modelCalls": 0,
                }
            candidate = _load_state_candidate(state_dir, lifecycle)
            source = "lifecycle"
        else:
            resolved_candidate = candidate_path.resolve(strict=False)
            if not resolved_candidate.is_file():
                raise ValueError("shadow candidate file does not exist")
            candidate = json.loads(resolved_candidate.read_text(encoding="utf-8"))
            if not isinstance(candidate, dict) or not _candidate_is_intact(candidate):
                raise ValueError("shadow candidate integrity check failed")
            source = "explicit-file"
        if candidate.get("featureSchemaVersion") != FEATURE_SCHEMA_VERSION:
            raise ValueError("shadow candidate uses a stale routing feature schema")
        if candidate.get("modelRegistryDigest") != registry_digest(active_registry):
            raise ValueError("shadow candidate uses a stale model registry")
        priors = load_benchmark_priors(registry=active_registry)
        if candidate.get("benchmarkPriorsDigest") != benchmark_priors_digest(priors):
            raise ValueError("shadow candidate uses stale benchmark priors")
        baseline = policy_from_dict(candidate.get("basePolicy", {}))
        contender = policy_from_dict(candidate.get("policy", {}))
        if candidate.get("basePolicyDigest") != policy_digest(baseline):
            raise ValueError("shadow candidate base policy digest is invalid")
        active_policy, _ = load_active_policy(state_dir)
        if policy_digest(active_policy) not in {
            policy_digest(baseline),
            policy_digest(contender),
        }:
            raise ValueError("shadow candidate is stale because the active policy changed")
        events, feedback_storage = load_maintained_feedback(
            feedback_path or default_feedback_path(state_dir),
            maximum_routes=DEFAULT_MAX_FEEDBACK_ROUTES,
            retention_days=DEFAULT_FEEDBACK_RETENTION_DAYS,
            apply=False,
        )
        comparison = shadow_policy_comparison(
            learning_samples(events, active_registry),
            baseline,
            contender,
            active_registry,
            priors,
        )
        comparison.update(
            {
                "candidateId": candidate.get("candidateId"),
                "candidateSource": source,
                "lifecycleStatus": lifecycle.get("status"),
                "feedbackStorage": feedback_storage,
            }
        )
        return comparison


def status(state_dir: Path, feedback_path: Path | None = None) -> dict[str, Any]:
    with control_plane_lock(state_dir, timeout_seconds=5) as acquired:
        if not acquired:
            raise RuntimeError("routing control plane is busy")
        recover_pending_transaction(state_dir)
        target_feedback = feedback_path or default_feedback_path(state_dir)
        events, feedback_storage = load_maintained_feedback(
            target_feedback,
            maximum_routes=DEFAULT_MAX_FEEDBACK_ROUTES,
            retention_days=DEFAULT_FEEDBACK_RETENTION_DAYS,
            apply=False,
        )
        registry = load_model_registry()
        samples = learning_samples(events, registry)
        active, source = load_active_policy(state_dir)
        reports = resolve_control_plane_path(state_dir, ControlPlanePaths(state_dir).reports)
        execution_report_storage = maintain_execution_reports(state_dir, apply=False)
        return {
            "schemaVersion": STATE_SCHEMA_VERSION,
            "config": load_config(state_dir),
            "lifecycle": load_state(state_dir),
            "activePolicy": policy_to_dict(active),
            "activePolicyDigest": policy_digest(active),
            "activePolicySource": source,
            "learningSignals": len(samples),
            "humanLabels": sum(
                sample.get("labelSource") != "verified-tier-escalation" for sample in samples
            ),
            "verifiedTierEscalations": sum(
                sample.get("labelSource") == "verified-tier-escalation" for sample in samples
            ),
            "executionReports": len(list(reports.glob("*.json"))) if reports.is_dir() else 0,
            "executionReportStorage": execution_report_storage,
            "feedbackStorage": feedback_storage,
            "storesTaskText": False,
            "modelCalls": 0,
        }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    configure_parser = subparsers.add_parser("configure")
    configure_parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR)
    configure_parser.add_argument(
        "--mode",
        choices=sorted(MODES),
        required=True,
    )
    configure_parser.add_argument("--minimum-signals", type=int, default=12)
    configure_parser.add_argument("--minimum-validation-accuracy-gain", type=float, default=0.05)
    configure_parser.add_argument("--canary-percent", type=int, default=20)
    configure_parser.add_argument("--minimum-canary-reports", type=int, default=6)
    configure_parser.add_argument("--minimum-baseline-reports", type=int, default=6)
    configure_parser.add_argument("--minimum-probation-reports", type=int, default=12)
    configure_parser.add_argument("--maximum-failure-rate-increase", type=float, default=0.05)

    cycle_parser = subparsers.add_parser("cycle")
    cycle_parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR)
    cycle_parser.add_argument("--feedback-file", type=Path)
    cycle_parser.add_argument("--dry-run", action="store_true")

    report_parser = subparsers.add_parser("report")
    report_parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR)
    report_parser.add_argument("--feedback-file", type=Path)
    report_parser.add_argument("--stdin", action="store_true", required=True)

    status_parser = subparsers.add_parser("status")
    status_parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR)
    status_parser.add_argument("--feedback-file", type=Path)

    feedback_parser = subparsers.add_parser("feedback")
    feedback_parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR)
    feedback_parser.add_argument("--feedback-file", type=Path)
    feedback_parser.add_argument(
        "--maximum-routes", type=int, default=DEFAULT_MAX_FEEDBACK_ROUTES
    )
    feedback_parser.add_argument(
        "--retention-days", type=int, default=DEFAULT_FEEDBACK_RETENTION_DAYS
    )
    feedback_parser.add_argument("--apply", action="store_true")

    reports_parser = subparsers.add_parser("reports")
    reports_parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR)
    reports_parser.add_argument(
        "--maximum-markers",
        type=int,
        default=DEFAULT_MAX_EXECUTION_REPORT_MARKERS,
    )
    reports_parser.add_argument(
        "--retention-days",
        type=int,
        default=DEFAULT_EXECUTION_REPORT_RETENTION_DAYS,
    )
    reports_parser.add_argument("--apply", action="store_true")

    recover_report_parser = subparsers.add_parser("recover-report")
    recover_report_parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR)
    recover_report_parser.add_argument("--feedback-file", type=Path)
    recover_report_parser.add_argument("--report-id", required=True)
    recover_report_parser.add_argument(
        "--action",
        choices=("inspect", "release-for-retry", "acknowledge-recorded"),
        default="inspect",
    )
    recover_report_parser.add_argument("--confirm-report-id")
    recover_report_parser.add_argument("--resolved-by")

    shadow_parser = subparsers.add_parser("shadow")
    shadow_parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR)
    shadow_parser.add_argument("--feedback-file", type=Path)
    shadow_parser.add_argument("--candidate", type=Path)

    boundary_parser = subparsers.add_parser("check-boundary")
    boundary_parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR)
    boundary_parser.add_argument("--feedback-file", type=Path)
    boundary_parser.add_argument("--host-permissions-json", required=True)
    boundary_parser.add_argument(
        "--requested-sandbox",
        choices=("inherit", "read-only", "workspace-write", "danger-full-access"),
        default="inherit",
    )
    boundary_parser.add_argument(
        "--model-affinity", choices=MODEL_AFFINITY_MODES, default="auto"
    )

    args = parser.parse_args()
    try:
        if args.command == "configure":
            result = configure(
                args.state_dir,
                mode=args.mode,
                minimum_signals=args.minimum_signals,
                minimum_validation_accuracy_gain=args.minimum_validation_accuracy_gain,
                canary_percent=args.canary_percent,
                minimum_canary_reports=args.minimum_canary_reports,
                minimum_baseline_reports=args.minimum_baseline_reports,
                minimum_probation_reports=args.minimum_probation_reports,
                maximum_failure_rate_increase=args.maximum_failure_rate_increase,
            )
        elif args.command == "cycle":
            result = run_cycle(args.state_dir, args.feedback_file, dry_run=args.dry_run)
        elif args.command == "report":
            payload = json.load(sys.stdin)
            if not isinstance(payload, dict):
                raise ValueError("execution report input must be an object")
            result = ingest_execution_report(payload, args.state_dir, args.feedback_file)
        elif args.command == "feedback":
            result = maintain_feedback_state(
                args.state_dir,
                args.feedback_file,
                maximum_routes=args.maximum_routes,
                retention_days=args.retention_days,
                apply=args.apply,
            )
        elif args.command == "reports":
            result = maintain_execution_report_state(
                args.state_dir,
                maximum_markers=args.maximum_markers,
                retention_days=args.retention_days,
                apply=args.apply,
            )
        elif args.command == "recover-report":
            result = recover_execution_report(
                args.state_dir,
                args.report_id,
                args.feedback_file,
                action=args.action,
                confirm_report_id=args.confirm_report_id,
                resolved_by=args.resolved_by,
            )
        elif args.command == "shadow":
            result = policy_shadow(
                args.state_dir,
                args.feedback_file,
                args.candidate,
            )
        elif args.command == "check-boundary":
            try:
                permissions = parse_host_permissions(args.host_permissions_json)
            except (ValueError, json.JSONDecodeError) as exc:
                print(json.dumps({
                    "protected": False,
                    "reason": "invalid-host-permissions",
                    "message": str(exc),
                    "modelCalls": 0,
                }, ensure_ascii=True, indent=2))
                return 2
            try:
                issue = learning_boundary_issue(
                    args.state_dir,
                    args.feedback_file,
                    permissions,
                    args.requested_sandbox,
                    args.model_affinity,
                )
            except ControlPlaneRecoveryRequired as exc:
                print(json.dumps({
                    "protected": False,
                    "reason": "guarded-auto-recovery-required",
                    "message": str(exc),
                    "modelCalls": 0,
                }, ensure_ascii=True, indent=2))
                return 2
            if issue:
                print(json.dumps({
                    "protected": False,
                    "reason": "guarded-auto-state-writable-by-child",
                    "message": issue,
                    "modelCalls": 0,
                }, ensure_ascii=True, indent=2))
                return 2
            result = {
                "protected": True,
                "mode": load_config(args.state_dir)["mode"],
                "modelCalls": 0,
            }
        else:
            result = status(args.state_dir, args.feedback_file)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        if args.command == "check-boundary":
            print(json.dumps({
                "protected": False,
                "reason": "guarded-auto-boundary-check-failed",
                "message": str(exc),
                "modelCalls": 0,
            }, ensure_ascii=True, indent=2))
            return 2
        parser.error(str(exc))
    print(json.dumps(result, ensure_ascii=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
