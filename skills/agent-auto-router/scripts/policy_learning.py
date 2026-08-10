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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from efficiency_metrics import summarize_feedback

from benchmark_priors import (
    BenchmarkPriors,
    benchmark_priors_digest,
    load_benchmark_priors,
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

FEEDBACK_SCHEMA_VERSION = 3
CANDIDATE_SCHEMA_VERSION = 1
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


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def default_feedback_path(state_dir: Path) -> Path:
    return state_dir / "feedback.jsonl"


def _canonical_digest(payload: dict[str, Any]) -> str:
    data = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
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


def _append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    with append_lock(path) as acquired:
        if not acquired:
            raise RuntimeError(f"timed out waiting to append router state: {path.name}")
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(line + "\n")
            handle.flush()
            os.fsync(handle.fileno())


def _reject_sensitive_keys(value: Any, parent_key: str | None = None) -> None:
    if isinstance(value, dict):
        for key, nested in value.items():
            normalized_key = str(key).lower()
            if parent_key != "observed_tokens" and normalized_key in FORBIDDEN_STORED_KEYS:
                raise ValueError(f"feedback payload may not store field: {key}")
            _reject_sensitive_keys(nested, normalized_key)
    elif isinstance(value, list):
        for nested in value:
            _reject_sensitive_keys(nested, parent_key)


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
    observed_tokens = payload.get("observed_tokens")
    normalized_tokens: dict[str, int] | None = None
    if observed_tokens is not None:
        if not isinstance(observed_tokens, dict):
            raise ValueError("observed_tokens must be an object or null")
        supported_token_fields = {
            "input", "cached_input", "output", "reasoning_output", "total"
        }
        if set(observed_tokens) - supported_token_fields:
            raise ValueError("observed_tokens contains unsupported fields")
        normalized_tokens = {}
        for key in ("input", "cached_input", "output", "reasoning_output", "total"):
            value = observed_tokens.get(key, 0)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"observed token count {key} must be a non-negative integer")
            normalized_tokens[key] = value
        if normalized_tokens["total"] != normalized_tokens["input"] + normalized_tokens["output"]:
            raise ValueError("observed total tokens must equal input plus output")
        if normalized_tokens["cached_input"] > normalized_tokens["input"]:
            raise ValueError("cached input tokens may not exceed input tokens")
        if normalized_tokens["reasoning_output"] > normalized_tokens["output"]:
            raise ValueError("reasoning output tokens may not exceed output tokens")
    exit_code = int(payload["exit_code"])
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
        "exitCode": exit_code,
        "executionSucceeded": exit_code == 0,
        "durationMs": max(0, int(payload["duration_ms"])),
        "observedTokens": normalized_tokens,
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


def _archive_policy(state_dir: Path, policy: RoutingPolicy, reason: str) -> Path:
    payload = policy_to_dict(policy)
    payload["archivedAt"] = utc_now()
    payload["archiveReason"] = reason
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    path = state_dir / "history" / f"{stamp}-{policy_digest(policy)[:12]}.json"
    _atomic_write_json(path, payload)
    return path


def _approve_candidate_unlocked(
    candidate_path: Path,
    state_dir: Path,
    approved_by: str,
    feedback_path: Path | None = None,
    registry: ModelRegistry | None = None,
) -> dict[str, Any]:
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
    archived = _archive_policy(state_dir, current, "approval")
    active_payload = policy_to_dict(next_policy)
    active_payload["activation"] = {
        "activatedAt": utc_now(),
        "approvedBy": approved_by,
        "candidateId": candidate_id,
    }
    _atomic_write_json(state_dir / "active-policy.json", active_payload)
    audit = {
        "eventType": "policy_approved",
        "recordedAt": utc_now(),
        "approvedBy": approved_by,
        "candidateId": candidate_id,
        "fromDigest": policy_digest(current),
        "toDigest": policy_digest(next_policy),
        "rollbackSnapshot": str(archived),
    }
    _append_jsonl(state_dir / "audit.jsonl", audit)
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
    current, _ = load_active_policy(state_dir)
    history_dir = state_dir / "history"
    choices: list[tuple[str, Path, RoutingPolicy]] = []
    for path in history_dir.glob("*.json") if history_dir.is_dir() else []:
        policy = policy_from_dict(json.loads(path.read_text(encoding="utf-8")))
        if policy_digest(policy) != policy_digest(current):
            choices.append((path.name, path, policy))
    if not choices:
        raise ValueError("no previous policy version is available for rollback")
    _, selected_path, previous = sorted(choices, reverse=True)[0]
    archived = _archive_policy(state_dir, current, "rollback")
    active_payload = policy_to_dict(previous)
    active_payload["activation"] = {
        "activatedAt": utc_now(),
        "approvedBy": approved_by,
        "rollbackFromDigest": policy_digest(current),
        "rollbackSource": str(selected_path),
    }
    _atomic_write_json(state_dir / "active-policy.json", active_payload)
    audit = {
        "eventType": "policy_rolled_back",
        "recordedAt": utc_now(),
        "approvedBy": approved_by,
        "fromDigest": policy_digest(current),
        "toDigest": policy_digest(previous),
        "archivedPolicy": str(archived),
    }
    _append_jsonl(state_dir / "audit.jsonl", audit)
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
        event = append_route_event(_read_stdin_object(), feedback_path, registry)
        from guarded_auto import process_recorded_outcome

        result = dict(event)
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
