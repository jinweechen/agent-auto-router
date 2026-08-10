from __future__ import annotations

from dataclasses import asdict
from typing import Any, Sequence

from model_registry import ModelRegistry
from execution_plan import build_execution_plan, variant_for_decision
from routing_policy import STRATEGIES, RoutingPolicy, select_model

VALID_MODES = set(STRATEGIES)
VARIANT_LABELS = {
    "A": "direct-sol",
    "B": "sol-plan-luna-workers-sol-review",
    "C": "sol-plan-terra-dispatch-luna-workers-sol-review",
    "D": "terra-plan-luna-workers-terra-review",
    "E": "direct-terra",
    "F": "direct-luna",
}

def route_case(
    case: dict[str, Any],
    mode: str = "balance",
    effort: str = "medium",
    policy: RoutingPolicy | None = None,
    registry: ModelRegistry | None = None,
    backends: Sequence[str] | None = None,
    allow_explicit_only: bool = False,
) -> dict[str, Any]:
    if mode not in VALID_MODES:
        raise ValueError(f"Unknown routing mode: {mode}")

    prompt = str(case.get("prompt", ""))
    criteria = case.get("acceptance_criteria", [])
    acceptance_criteria = criteria if isinstance(criteria, list) else []
    repository_features = case.get("repository_features")
    decision = select_model(
        prompt,
        mode,
        effort,
        acceptance_criteria,
        policy,
        registry,
        repository_features if isinstance(repository_features, dict) else None,
        backends=backends,
        allow_explicit_only=allow_explicit_only,
    )

    variant = variant_for_decision(decision)
    execution_plan = build_execution_plan(decision, effort)

    reasons = [
        f"model={decision.model}",
        f"target_tier={decision.target_tier}",
        f"complexity={decision.complexity_score}/10",
        f"risk={decision.risk_score}/10",
        f"clarity={decision.clarity_score}/10",
        f"parallelizable={str(decision.parallelizable).lower()}",
        f"orchestration_eligible={str(decision.orchestration_eligible).lower()}",
        f"dependency_ambiguity={str(decision.dependency_ambiguity).lower()}",
        f"model_reason={decision.reason}",
    ]
    if variant == "D":
        reasons.append("Terra orchestration selected for a routine parallelizable task")
    if variant == "C":
        reasons.append("Terra dispatch selected for ambiguous Sol-tier decomposition")

    features = asdict(decision)
    for key in (
        "model", "target_tier", "required_capabilities", "reason", "strategy", "effort",
        "policy_version", "policy_digest", "registry_digest",
        "feature_schema_version",
    ):
        features.pop(key, None)

    return {
        "router_version": decision.policy_version,
        "policy_digest": decision.policy_digest,
        "registry_digest": decision.registry_digest,
        "feature_schema_version": decision.feature_schema_version,
        "mode": mode,
        "effort": decision.effort,
        "variant": variant,
        "route": VARIANT_LABELS[variant],
        "selected_model": decision.model,
        "target_tier": decision.target_tier,
        "required_capabilities": list(decision.required_capabilities),
        "execution_plan": execution_plan,
        "repository_features": repository_features if isinstance(repository_features, dict) else None,
        "model_reason": decision.reason,
        "features": features,
        "reasons": reasons,
    }
