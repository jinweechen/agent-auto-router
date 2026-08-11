#!/usr/bin/env python3
"""Privacy-minimized feedback, candidate calibration, approval, and rollback."""

from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import math
import os
import re
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from efficiency_metrics import summarize_feedback
from model_affinity import ROLE_MODEL_POLICY_AFFINITY, ROLE_MODEL_POLICY_PROFILE

from benchmark_priors import (
    BenchmarkPriors,
    benchmark_priors_digest,
    load_benchmark_priors,
)
from control_plane_store import (
    ControlPlanePaths,
    append_jsonl as _append_jsonl,
    atomic_write_json as _atomic_write_json,
    canonical_digest as _canonical_digest,
    commit_control_plane_transaction,
    recover_pending_transaction,
    resolve_control_plane_path,
    utc_now,
)

from model_registry import (
    TIER_RANK,
    ModelRegistry,
    load_model_registry,
    registry_digest,
)
from routing_policy import (
    DEFAULT_STATE_DIR,
    EFFORTS,
    FEATURE_SCHEMA_VERSION,
    STRATEGIES,
    RoutingPolicy,
    load_active_policy,
    policy_digest,
    policy_from_dict,
    policy_to_dict,
)
from state_lock import append_lock, control_plane_lock

FEEDBACK_SCHEMA_VERSION = 5
CANDIDATE_SCHEMA_VERSION = 1
DEFAULT_FEEDBACK_RETENTION_DAYS = 90
DEFAULT_MAX_FEEDBACK_ROUTES = 5000
DEFAULT_REGISTRY = load_model_registry()
ROUTE_FEATURES = {
    "prompt_chars",
    "criteria_count",
    "complexity_score",
    "risk_score",
    "clarity_score",
    "high_risk",
    "constrained",
    "parallelizable",
    "dependency_ambiguity",
    "orchestration_eligible",
    "complex_debugging",
    "long_context",
    "multi_file",
    "computer_use",
    "validated_bounded",
    "scope_hits",
    "algorithm_hits",
    "repo_files",
    "source_files",
    "test_files",
    "language_count",
    "manifest_count",
    "large_repo",
    "monorepo",
    "dirty_worktree",
    "is_git_repo",
    "task_has_path_hint",
    "validation_configured",
    "validation_passed",
    "escalated",
}
ROUTE_REASONS = {
    "high_risk",
    "complexity",
    "intelligence_routine",
    "cost_proxy_complexity",
    "cost_proxy_default",
    "constrained",
    "balance_default",
    "explicit_model",
    "benchmark_validated_bounded",
    "benchmark_debugging_floor",
    "benchmark_long_context_floor",
    "benchmark_multi_file_floor",
    "benchmark_computer_use",
}
FORBIDDEN_STORED_KEYS = {
    "task",
    "prompt",
    "input",
    "output",
    "content",
    "credential",
    "credentials",
    "secret",
    "token",
    "api_key",
}
SAFE_ID_PATTERN = re.compile(r"[A-Za-z0-9._:-]{1,128}")


def default_feedback_path(state_dir: Path) -> Path:
    return ControlPlanePaths(state_dir).feedback


def _reject_sensitive_keys(value: Any, parent_key: str | None = None) -> None:
    if isinstance(value, dict):
        for key, nested in value.items():
            normalized_key = str(key).lower()
            if (
                parent_key not in {"observed_tokens", "selected_model_observed_tokens"}
                and normalized_key in FORBIDDEN_STORED_KEYS
            ):
                raise ValueError(f"feedback payload may not store field: {key}")
            _reject_sensitive_keys(nested, normalized_key)
    elif isinstance(value, list):
        for nested in value:
            _reject_sensitive_keys(nested, parent_key)


def _normalize_observed_tokens(
    value: Any,
    *,
    field_name: str,
) -> dict[str, int] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError(f"{field_name} must be an object or null")
    supported = {
        "input", "cached_input", "cache_write", "output", "reasoning_output", "total"
    }
    if set(value) - supported:
        raise ValueError(f"{field_name} contains unsupported fields")
    normalized: dict[str, int] = {}
    for key in (
        "input", "cached_input", "cache_write", "output", "reasoning_output", "total"
    ):
        count = value.get(key, 0)
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise ValueError(f"observed token count {key} must be a non-negative integer")
        normalized[key] = count
    if normalized["total"] != normalized["input"] + normalized["output"]:
        raise ValueError("observed total tokens must equal input plus output")
    if normalized["cached_input"] > normalized["input"]:
        raise ValueError("cached input tokens may not exceed input tokens")
    if normalized["cache_write"] > normalized["input"]:
        raise ValueError("cache write tokens may not exceed input tokens")
    if normalized["reasoning_output"] > normalized["output"]:
        raise ValueError("reasoning output tokens may not exceed output tokens")
    return normalized


def normalize_route_event(
    payload: dict[str, Any], registry: ModelRegistry | None = None
) -> dict[str, Any]:
    active_registry = registry or DEFAULT_REGISTRY
    _reject_sensitive_keys(payload)
    required = {
        "route_id",
        "strategy",
        "effort",
        "selector_model",
        "selected_model",
        "reason",
        "features",
        "policy_version",
        "policy_digest",
        "explicit_override",
        "exit_code",
        "duration_ms",
    }
    missing = sorted(required - payload.keys())
    if missing:
        raise ValueError(f"feedback payload is missing fields: {', '.join(missing)}")
    if payload["strategy"] not in STRATEGIES:
        raise ValueError("invalid feedback strategy")
    if payload["effort"] not in EFFORTS:
        raise ValueError("invalid feedback effort")
    route_id = str(payload["route_id"])
    if not SAFE_ID_PATTERN.fullmatch(route_id):
        raise ValueError("invalid feedback route ID")
    active_registry.get(str(payload["selector_model"]), role="direct")
    active_registry.get(str(payload["selected_model"]))
    target_tier = str(
        payload.get("target_tier") or active_registry.tier_for_model(str(payload["selector_model"]))
    )
    if target_tier != active_registry.tier_for_model(str(payload["selector_model"])):
        raise ValueError("feedback target tier does not match selector model")
    features = payload["features"]
    if not isinstance(features, dict):
        raise ValueError("feedback features must be an object")
    unknown_features = sorted(set(features) - ROUTE_FEATURES)
    if unknown_features:
        raise ValueError(f"unsupported feedback features: {', '.join(unknown_features)}")
    integer_features = {
        "prompt_chars", "criteria_count", "complexity_score", "risk_score", "clarity_score",
        "scope_hits", "algorithm_hits", "repo_files", "source_files", "test_files",
        "language_count", "manifest_count",
    }
    boolean_features = ROUTE_FEATURES - integer_features
    normalized_features: dict[str, int | bool] = {}
    for key in sorted(features):
        value = features[key]
        if key in integer_features:
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"feedback feature {key} must be an integer")
            normalized_features[key] = max(0, value)
        elif key in boolean_features:
            if not isinstance(value, bool):
                raise ValueError(f"feedback feature {key} must be a boolean")
            normalized_features[key] = value
    reason = str(payload["reason"])
    if reason not in ROUTE_REASONS:
        raise ValueError("invalid feedback route reason")
    policy_version = str(payload["policy_version"])
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,80}", policy_version):
        raise ValueError("invalid feedback policy version")
    stored_policy_digest = str(payload["policy_digest"])
    if not re.fullmatch(r"[0-9a-f]{64}", stored_policy_digest):
        raise ValueError("invalid feedback policy digest")
    stored_registry_digest = str(
        payload.get("registry_digest") or registry_digest(active_registry)
    )
    if not re.fullmatch(r"[0-9a-f]{64}", stored_registry_digest):
        raise ValueError("invalid feedback model registry digest")
    normalized_tokens = _normalize_observed_tokens(
        payload.get("observed_tokens"), field_name="observed_tokens"
    )
    selected_model_tokens = _normalize_observed_tokens(
        payload.get("selected_model_observed_tokens"),
        field_name="selected_model_observed_tokens",
    )
    exit_code = int(payload["exit_code"])
    workspace_key = payload.get("workspace_key")
    if workspace_key is not None and not re.fullmatch(r"[0-9a-f]{64}", str(workspace_key)):
        raise ValueError("feedback workspace key must be a SHA-256 digest")
    topology = str(payload.get("topology") or "direct")
    if topology not in {"direct", "orchestrated"}:
        raise ValueError("invalid feedback topology")
    variant = str(
        payload.get("variant")
        or {"frontier": "A", "balanced": "E", "fast": "F"}[target_tier]
    )
    if variant not in {"A", "B", "C", "D", "E", "F"}:
        raise ValueError("invalid feedback variant")
    role_model_policy = str(
        payload.get("role_model_policy") or ROLE_MODEL_POLICY_PROFILE
    )
    if role_model_policy not in {ROLE_MODEL_POLICY_AFFINITY, ROLE_MODEL_POLICY_PROFILE}:
        raise ValueError("invalid feedback role model policy")
    estimated_switches = payload.get("estimated_role_tier_switches", 0)
    if (
        isinstance(estimated_switches, bool)
        or not isinstance(estimated_switches, int)
        or not 0 <= estimated_switches <= 20
    ):
        raise ValueError("invalid feedback estimated role tier switches")
    raw_feature_schema_version = payload.get("feature_schema_version", 1)
    if (
        isinstance(raw_feature_schema_version, bool)
        or not isinstance(raw_feature_schema_version, int)
        or raw_feature_schema_version < 1
    ):
        raise ValueError("feedback feature schema version must be a positive integer")
    return {
        "schemaVersion": FEEDBACK_SCHEMA_VERSION,
        "eventType": "route_outcome",
        "recordedAt": utc_now(),
        "routeId": route_id,
        "featureSchemaVersion": raw_feature_schema_version,
        "strategy": payload["strategy"],
        "effort": payload["effort"],
        "selectorModel": payload["selector_model"],
        "selectedModel": payload["selected_model"],
        "targetTier": target_tier,
        "reason": reason,
        "features": normalized_features,
        "policyVersion": policy_version,
        "policyDigest": stored_policy_digest,
        "modelRegistryDigest": stored_registry_digest,
        "explicitOverride": bool(payload["explicit_override"]),
        "workspaceKey": str(workspace_key) if workspace_key is not None else None,
        "topology": topology,
        "variant": variant,
        "roleModelPolicy": role_model_policy,
        "estimatedRoleTierSwitches": estimated_switches,
        "exitCode": exit_code,
        "executionSucceeded": exit_code == 0,
        "durationMs": max(0, int(payload["duration_ms"])),
        "observedTokens": normalized_tokens,
        "selectedModelObservedTokens": selected_model_tokens,
        "validationConfigured": bool(payload.get("validation_configured", False)),
        "validationPassed": (
            bool(payload.get("validation_passed"))
            if payload.get("validation_passed") is not None else None
        ),
        "escalated": bool(payload.get("escalated", False)),
        "attemptCount": max(1, int(payload.get("attempt_count", 1))),
    }


def append_route_event(
    payload: dict[str, Any],
    feedback_path: Path,
    registry: ModelRegistry | None = None,
) -> dict[str, Any]:
    event = normalize_route_event(payload, registry)
    _append_jsonl(feedback_path, event)
    return event


def append_label_event(
    route_id: str,
    preferred_model: str,
    outcome: str,
    feedback_path: Path,
    registry: ModelRegistry | None = None,
) -> dict[str, Any]:
    active_registry = registry or DEFAULT_REGISTRY
    preferred = active_registry.get(preferred_model, role="direct")
    if outcome not in {"pass", "partial", "fail"}:
        raise ValueError("outcome must be pass, partial, or fail")
    if not SAFE_ID_PATTERN.fullmatch(route_id):
        raise ValueError("invalid feedback route ID")
    event = {
        "schemaVersion": FEEDBACK_SCHEMA_VERSION,
        "eventType": "human_label",
        "recordedAt": utc_now(),
        "routeId": route_id,
        "preferredModel": preferred_model,
        "preferredTier": preferred.tier,
        "outcome": outcome,
    }
    _append_jsonl(feedback_path, event)
    return event


def read_feedback(feedback_path: Path) -> list[dict[str, Any]]:
    if not feedback_path.is_file():
        return []
    with append_lock(feedback_path) as acquired:
        if not acquired:
            raise RuntimeError(
                f"timed out waiting to read router state: {feedback_path.name}"
            )
        lines = feedback_path.read_text(encoding="utf-8").splitlines()
    return _parse_feedback_lines(lines)


def _parse_feedback_lines(lines: Iterable[str]) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for line_number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid feedback JSON at line {line_number}") from exc
        if not isinstance(event, dict):
            raise ValueError(f"feedback line {line_number} must be an object")
        events.append(event)
    return events


def _feedback_timestamp(event: dict[str, Any]) -> datetime:
    value = event.get("recordedAt")
    if not isinstance(value, str):
        raise ValueError("feedback event is missing recordedAt")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("feedback event has invalid recordedAt") from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _compact_feedback_events(
    events: list[dict[str, Any]],
    *,
    maximum_routes: int,
    retention_days: int,
    now: datetime,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    cutoff = now.astimezone(timezone.utc) - timedelta(days=retention_days)
    grouped: dict[str, dict[str, tuple[int, datetime, dict[str, Any]]]] = {}
    latest_by_route: dict[str, tuple[int, datetime]] = {}
    for index, event in enumerate(events):
        event_type = event.get("eventType")
        if event_type not in {"route_outcome", "human_label"}:
            raise ValueError("feedback contains an unsupported event type")
        route_id = str(event.get("routeId", ""))
        if not SAFE_ID_PATTERN.fullmatch(route_id):
            raise ValueError("feedback contains an invalid route ID")
        recorded_at = _feedback_timestamp(event).astimezone(timezone.utc)
        grouped.setdefault(route_id, {})[str(event_type)] = (index, recorded_at, event)
        previous = latest_by_route.get(route_id)
        if previous is None or (recorded_at, index) > (previous[1], previous[0]):
            latest_by_route[route_id] = (index, recorded_at)

    age_eligible = [
        route_id
        for route_id, (_, recorded_at) in latest_by_route.items()
        if recorded_at >= cutoff
    ]
    selected_routes = set(
        sorted(
            age_eligible,
            key=lambda route_id: (
                latest_by_route[route_id][1],
                latest_by_route[route_id][0],
                route_id,
            ),
        )[-maximum_routes:]
    )
    retained_indexed = [
        item
        for route_id in selected_routes
        for item in grouped[route_id].values()
    ]
    retained = [item[2] for item in sorted(retained_indexed, key=lambda item: item[0])]
    summary = {
        "beforeEvents": len(events),
        "afterEvents": len(retained),
        "eventsRemoved": len(events) - len(retained),
        "beforeRoutes": len(grouped),
        "afterRoutes": len(selected_routes),
        "routesRemovedByAge": len(grouped) - len(age_eligible),
        "routesRemovedByLimit": max(0, len(age_eligible) - len(selected_routes)),
        "wouldChange": retained != events,
    }
    return retained, summary


def _rewrite_feedback_unlocked(path: Path, events: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            for event in events:
                handle.write(
                    json.dumps(event, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
                    + "\n"
                )
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def load_maintained_feedback(
    feedback_path: Path,
    *,
    maximum_routes: int = DEFAULT_MAX_FEEDBACK_ROUTES,
    retention_days: int = DEFAULT_FEEDBACK_RETENTION_DAYS,
    apply: bool = True,
    now: datetime | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Read and optionally compact feedback under one append lock."""
    if isinstance(maximum_routes, bool) or not 1 <= maximum_routes <= 100000:
        raise ValueError("maximum feedback routes must be between 1 and 100000")
    if isinstance(retention_days, bool) or not 1 <= retention_days <= 3650:
        raise ValueError("feedback retention days must be between 1 and 3650")
    if not feedback_path.is_file():
        return [], {
            "path": str(feedback_path),
            "exists": False,
            "maximumRoutes": maximum_routes,
            "retentionDays": retention_days,
            "beforeEvents": 0,
            "afterEvents": 0,
            "eventsRemoved": 0,
            "beforeRoutes": 0,
            "afterRoutes": 0,
            "routesRemovedByAge": 0,
            "routesRemovedByLimit": 0,
            "wouldChange": False,
            "beforeBytes": 0,
            "afterBytes": 0,
            "oldestRetainedAt": None,
            "newestRetainedAt": None,
            "applied": False,
            "storesTaskText": False,
            "modelCalls": 0,
        }
    with append_lock(feedback_path) as acquired:
        if not acquired:
            raise RuntimeError(
                f"timed out waiting to maintain router state: {feedback_path.name}"
            )
        before_bytes = feedback_path.stat().st_size
        events = _parse_feedback_lines(
            feedback_path.read_text(encoding="utf-8").splitlines()
        )
        retained, summary = _compact_feedback_events(
            events,
            maximum_routes=maximum_routes,
            retention_days=retention_days,
            now=now or datetime.now(timezone.utc),
        )
        changed = bool(summary["wouldChange"])
        if apply and changed:
            _rewrite_feedback_unlocked(feedback_path, retained)
        after_bytes = (
            feedback_path.stat().st_size
            if apply and changed
            else sum(
                len(
                    (
                        json.dumps(
                            event,
                            ensure_ascii=True,
                            sort_keys=True,
                            separators=(",", ":"),
                        )
                        + "\n"
                    ).encode("utf-8")
                )
                for event in retained
            )
        )
    retained_times = [_feedback_timestamp(event) for event in retained]
    summary.update(
        {
            "path": str(feedback_path),
            "exists": True,
            "maximumRoutes": maximum_routes,
            "retentionDays": retention_days,
            "beforeBytes": before_bytes,
            "afterBytes": after_bytes,
            "oldestRetainedAt": (
                min(retained_times).astimezone(timezone.utc).isoformat()
                if retained_times
                else None
            ),
            "newestRetainedAt": (
                max(retained_times).astimezone(timezone.utc).isoformat()
                if retained_times
                else None
            ),
            "applied": bool(apply and changed),
            "storesTaskText": False,
            "modelCalls": 0,
        }
    )
    return retained, summary


def maintain_feedback(
    feedback_path: Path,
    *,
    maximum_routes: int = DEFAULT_MAX_FEEDBACK_ROUTES,
    retention_days: int = DEFAULT_FEEDBACK_RETENTION_DAYS,
    apply: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Inspect feedback retention or atomically apply it without exposing content."""
    _, summary = load_maintained_feedback(
        feedback_path,
        maximum_routes=maximum_routes,
        retention_days=retention_days,
        apply=apply,
        now=now,
    )
    return summary


def labeled_samples(
    events: Iterable[dict[str, Any]], registry: ModelRegistry | None = None
) -> list[dict[str, Any]]:
    active_registry = registry or DEFAULT_REGISTRY
    routes: dict[str, dict[str, Any]] = {}
    labels: dict[str, dict[str, Any]] = {}
    for event in events:
        route_id = str(event.get("routeId", ""))
        if event.get("eventType") == "route_outcome":
            if event.get("featureSchemaVersion") == FEATURE_SCHEMA_VERSION:
                routes[route_id] = event
        elif event.get("eventType") == "human_label":
            labels[route_id] = event
    samples = []
    for route_id in sorted(routes.keys() & labels.keys()):
        route = routes[route_id]
        label = labels[route_id]
        if route.get("explicitOverride"):
            continue
        if route.get("escalated"):
            continue
        try:
            selector_spec = active_registry.get(str(route.get("selectorModel")), role="direct")
            if not selector_spec.auto_eligible:
                continue
        except ValueError:
            continue
        preferred = label.get("preferredModel")
        try:
            preferred_spec = active_registry.get(str(preferred), role="direct")
            if not preferred_spec.auto_eligible:
                continue
            preferred_tier = preferred_spec.tier
        except ValueError:
            continue
        sample = dict(route)
        sample["preferredModel"] = preferred
        sample["preferredTier"] = preferred_tier
        sample["outcome"] = label.get("outcome")
        sample["evidenceRecordedAt"] = max(
            _feedback_timestamp(route), _feedback_timestamp(label)
        ).astimezone(timezone.utc).isoformat()
        samples.append(sample)
    return samples


def predict_sample(
    sample: dict[str, Any],
    policy: RoutingPolicy,
    benchmark_priors: BenchmarkPriors | None = None,
) -> str:
    """Predict the effective runtime tier, including fixed benchmark guidance."""
    active_priors = benchmark_priors or load_benchmark_priors()
    features = sample.get("features", {})
    strategy = sample.get("strategy")
    effort = sample.get("effort", "medium")
    complexity = int(features.get("complexity_score", 0))
    if bool(features.get("high_risk")):
        return "frontier"
    if bool(features.get("computer_use")):
        predicted = str(active_priors.guidance("computerUse")["minimumTier"])
    elif strategy == "intelligence":
        predicted = (
            "frontier"
            if complexity >= policy.intelligence_frontier_threshold
            or effort in {"xhigh", "max"}
            else "balanced"
        )
    elif strategy == "cost":
        predicted = (
            "balanced"
            if complexity >= policy.cost_balanced_threshold
            or effort in {"xhigh", "max"}
            else "fast"
        )
    elif bool(features.get("validated_bounded")):
        predicted = str(active_priors.guidance("validatedBoundedCoding")["recommendedTier"])
    elif bool(features.get("constrained")):
        predicted = "fast"
    else:
        predicted = (
            "frontier"
            if complexity >= policy.balance_frontier_threshold
            or effort in {"xhigh", "max"}
            else "balanced"
        )

    for active, signal in (
        (bool(features.get("complex_debugging")), "complexDebugging"),
        (bool(features.get("long_context")), "longContext"),
        (bool(features.get("multi_file")), "multiFile"),
    ):
        if not active:
            continue
        minimum_tier = str(active_priors.guidance(signal)["minimumTier"])
        if TIER_RANK[predicted] < TIER_RANK[minimum_tier]:
            predicted = minimum_tier
    return predicted


def score_policy(
    samples: list[dict[str, Any]],
    policy: RoutingPolicy,
    registry: ModelRegistry | None = None,
    benchmark_priors: BenchmarkPriors | None = None,
) -> dict[str, Any]:
    active_registry = registry or DEFAULT_REGISTRY
    active_priors = benchmark_priors or load_benchmark_priors(registry=active_registry)
    exact = 0
    weighted_loss = 0
    false_upgrades = 0
    false_downgrades = 0
    high_risk_violations = 0
    for sample in samples:
        predicted = predict_sample(sample, policy, active_priors)
        preferred = str(
            sample.get("preferredTier")
            or active_registry.tier_for_model(str(sample["preferredModel"]))
        )
        if predicted == preferred:
            exact += 1
        difference = TIER_RANK[predicted] - TIER_RANK[preferred]
        if difference > 0:
            false_upgrades += 1
            weighted_loss += difference
        elif difference < 0:
            false_downgrades += 1
            weighted_loss += abs(difference) * 2
        if bool(sample.get("features", {}).get("high_risk")) and predicted != "frontier":
            high_risk_violations += 1
    return {
        "samples": len(samples),
        "accuracy": (exact / len(samples)) if samples else 0.0,
        "weightedLoss": weighted_loss,
        "falseUpgrades": false_upgrades,
        "falseDowngrades": false_downgrades,
        "highRiskViolations": high_risk_violations,
    }


def _wilson_interval(successes: int, total: int) -> list[float] | None:
    if total <= 0:
        return None
    z = 1.959963984540054
    rate = successes / total
    denominator = 1 + (z * z / total)
    midpoint = rate + (z * z / (2 * total))
    margin = z * math.sqrt((rate * (1 - rate) / total) + (z * z / (4 * total * total)))
    return [
        max(0.0, (midpoint - margin) / denominator),
        min(1.0, (midpoint + margin) / denominator),
    ]


def _paired_shadow_statistics(
    samples: list[dict[str, Any]],
    baseline: RoutingPolicy,
    candidate: RoutingPolicy,
    registry: ModelRegistry,
    benchmark_priors: BenchmarkPriors,
) -> dict[str, Any]:
    baseline_wins = 0
    candidate_wins = 0
    both_correct = 0
    both_wrong = 0
    baseline_correct = 0
    candidate_correct = 0
    for sample in samples:
        preferred = str(
            sample.get("preferredTier")
            or registry.tier_for_model(str(sample["preferredModel"]))
        )
        baseline_match = predict_sample(sample, baseline, benchmark_priors) == preferred
        candidate_match = predict_sample(sample, candidate, benchmark_priors) == preferred
        baseline_correct += int(baseline_match)
        candidate_correct += int(candidate_match)
        if baseline_match and candidate_match:
            both_correct += 1
        elif baseline_match:
            baseline_wins += 1
        elif candidate_match:
            candidate_wins += 1
        else:
            both_wrong += 1
    discordant = baseline_wins + candidate_wins
    if discordant:
        smaller = min(baseline_wins, candidate_wins)
        tail = sum(math.comb(discordant, index) for index in range(smaller + 1))
        p_value = min(1.0, 2 * tail / (2 ** discordant))
    else:
        p_value = 1.0
    return {
        "samples": len(samples),
        "bothCorrect": both_correct,
        "bothWrong": both_wrong,
        "candidateWins": candidate_wins,
        "baselineWins": baseline_wins,
        "netCandidateWins": candidate_wins - baseline_wins,
        "discordantPairs": discordant,
        "twoSidedExactPValue": p_value,
        "baselineAccuracyInterval95": _wilson_interval(baseline_correct, len(samples)),
        "candidateAccuracyInterval95": _wilson_interval(candidate_correct, len(samples)),
    }


def _shadow_strata(
    samples: list[dict[str, Any]],
    baseline: RoutingPolicy,
    candidate: RoutingPolicy,
    registry: ModelRegistry,
    benchmark_priors: BenchmarkPriors,
    *,
    minimum_size: int,
) -> dict[str, Any]:
    dimensions = {
        "strategy": lambda sample: str(sample.get("strategy") or "unknown"),
        "risk": lambda sample: (
            "high-risk" if bool(sample.get("features", {}).get("high_risk")) else "standard"
        ),
        "labelSource": lambda sample: str(sample.get("labelSource") or "human"),
    }
    emitted: dict[str, list[dict[str, Any]]] = {}
    suppressed: dict[str, int] = {}
    for dimension, key_function in dimensions.items():
        grouped: dict[str, list[dict[str, Any]]] = {}
        for sample in samples:
            grouped.setdefault(key_function(sample), []).append(sample)
        rows: list[dict[str, Any]] = []
        suppressed[dimension] = 0
        for key in sorted(grouped):
            group = grouped[key]
            if len(group) < minimum_size:
                suppressed[dimension] += 1
                continue
            baseline_score = score_policy(group, baseline, registry, benchmark_priors)
            candidate_score = score_policy(group, candidate, registry, benchmark_priors)
            changed = sum(
                predict_sample(sample, baseline, benchmark_priors)
                != predict_sample(sample, candidate, benchmark_priors)
                for sample in group
            )
            rows.append(
                {
                    "value": key,
                    "samples": len(group),
                    "changedRoutes": changed,
                    "baselineAccuracy": baseline_score["accuracy"],
                    "candidateAccuracy": candidate_score["accuracy"],
                    "accuracyDelta": candidate_score["accuracy"] - baseline_score["accuracy"],
                    "baselineWeightedLoss": baseline_score["weightedLoss"],
                    "candidateWeightedLoss": candidate_score["weightedLoss"],
                    "weightedLossDelta": (
                        candidate_score["weightedLoss"] - baseline_score["weightedLoss"]
                    ),
                }
            )
        emitted[dimension] = rows
    return {
        "minimumStratumSize": minimum_size,
        "dimensions": emitted,
        "suppressedStrata": suppressed,
        "storesRouteIds": False,
        "storesTaskText": False,
    }


def shadow_policy_comparison(
    samples: list[dict[str, Any]],
    baseline: RoutingPolicy,
    candidate: RoutingPolicy,
    registry: ModelRegistry | None = None,
    benchmark_priors: BenchmarkPriors | None = None,
) -> dict[str, Any]:
    """Compare two policies on the same evidence without returning route identifiers."""
    active_registry = registry or DEFAULT_REGISTRY
    active_priors = benchmark_priors or load_benchmark_priors(registry=active_registry)
    if any(
        sample.get("featureSchemaVersion") != FEATURE_SCHEMA_VERSION
        for sample in samples
    ):
        raise ValueError("shadow samples must use the current routing feature schema")
    holdout = _split_samples(samples)[1] if len(samples) >= 4 else list(samples)
    baseline_all = score_policy(samples, baseline, active_registry, active_priors)
    candidate_all = score_policy(samples, candidate, active_registry, active_priors)
    baseline_holdout = score_policy(holdout, baseline, active_registry, active_priors)
    candidate_holdout = score_policy(holdout, candidate, active_registry, active_priors)
    changed_routes = sum(
        predict_sample(sample, baseline, active_priors)
        != predict_sample(sample, candidate, active_priors)
        for sample in samples
    )
    minimum_evidence = 8
    minimum_accuracy_gain = 0.02
    minimum_discordant_pairs = 4
    maximum_p_value = 0.10
    all_accuracy_delta = candidate_all["accuracy"] - baseline_all["accuracy"]
    all_loss_delta = candidate_all["weightedLoss"] - baseline_all["weightedLoss"]
    paired_all = _paired_shadow_statistics(
        samples, baseline, candidate, active_registry, active_priors
    )
    paired_holdout = _paired_shadow_statistics(
        holdout, baseline, candidate, active_registry, active_priors
    )
    minimum_effect_met = (
        all_accuracy_delta >= minimum_accuracy_gain or all_loss_delta <= -1
    )
    statistically_supported = (
        paired_all["discordantPairs"] >= minimum_discordant_pairs
        and paired_all["candidateWins"] > paired_all["baselineWins"]
        and paired_all["twoSidedExactPValue"] <= maximum_p_value
    )
    guardrails = {
        "minimumEvidenceMet": len(samples) >= minimum_evidence,
        "noHighRiskViolationRegression": (
            candidate_holdout["highRiskViolations"]
            <= baseline_holdout["highRiskViolations"]
        ),
        "noFalseDowngradeRegression": (
            candidate_holdout["falseDowngrades"]
            <= baseline_holdout["falseDowngrades"]
        ),
        "holdoutAccuracyNonDecreasing": (
            candidate_holdout["accuracy"] >= baseline_holdout["accuracy"]
        ),
        "holdoutWeightedLossNonIncreasing": (
            candidate_holdout["weightedLoss"] <= baseline_holdout["weightedLoss"]
        ),
    }
    if not guardrails["minimumEvidenceMet"]:
        assessment = "insufficient-evidence"
    elif not all(guardrails.values()):
        assessment = "regression"
    elif changed_routes == 0:
        assessment = "no-routing-difference"
    elif minimum_effect_met and statistically_supported:
        assessment = "candidate-favorable"
    elif minimum_effect_met:
        assessment = "promising-unconfirmed"
    else:
        assessment = "neutral"
    return {
        "schema": "agent-auto-router.policy-shadow.v1",
        "assessment": assessment,
        "dataset": {
            "samples": len(samples),
            "holdoutSamples": len(holdout),
            "changedRoutes": changed_routes,
            "storesRouteIds": False,
            "storesTaskText": False,
        },
        "baseline": {
            "policyDigest": policy_digest(baseline),
            "allEvidence": baseline_all,
            "holdout": baseline_holdout,
        },
        "candidate": {
            "policyDigest": policy_digest(candidate),
            "allEvidence": candidate_all,
            "holdout": candidate_holdout,
        },
        "delta": {
            "allEvidenceAccuracy": all_accuracy_delta,
            "allEvidenceWeightedLoss": all_loss_delta,
            "holdoutAccuracy": (
                candidate_holdout["accuracy"] - baseline_holdout["accuracy"]
            ),
            "holdoutWeightedLoss": (
                candidate_holdout["weightedLoss"] - baseline_holdout["weightedLoss"]
            ),
        },
        "confidence": {
            "minimumEvidence": minimum_evidence,
            "minimumAccuracyGain": minimum_accuracy_gain,
            "minimumDiscordantPairs": minimum_discordant_pairs,
            "maximumPValue": maximum_p_value,
            "minimumEffectMet": minimum_effect_met,
            "statisticallySupported": statistically_supported,
            "allEvidence": paired_all,
            "holdout": paired_holdout,
        },
        "strata": _shadow_strata(
            samples,
            baseline,
            candidate,
            active_registry,
            active_priors,
            minimum_size=3,
        ),
        "guardrails": guardrails,
        "activationAuthorized": False,
        "modelCalls": 0,
    }


def _split_samples(samples: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    ordered = sorted(
        samples,
        key=lambda item: hashlib.sha256(str(item.get("routeId", "")).encode("utf-8")).hexdigest(),
    )
    validation_count = max(3, math.ceil(len(ordered) * 0.25))
    validation_count = min(validation_count, len(ordered) - 1)
    return ordered[validation_count:], ordered[:validation_count]


def _candidate_policies(base: RoutingPolicy) -> Iterable[RoutingPolicy]:
    for intelligence, balance, cost in itertools.product(range(1, 9), repeat=3):
        yield RoutingPolicy(
            policy_version="candidate",
            intelligence_frontier_threshold=intelligence,
            balance_frontier_threshold=balance,
            cost_balanced_threshold=cost,
        )


def build_candidate(
    samples: list[dict[str, Any]],
    base_policy: RoutingPolicy,
    *,
    min_labels: int = 20,
    min_validation_accuracy_gain: float = 0.05,
    max_threshold_step: int | None = None,
    conservative_only: bool = False,
    requires_human_approval: bool = True,
    registry: ModelRegistry | None = None,
    benchmark_priors: BenchmarkPriors | None = None,
) -> dict[str, Any]:
    active_registry = registry or DEFAULT_REGISTRY
    active_priors = benchmark_priors or load_benchmark_priors(registry=active_registry)
    if min_labels < 4:
        raise ValueError("min_labels must be at least 4")
    if not 0 <= min_validation_accuracy_gain <= 1:
        raise ValueError("min_validation_accuracy_gain must be between 0 and 1")
    if max_threshold_step is not None and (
        isinstance(max_threshold_step, bool) or not 1 <= max_threshold_step <= 7
    ):
        raise ValueError("max_threshold_step must be between 1 and 7")
    if len(samples) < min_labels:
        raise ValueError(f"at least {min_labels} labeled routes are required; found {len(samples)}")
    if any(
        sample.get("featureSchemaVersion") != FEATURE_SCHEMA_VERSION
        for sample in samples
    ):
        raise ValueError(
            "all candidate samples must use the current routing feature schema"
        )
    training, validation = _split_samples(samples)
    base_training = score_policy(training, base_policy, active_registry, active_priors)

    def rank(policy: RoutingPolicy) -> tuple[float, float, int]:
        score = score_policy(training, policy, active_registry, active_priors)
        distance = (
            abs(policy.intelligence_frontier_threshold - base_policy.intelligence_frontier_threshold)
            + abs(policy.balance_frontier_threshold - base_policy.balance_frontier_threshold)
            + abs(policy.cost_balanced_threshold - base_policy.cost_balanced_threshold)
        )
        return (float(score["weightedLoss"]), -float(score["accuracy"]), distance)

    policies = list(_candidate_policies(base_policy))
    if max_threshold_step is not None:
        base_thresholds = (
            base_policy.intelligence_frontier_threshold,
            base_policy.balance_frontier_threshold,
            base_policy.cost_balanced_threshold,
        )
        policies = [
            policy
            for policy in policies
            if all(
                abs(candidate - base) <= max_threshold_step
                for candidate, base in zip(
                    (
                        policy.intelligence_frontier_threshold,
                        policy.balance_frontier_threshold,
                        policy.cost_balanced_threshold,
                    ),
                    base_thresholds,
                )
            )
            and (
                not conservative_only
                or (
                    policy.intelligence_frontier_threshold
                    <= base_policy.intelligence_frontier_threshold
                    and policy.balance_frontier_threshold
                    <= base_policy.balance_frontier_threshold
                    and policy.cost_balanced_threshold
                    <= base_policy.cost_balanced_threshold
                )
            )
        ]
    if not policies:
        raise ValueError("candidate constraints leave no routing policies to evaluate")
    best = min(policies, key=rank)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    best = RoutingPolicy(
        policy_version=f"candidate-{timestamp}",
        intelligence_frontier_threshold=best.intelligence_frontier_threshold,
        balance_frontier_threshold=best.balance_frontier_threshold,
        cost_balanced_threshold=best.cost_balanced_threshold,
    )
    candidate_training = score_policy(training, best, active_registry, active_priors)
    base_validation = score_policy(validation, base_policy, active_registry, active_priors)
    candidate_validation = score_policy(validation, best, active_registry, active_priors)
    accuracy_gain = candidate_validation["accuracy"] - base_validation["accuracy"]
    high_risk_model = active_registry.resolve_tier(
        "frontier", role="direct", required_capabilities=("high-risk-primary",)
    )
    safety_checks = {
        "thresholdsValid": True,
        "trustedRegistryValid": bool(active_registry.enabled_model_ids),
        "highRiskPrimaryAvailable": bool(high_risk_model.model_id),
        "highRiskAlwaysFrontier": all(
            predict_sample({
                "strategy": strategy,
                "effort": "medium",
                "features": {"high_risk": True, "complexity_score": 0, "constrained": True},
                "preferredTier": "frontier",
            }, best, active_priors) == "frontier"
            for strategy in STRATEGIES
        ),
        "validationHighRiskViolations": candidate_validation["highRiskViolations"] == 0,
    }
    eligible = (
        all(safety_checks.values())
        and accuracy_gain >= min_validation_accuracy_gain
        and candidate_validation["weightedLoss"] < base_validation["weightedLoss"]
        and candidate_validation["falseDowngrades"] <= base_validation["falseDowngrades"]
    )
    candidate: dict[str, Any] = {
        "schemaVersion": CANDIDATE_SCHEMA_VERSION,
        "featureSchemaVersion": FEATURE_SCHEMA_VERSION,
        "createdAt": utc_now(),
        "basePolicyDigest": policy_digest(base_policy),
        "modelRegistryDigest": registry_digest(active_registry),
        "benchmarkPriorsDigest": benchmark_priors_digest(active_priors),
        "basePolicy": policy_to_dict(base_policy),
        "policy": policy_to_dict(best),
        "dataset": {
            "labeledRoutes": len(samples),
            "trainingRoutes": len(training),
            "validationRoutes": len(validation),
            "routeIdsDigest": _canonical_digest({"routeIds": sorted(str(item["routeId"]) for item in samples)}),
            "storesTaskText": False,
        },
        "evaluation": {
            "training": {"baseline": base_training, "candidate": candidate_training},
            "validation": {"baseline": base_validation, "candidate": candidate_validation},
            "validationAccuracyGain": accuracy_gain,
            "minimumRequiredAccuracyGain": min_validation_accuracy_gain,
        },
        "safetyChecks": safety_checks,
        "eligibleForApproval": eligible,
        "requiresHumanApproval": requires_human_approval,
        "modelCalls": 0,
        "optimizerSettings": {
            "minimumLabels": min_labels,
            "minimumValidationAccuracyGain": min_validation_accuracy_gain,
            "maximumThresholdStep": max_threshold_step,
            "conservativeOnly": conservative_only,
        },
    }
    candidate["candidateId"] = _canonical_digest(candidate)
    return candidate


def prepare_policy_archive(
    state_dir: Path, policy: RoutingPolicy, reason: str
) -> tuple[Path, dict[str, Any]]:
    payload = policy_to_dict(policy)
    payload["archivedAt"] = utc_now()
    payload["archiveReason"] = reason
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    path = ControlPlanePaths(state_dir).history / f"{stamp}-{policy_digest(policy)[:12]}.json"
    return path, payload


def _approve_candidate_unlocked(
    candidate_path: Path,
    state_dir: Path,
    approved_by: str,
    feedback_path: Path | None = None,
    registry: ModelRegistry | None = None,
) -> dict[str, Any]:
    recover_pending_transaction(state_dir)
    active_registry = registry or DEFAULT_REGISTRY
    candidate = json.loads(candidate_path.read_text(encoding="utf-8"))
    if not isinstance(candidate, dict) or candidate.get("schemaVersion") != CANDIDATE_SCHEMA_VERSION:
        raise ValueError("unsupported policy candidate")
    if candidate.get("featureSchemaVersion") != FEATURE_SCHEMA_VERSION:
        raise ValueError(
            "candidate is stale because the routing feature schema has changed"
        )
    if not candidate.get("eligibleForApproval") or not all(candidate.get("safetyChecks", {}).values()):
        raise ValueError("candidate is not eligible for approval")
    candidate_id = candidate.get("candidateId")
    unsigned = dict(candidate)
    unsigned.pop("candidateId", None)
    if candidate_id != _canonical_digest(unsigned):
        raise ValueError("candidate integrity check failed")
    current, _ = load_active_policy(state_dir)
    if candidate.get("basePolicyDigest") != policy_digest(current):
        raise ValueError("candidate is stale because the active policy has changed")
    if candidate.get("modelRegistryDigest") != registry_digest(active_registry):
        raise ValueError("candidate is stale because the trusted model registry has changed")
    active_priors = load_benchmark_priors(registry=active_registry)
    if candidate.get("benchmarkPriorsDigest") != benchmark_priors_digest(active_priors):
        raise ValueError("candidate is stale because the benchmark priors have changed")
    next_policy = policy_from_dict(candidate["policy"])
    settings = candidate.get("optimizerSettings", {})
    if not isinstance(settings, dict):
        raise ValueError("candidate optimizer settings are invalid")
    samples = labeled_samples(
        read_feedback(feedback_path or default_feedback_path(state_dir)), active_registry
    )
    rebuilt = build_candidate(
        samples,
        current,
        min_labels=int(settings.get("minimumLabels", 20)),
        min_validation_accuracy_gain=float(settings.get("minimumValidationAccuracyGain", 0.05)),
        max_threshold_step=settings.get("maximumThresholdStep"),
        conservative_only=bool(settings.get("conservativeOnly", False)),
        requires_human_approval=bool(candidate.get("requiresHumanApproval", True)),
        registry=active_registry,
        benchmark_priors=active_priors,
    )
    expected_thresholds = rebuilt["policy"]["thresholds"]
    if candidate["policy"].get("thresholds") != expected_thresholds:
        raise ValueError("candidate no longer matches the optimizer result for current feedback")
    for key in ("evaluation", "safetyChecks", "eligibleForApproval"):
        if candidate.get(key) != rebuilt.get(key):
            raise ValueError(f"candidate {key} failed live revalidation")
    archived, archived_payload = prepare_policy_archive(state_dir, current, "approval")
    active_payload = policy_to_dict(next_policy)
    active_payload["activation"] = {
        "activatedAt": utc_now(),
        "approvedBy": approved_by,
        "candidateId": candidate_id,
    }
    audit = {
        "eventType": "policy_approved",
        "recordedAt": utc_now(),
        "approvedBy": approved_by,
        "candidateId": candidate_id,
        "fromDigest": policy_digest(current),
        "toDigest": policy_digest(next_policy),
        "rollbackSnapshot": str(archived),
    }
    transaction_id = commit_control_plane_transaction(
        state_dir,
        operation="manual-approval",
        writes=(
            (archived, archived_payload),
            (ControlPlanePaths(state_dir).active_policy, active_payload),
        ),
        audit_events=(audit,),
    )
    audit["transactionId"] = transaction_id
    return audit


def approve_candidate(
    candidate_path: Path,
    state_dir: Path,
    approved_by: str,
    feedback_path: Path | None = None,
    registry: ModelRegistry | None = None,
) -> dict[str, Any]:
    with control_plane_lock(state_dir, timeout_seconds=5) as acquired:
        if not acquired:
            raise RuntimeError("routing control plane is busy")
        return _approve_candidate_unlocked(
            candidate_path,
            state_dir,
            approved_by,
            feedback_path,
            registry,
        )


def _rollback_policy_unlocked(state_dir: Path, approved_by: str) -> dict[str, Any]:
    recover_pending_transaction(state_dir)
    current, _ = load_active_policy(state_dir)
    paths = ControlPlanePaths(state_dir)
    history_dir = resolve_control_plane_path(state_dir, paths.history)
    choices: list[tuple[str, Path, RoutingPolicy]] = []
    for path in history_dir.glob("*.json") if history_dir.is_dir() else []:
        policy = policy_from_dict(json.loads(path.read_text(encoding="utf-8")))
        if policy_digest(policy) != policy_digest(current):
            choices.append((path.name, path, policy))
    if not choices:
        raise ValueError("no previous policy version is available for rollback")
    _, selected_path, previous = sorted(choices, reverse=True)[0]
    archived, archived_payload = prepare_policy_archive(state_dir, current, "rollback")
    active_payload = policy_to_dict(previous)
    active_payload["activation"] = {
        "activatedAt": utc_now(),
        "approvedBy": approved_by,
        "rollbackFromDigest": policy_digest(current),
        "rollbackSource": str(selected_path),
    }
    audit = {
        "eventType": "policy_rolled_back",
        "recordedAt": utc_now(),
        "approvedBy": approved_by,
        "fromDigest": policy_digest(current),
        "toDigest": policy_digest(previous),
        "archivedPolicy": str(archived),
    }
    transaction_id = commit_control_plane_transaction(
        state_dir,
        operation="manual-rollback",
        writes=((archived, archived_payload), (paths.active_policy, active_payload)),
        audit_events=(audit,),
    )
    audit["transactionId"] = transaction_id
    return audit


def rollback_policy(state_dir: Path, approved_by: str) -> dict[str, Any]:
    with control_plane_lock(state_dir, timeout_seconds=5) as acquired:
        if not acquired:
            raise RuntimeError("routing control plane is busy")
        return _rollback_policy_unlocked(state_dir, approved_by)


def _read_stdin_object() -> dict[str, Any]:
    payload = json.load(sys.stdin)
    if not isinstance(payload, dict):
        raise ValueError("stdin must contain one JSON object")
    return payload


def main() -> int:
    registry = load_model_registry()
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    record = subparsers.add_parser("record", help="append a privacy-minimized route outcome")
    record.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR)
    record.add_argument("--feedback-file", type=Path)
    record.add_argument("--stdin", action="store_true", required=True)

    label = subparsers.add_parser("label", help="attach a human preferred-model label")
    label.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR)
    label.add_argument("--feedback-file", type=Path)
    label.add_argument("--route-id", required=True)
    label.add_argument("--preferred-model", choices=registry.enabled_model_ids, required=True)
    label.add_argument("--outcome", choices=("pass", "partial", "fail"), required=True)
    label.add_argument("--min-labels", type=int, default=20)
    label.add_argument("--min-validation-accuracy-gain", type=float, default=0.05)
    label.add_argument("--no-auto-propose", action="store_true")

    propose = subparsers.add_parser("propose", help="build a gated threshold candidate")
    propose.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR)
    propose.add_argument("--feedback-file", type=Path)
    propose.add_argument("--output", type=Path, required=True)
    propose.add_argument("--min-labels", type=int, default=20)
    propose.add_argument("--min-validation-accuracy-gain", type=float, default=0.05)

    approve = subparsers.add_parser("approve", help="explicitly activate an eligible candidate")
    approve.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR)
    approve.add_argument("--candidate", type=Path, required=True)
    approve.add_argument("--approved-by", required=True)
    approve.add_argument("--feedback-file", type=Path)

    rollback = subparsers.add_parser("rollback", help="restore the latest previous policy")
    rollback.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR)
    rollback.add_argument("--approved-by", required=True)

    status = subparsers.add_parser("status", help="show active policy and feedback counts")
    status.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR)
    status.add_argument("--feedback-file", type=Path)

    args = parser.parse_args()
    state_dir: Path = args.state_dir
    feedback_path = getattr(args, "feedback_file", None) or default_feedback_path(state_dir)

    if args.command == "record":
        payload = _read_stdin_object()
        normalize_route_event(payload, registry)
        from guarded_auto import (
            feedback_recording_enabled,
            learning_mode,
            process_recorded_outcome,
        )

        mode = learning_mode(state_dir)
        if not feedback_recording_enabled(state_dir):
            result = {
                "recorded": False,
                "reason": "feedback-disabled",
                "learningMode": mode,
                "guardedAuto": {
                    "status": mode,
                    "action": "none",
                    "modelCalls": 0,
                },
            }
        else:
            event = append_route_event(payload, feedback_path, registry)
            result = dict(event)
            result["recorded"] = True
            result["learningMode"] = mode
            result["guardedAuto"] = process_recorded_outcome(
                state_dir, feedback_path, registry=registry
            )
    elif args.command == "label":
        label_event = append_label_event(
            args.route_id, args.preferred_model, args.outcome, feedback_path, registry
        )
        result = {"label": label_event, "autoProposal": None}
        samples = labeled_samples(read_feedback(feedback_path), registry)
        if not args.no_auto_propose and len(samples) >= args.min_labels:
            policy, source = load_active_policy(state_dir)
            candidate = build_candidate(
                samples,
                policy,
                min_labels=args.min_labels,
                min_validation_accuracy_gain=args.min_validation_accuracy_gain,
                registry=registry,
            )
            candidate["basePolicySource"] = source
            unsigned = dict(candidate)
            unsigned.pop("candidateId", None)
            candidate["candidateId"] = _canonical_digest(unsigned)
            candidate_path = (
                state_dir / "candidates" / f"candidate-{candidate['candidateId'][:16]}.json"
            )
            _atomic_write_json(candidate_path, candidate)
            result["autoProposal"] = {
                "path": str(candidate_path),
                "candidateId": candidate["candidateId"],
                "eligibleForApproval": candidate["eligibleForApproval"],
                "requiresHumanApproval": True,
            }
        from guarded_auto import process_recorded_outcome

        result["guardedAuto"] = process_recorded_outcome(
            state_dir, feedback_path, registry=registry
        )
    elif args.command == "propose":
        policy, source = load_active_policy(state_dir)
        samples = labeled_samples(read_feedback(feedback_path), registry)
        result = build_candidate(
            samples,
            policy,
            min_labels=args.min_labels,
            min_validation_accuracy_gain=args.min_validation_accuracy_gain,
            registry=registry,
        )
        result["basePolicySource"] = source
        unsigned = dict(result)
        unsigned.pop("candidateId", None)
        result["candidateId"] = _canonical_digest(unsigned)
        _atomic_write_json(args.output, result)
    elif args.command == "approve":
        result = approve_candidate(
            args.candidate,
            state_dir,
            args.approved_by,
            args.feedback_file or default_feedback_path(state_dir),
            registry,
        )
    elif args.command == "rollback":
        result = rollback_policy(state_dir, args.approved_by)
    else:
        policy, source = load_active_policy(state_dir)
        events = read_feedback(feedback_path)
        result = {
            "activePolicy": policy_to_dict(policy),
            "activePolicyDigest": policy_digest(policy),
            "activePolicySource": source,
            "feedbackFile": str(feedback_path),
            "routeOutcomes": sum(event.get("eventType") == "route_outcome" for event in events),
            "humanLabels": sum(event.get("eventType") == "human_label" for event in events),
            "labeledRoutes": len(labeled_samples(events, registry)),
            "modelRegistryDigest": registry_digest(registry),
            "enabledModels": list(registry.enabled_model_ids),
            "autoModels": list(registry.auto_model_ids),
            "historyVersions": len(list((state_dir / "history").glob("*.json"))) if (state_dir / "history").is_dir() else 0,
            "storesTaskText": False,
            "efficiency": summarize_feedback(events),
        }
    print(json.dumps(result, ensure_ascii=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
