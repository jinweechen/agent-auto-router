#!/usr/bin/env python3
"""Guarded automatic routing calibration with canary promotion and rollback."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from benchmark_priors import benchmark_priors_digest, load_benchmark_priors
from host_permissions import HostPermissions, parse_host_permissions
from model_registry import TIER_RANK, ModelRegistry, load_model_registry, registry_digest
from policy_learning import (
    SAFE_ID_PATTERN,
    _append_jsonl,
    _archive_policy,
    _atomic_write_json,
    _canonical_digest,
    append_label_event,
    append_route_event,
    build_candidate,
    default_feedback_path,
    labeled_samples,
    read_feedback,
    utc_now,
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
from state_lock import control_plane_lock


EXECUTION_REPORT_SCHEMA = "agent-auto-router.execution-report.v1"
CONFIG_SCHEMA_VERSION = 1
STATE_SCHEMA_VERSION = 1
MODES = frozenset({"manual", "guarded-auto"})
REPORT_STATUSES = frozenset({"succeeded", "failed", "blocked", "cancelled", "timed_out"})
VERIFICATION_STATUSES = frozenset({"passed", "failed", "not-run"})
SAFE_HOST_PATTERN = re.compile(r"[A-Za-z0-9._:-]{1,80}")


DEFAULT_CONFIG: dict[str, Any] = {
    "schemaVersion": CONFIG_SCHEMA_VERSION,
    "mode": "manual",
    "minimumSignals": 20,
    "minimumValidationAccuracyGain": 0.05,
    "maximumThresholdStep": 1,
    "canaryPercent": 10,
    "minimumCanaryReports": 10,
    "minimumBaselineReports": 10,
    "minimumProbationReports": 20,
    "maximumFailureRateIncrease": 0.05,
}


def _config_path(state_dir: Path) -> Path:
    return state_dir / "guarded-auto-config.json"


def _state_path(state_dir: Path) -> Path:
    return state_dir / "guarded-auto-state.json"


def _audit_path(state_dir: Path) -> Path:
    return state_dir / "audit.jsonl"


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
        raise ValueError("guarded-auto mode must be manual or guarded-auto")
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
    path = _config_path(state_dir)
    if not path.is_file():
        return dict(DEFAULT_CONFIG)
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("guarded-auto config must be an object")
    return validate_config(payload)


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
) -> str | None:
    """Return why guarded state is writable by the child, or None when protected."""
    if load_config(state_dir)["mode"] != "guarded-auto":
        return None
    effective = permissions.effective_sandbox(requested_sandbox)
    if effective in {"danger-full-access", "external-sandbox"}:
        return f"guarded-auto requires protected state; child sandbox is {effective}"
    if effective == "read-only":
        return None
    protected_paths = [state_dir, feedback_path or default_feedback_path(state_dir)]
    for protected_path in protected_paths:
        for root_value in permissions.writable_roots:
            if _path_is_within(protected_path, Path(root_value)):
                return (
                    "guarded-auto state and feedback must be outside child writable roots: "
                    f"{protected_path}"
                )
    return None


def _configure_unlocked(
    state_dir: Path,
    *,
    mode: str,
    minimum_signals: int = 20,
    minimum_validation_accuracy_gain: float = 0.05,
    canary_percent: int = 10,
    minimum_canary_reports: int = 10,
    minimum_baseline_reports: int = 10,
    minimum_probation_reports: int = 20,
    maximum_failure_rate_increase: float = 0.05,
) -> dict[str, Any]:
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
    if mode == "manual":
        state = load_state(state_dir)
        if state.get("status") == "probation":
            _restore_snapshot(
                state_dir, state, "guarded-auto-disabled-during-probation"
            )
        elif state.get("status") == "canary":
            event = {
                "eventType": "policy_auto_cancelled",
                "recordedAt": utc_now(),
                "reason": "guarded-auto-disabled",
                "candidateId": state.get("candidateId"),
            }
            _append_jsonl(_audit_path(state_dir), event)
            _atomic_write_json(_state_path(state_dir), _idle_state("guarded-auto-disabled"))
    _atomic_write_json(_config_path(state_dir), payload)
    return payload


def configure(
    state_dir: Path,
    *,
    mode: str,
    minimum_signals: int = 20,
    minimum_validation_accuracy_gain: float = 0.05,
    canary_percent: int = 10,
    minimum_canary_reports: int = 10,
    minimum_baseline_reports: int = 10,
    minimum_probation_reports: int = 20,
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
    last_evaluated_signals: int | None = None,
    last_candidate_id: str | None = None,
) -> dict[str, Any]:
    state: dict[str, Any] = {
        "schemaVersion": STATE_SCHEMA_VERSION,
        "status": "idle",
        "updatedAt": utc_now(),
    }
    if reason:
        state["reason"] = reason
    if last_evaluated_signals is not None:
        state["lastEvaluatedSignals"] = max(0, int(last_evaluated_signals))
    if last_candidate_id:
        state["lastCandidateId"] = last_candidate_id
    return state


def load_state(state_dir: Path) -> dict[str, Any]:
    path = _state_path(state_dir)
    if not path.is_file():
        return _idle_state()
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("schemaVersion") != STATE_SCHEMA_VERSION:
        raise ValueError("unsupported guarded-auto state")
    if payload.get("status") not in {"idle", "canary", "probation"}:
        raise ValueError("invalid guarded-auto lifecycle state")
    return payload


def _timestamp(value: Any) -> datetime:
    if not isinstance(value, str):
        return datetime.min.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


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
    return isinstance(candidate_id, str) and candidate_id == _canonical_digest(unsigned)


def _write_guarded_candidate(
    state_dir: Path,
    samples: list[dict[str, Any]],
    base_policy: RoutingPolicy,
    config: dict[str, Any],
    registry: ModelRegistry,
    *,
    write: bool = True,
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
    candidate["candidateId"] = _canonical_digest(candidate)
    path = state_dir / "candidates" / f"guarded-{candidate['candidateId'][:16]}.json"
    if write:
        _atomic_write_json(path, candidate)
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
    _atomic_write_json(_state_path(state_dir), state)
    _append_jsonl(_audit_path(state_dir), {
        "eventType": "policy_auto_canary_started",
        "recordedAt": started_at,
        "candidateId": candidate["candidateId"],
        "basePolicyDigest": state["basePolicyDigest"],
        "candidatePolicyDigest": state["candidatePolicyDigest"],
        "canaryPercent": state["canaryPercent"],
    })
    return state


def _load_state_candidate(state: dict[str, Any]) -> dict[str, Any]:
    path = Path(str(state.get("candidatePath", "")))
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
    archived = _archive_policy(state_dir, base_policy, "guarded-auto-promotion")
    active_payload = policy_to_dict(candidate_policy)
    active_payload["activation"] = {
        "activatedAt": utc_now(),
        "activationMode": "guarded-auto",
        "candidateId": candidate["candidateId"],
    }
    _atomic_write_json(state_dir / "active-policy.json", active_payload)
    started_at = utc_now()
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
    _atomic_write_json(_state_path(state_dir), next_state)
    _append_jsonl(_audit_path(state_dir), {
        "eventType": "policy_auto_promoted",
        "recordedAt": started_at,
        "candidateId": candidate["candidateId"],
        "fromDigest": policy_digest(base_policy),
        "toDigest": policy_digest(candidate_policy),
        "rollbackSnapshot": str(archived.resolve()),
    })
    return next_state


def _restore_snapshot(state_dir: Path, state: dict[str, Any], reason: str) -> dict[str, Any]:
    current, _ = load_active_policy(state_dir)
    snapshot_path = Path(str(state.get("basePolicySnapshot", "")))
    snapshot_payload = json.loads(snapshot_path.read_text(encoding="utf-8"))
    if not isinstance(snapshot_payload, dict):
        raise ValueError("guarded-auto rollback snapshot is invalid")
    previous = policy_from_dict(snapshot_payload)
    _archive_policy(state_dir, current, "guarded-auto-rollback")
    active_payload = policy_to_dict(previous)
    active_payload["activation"] = {
        "activatedAt": utc_now(),
        "activationMode": "guarded-auto-rollback",
        "candidateId": state.get("candidateId"),
        "reason": reason,
    }
    _atomic_write_json(state_dir / "active-policy.json", active_payload)
    event = {
        "eventType": "policy_auto_rolled_back",
        "recordedAt": utc_now(),
        "candidateId": state.get("candidateId"),
        "fromDigest": policy_digest(current),
        "toDigest": policy_digest(previous),
        "reason": reason,
    }
    _append_jsonl(_audit_path(state_dir), event)
    _atomic_write_json(_state_path(state_dir), _idle_state(reason))
    return event


def _cycle_unlocked(
    state_dir: Path,
    feedback_path: Path,
    *,
    dry_run: bool,
    registry: ModelRegistry,
) -> dict[str, Any]:
    config = load_config(state_dir)
    state = load_state(state_dir)
    events = read_feedback(feedback_path)
    if config["mode"] != "guarded-auto":
        return {"status": "manual", "action": "none", "modelCalls": 0}
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
        previous_signal_count = int(state.get("lastEvaluatedSignals", 0))
        reevaluation_interval = max(4, int(config["minimumSignals"]) // 4)
        if previous_signal_count and len(samples) < previous_signal_count + reevaluation_interval:
            return {
                "status": "waiting-for-new-signals",
                "action": "none",
                "signals": len(samples),
                "lastEvaluatedSignals": previous_signal_count,
                "nextEvaluationAt": previous_signal_count + reevaluation_interval,
                "modelCalls": 0,
            }
        candidate, path = _write_guarded_candidate(
            state_dir,
            samples,
            active_policy,
            config,
            registry,
            write=not dry_run,
        )
        if not candidate.get("eligibleForAutoCanary"):
            if not dry_run:
                _atomic_write_json(
                    _state_path(state_dir),
                    _idle_state(
                        "candidate-rejected",
                        last_evaluated_signals=len(samples),
                        last_candidate_id=str(candidate["candidateId"]),
                    ),
                )
                _append_jsonl(_audit_path(state_dir), {
                    "eventType": "policy_auto_candidate_rejected",
                    "recordedAt": utc_now(),
                    "candidateId": candidate["candidateId"],
                    "signals": len(samples),
                })
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

    candidate = _load_state_candidate(state)
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
                _append_jsonl(_audit_path(state_dir), event)
                _atomic_write_json(
                    _state_path(state_dir),
                    _idle_state(
                        event["reason"],
                        last_evaluated_signals=len(learning_samples(events, registry)),
                        last_candidate_id=str(candidate["candidateId"]),
                    ),
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
        event = _restore_snapshot(state_dir, state, "probation-failure-rate-regression")
        _atomic_write_json(
            _state_path(state_dir),
            _idle_state(
                "probation-failure-rate-regression",
                last_evaluated_signals=len(learning_samples(events, registry)),
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
        _append_jsonl(_audit_path(state_dir), event)
        _atomic_write_json(
            _state_path(state_dir),
            _idle_state(
                "probation-passed",
                last_evaluated_signals=len(learning_samples(events, registry)),
                last_candidate_id=str(candidate["candidateId"]),
            ),
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
        return _cycle_unlocked(
            state_dir,
            feedback_path or default_feedback_path(state_dir),
            dry_run=dry_run,
            registry=active_registry,
        )


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
        "featureSchemaVersion", "explicitOverride",
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
    marker = state_dir / "reports" / f"{hashlib.sha256(report_id.encode('utf-8')).hexdigest()}.json"
    marker.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        existing = json.loads(marker.read_text(encoding="utf-8"))
        if existing.get("reportId") != report_id:
            raise ValueError("execution report marker collision")
        if existing.get("state") != "recorded":
            raise ValueError("execution report has an incomplete prior recording")
        return {"status": "duplicate", "reportId": report_id, "modelCalls": 0}
    normalized_marker = {
        "schema": EXECUTION_REPORT_SCHEMA,
        "reportId": report_id,
        "recordedAt": utc_now(),
        "host": host,
        "routeId": route_payload["route_id"],
        "state": "pending",
        "storesTaskText": False,
    }
    route_recorded = False
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as marker_stream:
            marker_stream.write(json.dumps(normalized_marker, ensure_ascii=True) + "\n")
        target_feedback = feedback_path or default_feedback_path(state_dir)
        event = append_route_event(route_payload, target_feedback, active_registry)
        route_recorded = True
        if preferred is not None:
            append_label_event(
                str(route_payload["route_id"]),
                preferred,
                "pass" if status == "succeeded" else "fail",
                target_feedback,
                active_registry,
            )
        cycle = process_recorded_outcome(state_dir, target_feedback, registry=active_registry)
        normalized_marker["state"] = "recorded"
        normalized_marker["completedAt"] = utc_now()
        _atomic_write_json(marker, normalized_marker)
        return {"status": "recorded", "reportId": report_id, "event": event, "guardedAuto": cycle, "modelCalls": 0}
    except Exception as exc:
        if route_recorded:
            normalized_marker["state"] = "incomplete"
            normalized_marker["errorType"] = type(exc).__name__
            _atomic_write_json(marker, normalized_marker)
        else:
            try:
                marker.unlink()
            except FileNotFoundError:
                pass
        raise


def status(state_dir: Path, feedback_path: Path | None = None) -> dict[str, Any]:
    events = read_feedback(feedback_path or default_feedback_path(state_dir))
    registry = load_model_registry()
    samples = learning_samples(events, registry)
    active, source = load_active_policy(state_dir)
    return {
        "schemaVersion": STATE_SCHEMA_VERSION,
        "config": load_config(state_dir),
        "lifecycle": load_state(state_dir),
        "activePolicy": policy_to_dict(active),
        "activePolicyDigest": policy_digest(active),
        "activePolicySource": source,
        "learningSignals": len(samples),
        "humanLabels": sum(sample.get("labelSource") != "verified-tier-escalation" for sample in samples),
        "verifiedTierEscalations": sum(sample.get("labelSource") == "verified-tier-escalation" for sample in samples),
        "executionReports": len(list((state_dir / "reports").glob("*.json"))) if (state_dir / "reports").is_dir() else 0,
        "storesTaskText": False,
        "modelCalls": 0,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    configure_parser = subparsers.add_parser("configure")
    configure_parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR)
    configure_parser.add_argument("--mode", choices=sorted(MODES), required=True)
    configure_parser.add_argument("--minimum-signals", type=int, default=20)
    configure_parser.add_argument("--minimum-validation-accuracy-gain", type=float, default=0.05)
    configure_parser.add_argument("--canary-percent", type=int, default=10)
    configure_parser.add_argument("--minimum-canary-reports", type=int, default=10)
    configure_parser.add_argument("--minimum-baseline-reports", type=int, default=10)
    configure_parser.add_argument("--minimum-probation-reports", type=int, default=20)
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

    boundary_parser = subparsers.add_parser("check-boundary")
    boundary_parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR)
    boundary_parser.add_argument("--feedback-file", type=Path)
    boundary_parser.add_argument("--host-permissions-json", required=True)
    boundary_parser.add_argument(
        "--requested-sandbox",
        choices=("inherit", "read-only", "workspace-write", "danger-full-access"),
        default="inherit",
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
        elif args.command == "check-boundary":
            permissions = parse_host_permissions(args.host_permissions_json)
            issue = learning_boundary_issue(
                args.state_dir,
                args.feedback_file,
                permissions,
                args.requested_sandbox,
            )
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
        parser.error(str(exc))
    print(json.dumps(result, ensure_ascii=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
