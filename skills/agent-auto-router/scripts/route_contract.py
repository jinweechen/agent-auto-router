#!/usr/bin/env python3
"""Canonical privacy-safe route-decision output shared by all entrypoints."""

from __future__ import annotations

import copy
import hashlib
import hmac
import json
import re
import secrets
from typing import Any, Mapping

from model_registry import ModelRegistry, registry_digest


ROUTE_DECISION_SCHEMA = "agent-auto-router.route-decision.v2"
TASK_BINDING_SCHEMA = "agent-auto-router.task-binding.v1"
EXECUTION_ENVELOPE_SCHEMA = "agent-auto-router.execution-envelope.v1"
HOST_REQUEST_SCHEMA = "agent-auto-router.host-request.v1"
MAX_ROUTE_DECISION_BYTES = 64 * 1024
MAX_TASK_BYTES = 4 * 1024 * 1024
WORKSPACE_KEY_PATTERN = re.compile(r"[0-9a-f]{64}")
NONCE_PATTERN = re.compile(r"[0-9a-f]{32}")
SAFE_ID_PATTERN = re.compile(r"[A-Za-z0-9._:-]{1,200}")
STRATEGIES = frozenset({"intelligence", "balance", "cost"})
EFFORTS = frozenset({"none", "low", "medium", "high", "xhigh", "max"})
TIERS = frozenset({"fast", "balanced", "frontier"})
VARIANT_TOPOLOGY = {
    "A": "direct", "B": "orchestrated", "C": "orchestrated",
    "D": "orchestrated", "E": "direct", "F": "direct",
}
VARIANT_MAX_MODEL_CALLS = {"A": 2, "B": 6, "C": 7, "D": 5, "E": 1, "F": 1}
REPOSITORY_METADATA_FIELDS = frozenset({
    "repo_files", "source_files", "test_files", "language_count",
    "manifest_count", "large_repo", "monorepo", "dirty_worktree",
    "is_git_repo", "task_has_path_hint", "scan_truncated",
    "scan_duration_ms", "inspection_disabled", "candidate_files",
    "context_chars", "context_useful",
})
FORBIDDEN_CONTENT_FIELDS = frozenset({
    "task", "prompt", "input", "output", "content", "credential",
    "credentials", "secret", "api_key", "files", "file", "path", "workdir",
})
ROUTE_FIELDS = frozenset({
    "schema", "routeId", "strategy", "effort", "selectedModel",
    "selectedTier", "selectorModel", "targetTier", "reasonCode",
    "explicitOverride", "requiredCapabilities", "taskBinding", "workspaceKey",
    "modelAffinity", "featureSchemaVersion", "features", "matchedSignals",
    "repository", "executionPlan", "policy", "registry", "modelCalls",
})
FEATURE_FIELDS = frozenset({
    "prompt_chars", "criteria_count", "high_risk_hits", "risk_action_hits",
    "complex_hits", "simple_hits", "scope_hits", "algorithm_hits",
    "debugging_hits", "long_context_hits", "multi_file_hits",
    "computer_use_hits", "complexity_score", "risk_score", "clarity_score",
    "high_risk", "constrained", "parallelizable", "dependency_ambiguity",
    "orchestration_eligible", "complex_debugging", "long_context",
    "multi_file", "computer_use", "validation_configured",
    "validated_bounded", "benchmark_prior_version", "benchmark_prior_as_of",
    "benchmark_prior_digest", "benchmark_signals",
})
FEATURE_BOOLEAN_FIELDS = frozenset({
    "high_risk", "constrained", "parallelizable", "dependency_ambiguity",
    "orchestration_eligible", "complex_debugging", "long_context",
    "multi_file", "computer_use", "validation_configured", "validated_bounded",
})
MATCHED_SIGNAL_FIELDS = frozenset({
    "complexity", "risk", "riskAction", "sensitiveDomain",
    "inherentHighRisk", "parallel", "ambiguity", "simple", "scope",
    "algorithm", "debugging", "longContext", "multiFile", "computerUse",
})
AFFINITY_FIELDS = frozenset({
    "schema", "mode", "workspaceKey", "storesWorkspacePath", "selectorModel",
    "selectedModel", "targetTier", "selectedTier", "applied", "reason",
    "retainedStrongerTier", "previousModel", "previousModelAgeSeconds",
    "roleModelPolicy", "evidence", "previousModelEvidence", "errorType",
    "modelCalls",
})
AFFINITY_EVIDENCE_FIELDS = frozenset({
    "samples", "inputTokens", "cachedInputTokens", "cacheWriteInputTokens",
    "cacheReadRatio", "cacheWriteRatio", "cacheSignalRatio",
    "billingCostEstimated",
})
EXECUTION_PLAN_FIELDS = frozenset({
    "model", "requiredTier", "selectedTier", "effort", "effortSource",
    "topology", "variant",
    "variantSource", "orchestrationPolicy", "roleModelPolicy", "modelAffinity",
    "orchestrationRecommendation", "context", "graderPolicy", "maxModelCalls",
    "escalation",
})
RECOMMENDATION_FIELDS = frozenset({
    "eligible", "recommended", "recommendedTopology", "recommendedVariant",
    "estimatedMaximumModelCalls", "requiresExplicitOptIn", "utility",
    "blockedByUtilityGate", "blockedByRiskGate", "highRiskConfirmationProvided",
    "reason",
})
UTILITY_FIELDS = frozenset({
    "score", "minimumScore", "passes", "benefitPoints", "overheadPoints",
    "estimatedAdditionalModelCalls", "estimatedRoleTierSwitches",
    "estimatedProfileTierSwitches", "roleModelPolicy", "cacheSignalRatio",
    "sessionBoundaryOverheadPoints", "billingCostEstimated", "components",
})
UTILITY_COMPONENT_FIELDS = frozenset({
    "independentParallelScale", "complexity", "acceptanceCriteria", "debugging",
    "longContext", "multiFile", "dependencyCoordination", "largePrompt", "scope",
})
CONTEXT_FIELDS = frozenset({
    "profile", "repoMapTokens", "maxCandidateFiles", "maxToolOutputChars",
})
ESCALATION_FIELDS = frozenset({
    "eligible", "nextTier", "requiresExplicitOptIn", "nextModel", "nextEffort",
    "unavailableReason", "reason",
})


def _reject_unknown_fields(
    value: Mapping[str, Any], allowed: frozenset[str], owner: str
) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ValueError(
            f"route {owner} contains unsupported fields: " + ", ".join(unknown)
        )


def _validate_nonnegative_integer(value: Any, owner: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"route {owner} must be a non-negative integer")


def _validate_ratio(value: Any, owner: str) -> None:
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"route {owner} must be a numeric ratio or null")
    if not 0 <= float(value) <= 1:
        raise ValueError(f"route {owner} must be between zero and one")


def _validate_features(features: Mapping[str, Any]) -> dict[str, Any]:
    value = copy.deepcopy(dict(features))
    _reject_unknown_fields(value, FEATURE_FIELDS, "features")
    for key, item in value.items():
        if key in FEATURE_BOOLEAN_FIELDS:
            if not isinstance(item, bool):
                raise ValueError(f"route feature {key} must be boolean")
        elif key == "benchmark_prior_digest":
            if not WORKSPACE_KEY_PATTERN.fullmatch(str(item)):
                raise ValueError("route benchmark_prior_digest must be SHA-256")
        elif key == "benchmark_signals":
            if not isinstance(item, (list, tuple)) or len(item) > 32:
                raise ValueError("route benchmark_signals must be a bounded array")
            if any(not SAFE_ID_PATTERN.fullmatch(str(signal)) for signal in item):
                raise ValueError("route benchmark_signals contains an invalid identifier")
            value[key] = [str(signal) for signal in item]
        elif key in {"benchmark_prior_version", "benchmark_prior_as_of"}:
            if not SAFE_ID_PATTERN.fullmatch(str(item)):
                raise ValueError(f"route feature {key} must be a safe identifier")
        else:
            _validate_nonnegative_integer(item, f"feature {key}")
    return value


def _validate_matched_signals(signals: Mapping[str, Any]) -> dict[str, Any]:
    value = copy.deepcopy(dict(signals))
    _reject_unknown_fields(value, MATCHED_SIGNAL_FIELDS, "matchedSignals")
    from routing_policy import (  # Imported lazily to avoid an import cycle.
        ALGORITHM_TERMS, AMBIGUITY_TERMS, COMPLEXITY_TERMS, COMPUTER_USE_TERMS,
        DEBUGGING_TERMS, INHERENT_HIGH_RISK_TERMS, LONG_CONTEXT_TERMS,
        MULTI_FILE_TERMS, PARALLEL_TERMS, RISK_ACTION_TERMS, RISK_TERMS,
        SENSITIVE_DOMAIN_TERMS, SIMPLE_TERMS, SCOPE_TERMS,
    )
    packaged = {
        "complexity": frozenset(COMPLEXITY_TERMS), "risk": frozenset(RISK_TERMS),
        "riskAction": frozenset(RISK_ACTION_TERMS),
        "sensitiveDomain": frozenset(SENSITIVE_DOMAIN_TERMS),
        "inherentHighRisk": frozenset(INHERENT_HIGH_RISK_TERMS),
        "parallel": frozenset(PARALLEL_TERMS), "ambiguity": frozenset(AMBIGUITY_TERMS),
        "simple": frozenset(SIMPLE_TERMS), "scope": frozenset(SCOPE_TERMS),
        "algorithm": frozenset(ALGORITHM_TERMS), "debugging": frozenset(DEBUGGING_TERMS),
        "longContext": frozenset(LONG_CONTEXT_TERMS), "multiFile": frozenset(MULTI_FILE_TERMS),
        "computerUse": frozenset(COMPUTER_USE_TERMS),
    }
    for key, items in value.items():
        if not isinstance(items, list) or len(items) > len(packaged[key]):
            raise ValueError(f"route matchedSignals.{key} must be a bounded array")
        if any(not isinstance(item, str) or item not in packaged[key] for item in items):
            raise ValueError(f"route matchedSignals.{key} contains a non-packaged term")
    return value


def _validate_affinity_evidence(evidence: Mapping[str, Any], owner: str) -> dict[str, Any]:
    value = copy.deepcopy(dict(evidence))
    _reject_unknown_fields(value, AFFINITY_EVIDENCE_FIELDS, owner)
    if set(value) != AFFINITY_EVIDENCE_FIELDS:
        raise ValueError(f"route {owner} must contain the complete evidence schema")
    for key in ("samples", "inputTokens", "cachedInputTokens", "cacheWriteInputTokens"):
        _validate_nonnegative_integer(value.get(key), f"{owner}.{key}")
    for key in ("cacheReadRatio", "cacheWriteRatio", "cacheSignalRatio"):
        _validate_ratio(value.get(key), f"{owner}.{key}")
    if value.get("billingCostEstimated") is not False:
        raise ValueError(f"route {owner} must declare billingCostEstimated=false")
    return value


def _validate_model_affinity(affinity: Mapping[str, Any]) -> dict[str, Any]:
    value = copy.deepcopy(dict(affinity))
    _reject_unknown_fields(value, AFFINITY_FIELDS, "modelAffinity")
    if "evidence" not in value or not isinstance(value["evidence"], dict):
        raise ValueError("route modelAffinity evidence must be an object")
    value["evidence"] = _validate_affinity_evidence(
        value["evidence"], "modelAffinity.evidence"
    )
    if "previousModelEvidence" in value:
        if not isinstance(value["previousModelEvidence"], dict):
            raise ValueError("route modelAffinity previousModelEvidence must be an object")
        value["previousModelEvidence"] = _validate_affinity_evidence(
            value["previousModelEvidence"], "modelAffinity.previousModelEvidence"
        )
    if "errorType" in value and not SAFE_ID_PATTERN.fullmatch(str(value["errorType"])):
        raise ValueError("route modelAffinity errorType must be a safe identifier")
    return value


def _validate_execution_plan(plan: Mapping[str, Any]) -> dict[str, Any]:
    value = copy.deepcopy(dict(plan))
    _reject_unknown_fields(value, EXECUTION_PLAN_FIELDS, "executionPlan")
    for field, allowed in (
        ("effortSource", {"auto", "explicit", "registry-default"}),
        ("variantSource", {None, "policy", "explicit"}),
        ("orchestrationPolicy", {"direct", "recommend", "auto"}),
        ("graderPolicy", {"auto", "always", "never"}),
    ):
        if value.get(field) not in allowed:
            raise ValueError(f"route executionPlan {field} is invalid")
    context = value.get("context")
    if not isinstance(context, dict):
        raise ValueError("route executionPlan context must be an object")
    _reject_unknown_fields(context, CONTEXT_FIELDS, "executionPlan.context")
    for key in CONTEXT_FIELDS - {"profile"}:
        if key in context:
            _validate_nonnegative_integer(context[key], f"executionPlan.context.{key}")
    if context.get("profile") not in {None, "targeted", "standard", "expanded"}:
        raise ValueError("route executionPlan context profile is invalid")
    recommendation = value.get("orchestrationRecommendation")
    if not isinstance(recommendation, dict):
        raise ValueError("route executionPlan orchestrationRecommendation must be an object")
    _reject_unknown_fields(
        recommendation, RECOMMENDATION_FIELDS,
        "executionPlan.orchestrationRecommendation",
    )
    utility = recommendation.get("utility")
    if not isinstance(utility, dict):
        raise ValueError("route executionPlan recommendation utility must be an object")
    _reject_unknown_fields(utility, UTILITY_FIELDS, "executionPlan.utility")
    components = utility.get("components")
    if not isinstance(components, dict):
        raise ValueError("route executionPlan utility components must be an object")
    _reject_unknown_fields(components, UTILITY_COMPONENT_FIELDS, "executionPlan.utility.components")
    if any(isinstance(item, bool) or not isinstance(item, int) for item in components.values()):
        raise ValueError("route executionPlan utility components must be integers")
    escalation = value.get("escalation")
    if not isinstance(escalation, dict):
        raise ValueError("route executionPlan escalation must be an object")
    _reject_unknown_fields(escalation, ESCALATION_FIELDS, "executionPlan.escalation")
    return value


def build_task_binding(task_text: str) -> dict[str, Any]:
    """Bind a route to one task without retaining the task body."""
    task = str(task_text)
    if not task.strip():
        raise ValueError("route task binding requires a non-empty task")
    nonce = secrets.token_hex(16)
    digest = hashlib.sha256(
        f"{TASK_BINDING_SCHEMA}\0{nonce}\0{task}".encode("utf-8")
    ).hexdigest()
    return {
        "schema": TASK_BINDING_SCHEMA,
        "algorithm": "sha256",
        "nonce": nonce,
        "digest": digest,
        "storesTaskText": False,
    }


def validate_task_binding(
    binding: Mapping[str, Any],
    *,
    task_text: str | None = None,
) -> dict[str, Any]:
    value = copy.deepcopy(dict(binding))
    expected_fields = {
        "schema", "algorithm", "nonce", "digest", "storesTaskText",
    }
    unknown = sorted(set(value) - expected_fields)
    missing = sorted(expected_fields - set(value))
    if unknown:
        raise ValueError("route taskBinding contains unsupported fields: " + ", ".join(unknown))
    if missing:
        raise ValueError("route taskBinding is missing fields: " + ", ".join(missing))
    if value.get("schema") != TASK_BINDING_SCHEMA:
        raise ValueError(f"route taskBinding schema must be {TASK_BINDING_SCHEMA}")
    if value.get("algorithm") != "sha256":
        raise ValueError("route taskBinding algorithm must be sha256")
    nonce = str(value.get("nonce") or "")
    digest = str(value.get("digest") or "")
    if not NONCE_PATTERN.fullmatch(nonce):
        raise ValueError("route taskBinding nonce must be 128-bit lowercase hex")
    if not WORKSPACE_KEY_PATTERN.fullmatch(digest):
        raise ValueError("route taskBinding digest must be SHA-256")
    if value.get("storesTaskText") is not False:
        raise ValueError("route taskBinding must declare storesTaskText=false")
    if task_text is not None:
        expected = hashlib.sha256(
            f"{TASK_BINDING_SCHEMA}\0{nonce}\0{task_text}".encode("utf-8")
        ).hexdigest()
        if not hmac.compare_digest(digest, expected):
            raise ValueError("locked route task binding does not match the current task")
    return value


def extract_execution_envelope(
    payload: Mapping[str, Any],
) -> tuple[str, dict[str, Any], dict[str, Any]]:
    """Parse the transient stdin-only execution envelope."""
    value = copy.deepcopy(dict(payload))
    expected = {"schema", "task", "routeDecision", "hostPermissions"}
    unknown = sorted(set(value) - expected)
    missing = sorted(expected - set(value))
    if unknown:
        raise ValueError(
            "execution envelope contains unsupported fields: " + ", ".join(unknown)
        )
    if missing:
        raise ValueError(
            "execution envelope is missing fields: " + ", ".join(missing)
        )
    if value.get("schema") != EXECUTION_ENVELOPE_SCHEMA:
        raise ValueError(f"execution envelope schema must be {EXECUTION_ENVELOPE_SCHEMA}")
    task = value.get("task")
    if not isinstance(task, str) or not task.strip():
        raise ValueError("execution envelope task must be a non-empty string")
    if len(task.encode("utf-8")) > MAX_TASK_BYTES:
        raise ValueError(f"execution envelope task exceeds {MAX_TASK_BYTES} bytes")
    route = value.get("routeDecision")
    permissions = value.get("hostPermissions")
    if not isinstance(route, dict):
        raise ValueError("execution envelope routeDecision must be an object")
    if not isinstance(permissions, dict):
        raise ValueError("execution envelope hostPermissions must be an object")
    return task, route, permissions


def _reject_private_content(value: Any) -> None:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            if str(key).lower() in FORBIDDEN_CONTENT_FIELDS:
                raise ValueError(f"route decision may not contain field: {key}")
            _reject_private_content(nested)
    elif isinstance(value, (list, tuple)):
        for nested in value:
            _reject_private_content(nested)
    elif isinstance(value, str) and (
        re.match(r"^[A-Za-z]:[\\/]", value)
        or value.startswith(("/", "\\\\", "file://"))
    ):
        raise ValueError("route decision may not contain an absolute path")


def _privacy_safe_source(source: str | None) -> str | None:
    if source is None:
        return None
    value = str(source)
    if value == "builtin" or re.fullmatch(r"[a-z0-9-]+:[0-9a-f]{64}", value):
        return value
    name = value.replace("\\", "/").rsplit("/", 1)[-1]
    if not name or name in {".", ".."}:
        raise ValueError("route source must identify a file or stable source label")
    return f"file:{name}"


def _validated_workspace_key(workspace_key: str | None) -> str | None:
    if workspace_key is None:
        return None
    value = str(workspace_key)
    if not WORKSPACE_KEY_PATTERN.fullmatch(value):
        raise ValueError("route workspaceKey must be a SHA-256 digest")
    return value


def _validated_repository_metadata(
    metadata: Mapping[str, Any] | None,
) -> dict[str, Any]:
    value = dict(metadata or {})
    unsupported = sorted(set(value) - REPOSITORY_METADATA_FIELDS)
    if unsupported:
        raise ValueError(
            "route repository metadata contains unsupported fields: "
            + ", ".join(unsupported)
        )
    if any(not isinstance(item, (bool, int)) for item in value.values()):
        raise ValueError("route repository metadata values must be numeric or boolean")
    return copy.deepcopy(value)


def validate_route_decision(
    route: Mapping[str, Any],
    *,
    registry: ModelRegistry | None = None,
    task_text: str | None = None,
) -> dict[str, Any]:
    """Validate and copy one canonical route before a host acts on it."""
    value = copy.deepcopy(dict(route))
    _reject_private_content(value)
    if value.get("schema") != ROUTE_DECISION_SCHEMA:
        raise ValueError(f"route schema must be {ROUTE_DECISION_SCHEMA}")
    _reject_unknown_fields(value, ROUTE_FIELDS, "decision")
    missing = sorted(ROUTE_FIELDS - set(value))
    if missing:
        raise ValueError("route decision is missing fields: " + ", ".join(missing))
    task_binding = value.get("taskBinding")
    if not isinstance(task_binding, dict):
        raise ValueError("route taskBinding must be an object")
    value["taskBinding"] = validate_task_binding(task_binding, task_text=task_text)
    if value.get("modelCalls") != 0:
        raise ValueError("route decision must declare zero routing model calls")
    if not SAFE_ID_PATTERN.fullmatch(str(value.get("routeId") or "")):
        raise ValueError("route routeId must be an opaque safe identifier")
    if value.get("strategy") not in STRATEGIES:
        raise ValueError("route strategy is invalid")
    if value.get("effort") not in EFFORTS:
        raise ValueError("route effort is invalid")
    if value.get("selectedTier") not in TIERS or value.get("targetTier") not in TIERS:
        raise ValueError("route tier is invalid")
    if not SAFE_ID_PATTERN.fullmatch(str(value.get("reasonCode") or "")):
        raise ValueError("route reasonCode is invalid")
    feature_schema_version = value.get("featureSchemaVersion")
    if (
        isinstance(feature_schema_version, bool)
        or not isinstance(feature_schema_version, int)
        or feature_schema_version < 1
    ):
        raise ValueError("route featureSchemaVersion must be a positive integer")
    value["workspaceKey"] = _validated_workspace_key(value.get("workspaceKey"))
    repository = value.get("repository")
    if not isinstance(repository, dict):
        raise ValueError("route repository must be an object")
    _reject_unknown_fields(
        repository, frozenset({"mode", "metadata"}), "repository"
    )
    if set(repository) != {"mode", "metadata"}:
        raise ValueError("route repository must contain mode and metadata")
    repository["metadata"] = _validated_repository_metadata(repository.get("metadata"))
    if not isinstance(repository.get("mode"), str):
        raise ValueError("route repository mode must be a string")
    for key in ("features", "matchedSignals", "executionPlan", "policy", "registry"):
        if not isinstance(value.get(key), dict):
            raise ValueError(f"route {key} must be an object")
    if not isinstance(value.get("modelAffinity"), dict):
        raise ValueError("route modelAffinity must be an object")
    if not isinstance(value.get("requiredCapabilities"), list):
        raise ValueError("route requiredCapabilities must be an array")
    if len(value["requiredCapabilities"]) > 32 or any(
        not SAFE_ID_PATTERN.fullmatch(str(item))
        for item in value["requiredCapabilities"]
    ):
        raise ValueError("route requiredCapabilities contains an invalid identifier")
    value["features"] = _validate_features(value["features"])
    value["matchedSignals"] = _validate_matched_signals(value["matchedSignals"])
    value["modelAffinity"] = _validate_model_affinity(value["modelAffinity"])
    value["executionPlan"] = _validate_execution_plan(value["executionPlan"])
    for owner, allowed in (
        ("policy", frozenset({"version", "digest", "source"})),
        ("registry", frozenset({"digest", "source"})),
    ):
        _reject_unknown_fields(value[owner], allowed, owner)
        if set(value[owner]) != allowed:
            raise ValueError(f"route {owner} must contain the complete identity schema")
    if not SAFE_ID_PATTERN.fullmatch(str(value["policy"].get("version") or "")):
        raise ValueError("route policy version must be a safe identifier")
    for owner in ("policy", "registry"):
        digest = str(value[owner].get("digest") or "")
        if not WORKSPACE_KEY_PATTERN.fullmatch(digest):
            raise ValueError(f"route {owner} digest must be SHA-256")
        source = value[owner].get("source")
        if source is not None and not (
            source == "builtin"
            or re.fullmatch(r"file:[A-Za-z0-9._-]{1,200}", str(source))
            or re.fullmatch(r"[a-z0-9-]+:[0-9a-f]{64}", str(source))
        ):
            raise ValueError(f"route {owner} source is not privacy-safe")
    affinity = value["modelAffinity"]
    if affinity:
        if affinity.get("schema") != "agent-auto-router.model-affinity.v1":
            raise ValueError("route affinity schema is invalid")
        if affinity.get("mode") not in {"auto", "off"}:
            raise ValueError("route affinity mode is invalid")
        if affinity.get("modelCalls") != 0:
            raise ValueError("route affinity must declare zero model calls")
        if affinity.get("workspaceKey") != value["workspaceKey"]:
            raise ValueError("route affinity workspaceKey does not match the route")
        if affinity.get("selectedModel") != value["selectedModel"]:
            raise ValueError("route affinity selectedModel does not match the route")
        if affinity.get("selectorModel") != value["selectorModel"]:
            raise ValueError("route affinity selectorModel does not match the route")
        if affinity.get("storesWorkspacePath") is not False:
            raise ValueError("route affinity must declare storesWorkspacePath=false")
        if affinity.get("targetTier") != value["targetTier"]:
            raise ValueError("route affinity targetTier does not match the route")
        if affinity.get("selectedTier") != value["selectedTier"]:
            raise ValueError("route affinity selectedTier does not match the route")
        if affinity.get("roleModelPolicy") not in {
            "profile", "selected-model-preferred"
        }:
            raise ValueError("route affinity roleModelPolicy is invalid")
    execution_plan = value["executionPlan"]
    if execution_plan.get("model") != value["selectedModel"]:
        raise ValueError("route executionPlan model does not match selectedModel")
    if execution_plan.get("selectedTier") != value["selectedTier"]:
        raise ValueError("route executionPlan selectedTier does not match the route")
    if execution_plan.get("requiredTier") != value["targetTier"]:
        raise ValueError("route executionPlan requiredTier does not match the route")
    if execution_plan.get("effort") != value["effort"]:
        raise ValueError("route executionPlan effort does not match route effort")
    if execution_plan.get("modelAffinity", {}) != affinity:
        raise ValueError("route executionPlan affinity does not match route affinity")
    variant = execution_plan.get("variant")
    if variant not in VARIANT_TOPOLOGY:
        raise ValueError("route executionPlan variant is invalid")
    if execution_plan.get("topology") != VARIANT_TOPOLOGY[variant]:
        raise ValueError("route executionPlan topology does not match its variant")
    if execution_plan.get("variantSource") not in {"policy", "explicit"}:
        raise ValueError("route executionPlan variantSource is invalid")
    if execution_plan.get("maxModelCalls") != VARIANT_MAX_MODEL_CALLS[variant]:
        raise ValueError("route executionPlan maxModelCalls does not match its variant")
    recommendation = execution_plan["orchestrationRecommendation"]
    recommended_variant = recommendation.get("recommendedVariant")
    if recommended_variant not in VARIANT_TOPOLOGY:
        raise ValueError("route recommendedVariant is invalid")
    expected_recommended_topology = (
        "orchestrated"
        if VARIANT_TOPOLOGY[recommended_variant] == "orchestrated"
        and recommendation["utility"].get("passes") is True
        else "direct"
    )
    if recommendation.get("recommendedTopology") != expected_recommended_topology:
        raise ValueError("route recommendedTopology does not match the utility gate")
    utility_passes = recommendation["utility"].get("passes") is True
    orchestration_policy = execution_plan.get("orchestrationPolicy")
    expected_recommended = utility_passes and orchestration_policy != "direct"
    if recommendation.get("recommended") is not expected_recommended:
        raise ValueError("route recommended flag does not match orchestration policy")
    expected_explicit_opt_in = utility_passes and (
        orchestration_policy == "recommend"
        or recommendation.get("blockedByRiskGate") is True
    )
    if recommendation.get("requiresExplicitOptIn") is not expected_explicit_opt_in:
        raise ValueError("route explicit-opt-in flag does not match orchestration policy")
    if recommendation.get("estimatedMaximumModelCalls") != VARIANT_MAX_MODEL_CALLS[recommended_variant]:
        raise ValueError("route recommended model-call estimate does not match its variant")
    if execution_plan.get("roleModelPolicy") != affinity.get("roleModelPolicy"):
        raise ValueError("route executionPlan roleModelPolicy does not match affinity")
    active_registry = registry
    if active_registry is not None:
        if value["registry"].get("digest") != registry_digest(active_registry):
            raise ValueError("route registry digest does not match the active registry")
        selected = active_registry.get(str(value["selectedModel"]), role="direct")
        selector = active_registry.get(str(value["selectorModel"]), role="direct")
        if selected.tier != value["selectedTier"]:
            raise ValueError("route selectedTier does not match the selected model")
        if selector.tier != value["targetTier"]:
            raise ValueError("route targetTier does not match the selector model")
        if not selector.auto_eligible:
            raise ValueError("route selector model is not eligible for Auto")
        if not value["explicitOverride"] and not selected.auto_eligible:
            raise ValueError("automatic route selected an explicit-only model")
        required_capabilities = frozenset(
            str(item) for item in value["requiredCapabilities"]
        )
        if not required_capabilities.issubset(selected.capabilities):
            raise ValueError("route selected model lacks required capabilities")
    encoded = json.dumps(
        value, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    if len(encoded) > MAX_ROUTE_DECISION_BYTES:
        raise ValueError(
            f"route decision exceeds {MAX_ROUTE_DECISION_BYTES} encoded bytes"
        )
    return value


def extract_route_decision(
    payload: Mapping[str, Any],
    *,
    registry: ModelRegistry | None = None,
    task_text: str | None = None,
) -> dict[str, Any]:
    candidate = payload.get("routeDecision", payload)
    if not isinstance(candidate, Mapping):
        raise ValueError("routeDecision must be an object")
    return validate_route_decision(
        candidate, registry=registry, task_text=task_text
    )


def enrich_route_decision(
    route: Mapping[str, Any],
    *,
    repository_mode: str,
    repository_metadata: Mapping[str, Any] | None,
    policy_source: str | None,
    registry_source: str | None,
) -> dict[str, Any]:
    value = copy.deepcopy(dict(route))
    value["repository"] = {
        "mode": str(repository_mode),
        "metadata": _validated_repository_metadata(repository_metadata),
    }
    value["policy"]["source"] = _privacy_safe_source(policy_source)
    value["registry"]["source"] = _privacy_safe_source(registry_source)
    return value


def build_route_decision(
    *,
    route_id: str,
    task_text: str,
    strategy: str,
    effort: str,
    selected_model: str,
    selected_tier: str,
    selector_model: str | None = None,
    target_tier: str | None = None,
    reason_code: str,
    feature_schema_version: int,
    features: Mapping[str, Any],
    matched_signals: Mapping[str, Any],
    repository_mode: str,
    repository_metadata: Mapping[str, Any] | None,
    execution_plan: Mapping[str, Any],
    policy_version: str,
    policy_digest: str,
    registry_digest: str,
    policy_source: str | None = None,
    registry_source: str | None = None,
    explicit_override: bool = False,
    required_capabilities: tuple[str, ...] = (),
    workspace_key: str | None = None,
    model_affinity: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return the stable, content-free route-decision.v2 envelope."""
    result = {
        "schema": ROUTE_DECISION_SCHEMA,
        "routeId": str(route_id),
        "strategy": str(strategy),
        "effort": str(effort),
        "selectedModel": str(selected_model),
        "selectedTier": str(selected_tier),
        "selectorModel": str(selector_model or selected_model),
        "targetTier": str(target_tier or selected_tier),
        "reasonCode": str(reason_code),
        "explicitOverride": bool(explicit_override),
        "requiredCapabilities": [str(value) for value in required_capabilities],
        "taskBinding": build_task_binding(task_text),
        "workspaceKey": _validated_workspace_key(workspace_key),
        "modelAffinity": copy.deepcopy(dict(model_affinity or {})),
        "featureSchemaVersion": int(feature_schema_version),
        "features": copy.deepcopy(dict(features)),
        "matchedSignals": copy.deepcopy(dict(matched_signals)),
        "repository": {
            "mode": str(repository_mode),
            "metadata": _validated_repository_metadata(repository_metadata),
        },
        "executionPlan": copy.deepcopy(dict(execution_plan)),
        "policy": {
            "version": str(policy_version),
            "digest": str(policy_digest),
            "source": _privacy_safe_source(policy_source),
        },
        "registry": {
            "digest": str(registry_digest),
            "source": _privacy_safe_source(registry_source),
        },
        "modelCalls": 0,
    }
    return validate_route_decision(result, task_text=task_text)
