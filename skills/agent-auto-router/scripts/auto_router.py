from __future__ import annotations

from dataclasses import asdict, replace
from typing import Any, Iterable, Sequence

from model_affinity import resolve_model_affinity
from model_registry import ModelRegistry, load_model_registry
from execution_plan import build_execution_plan
from route_contract import ROUTE_DECISION_SCHEMA, build_route_decision
from routing_policy import STRATEGIES, RoutingPolicy, matched_signal_terms, select_model

VALID_MODES = set(STRATEGIES)
VARIANT_LABELS = {
    "A": "variant-a-direct",
    "B": "variant-b-plan-workers-review",
    "C": "variant-c-plan-dispatch-workers-review",
    "D": "variant-d-plan-workers-review",
    "E": "variant-e-direct",
    "F": "variant-f-direct",
}

def route_case(
    case: dict[str, Any],
    mode: str = "balance",
    effort: str = "medium",
    policy: RoutingPolicy | None = None,
    registry: ModelRegistry | None = None,
    backends: Sequence[str] | None = None,
    allow_explicit_only: bool = False,
    orchestration_policy: str = "auto",
    confirm_high_risk_orchestration: bool = False,
    affinity_events: Iterable[dict[str, Any]] = (),
    model_affinity_mode: str = "auto",
    explicit_variant: str | None = None,
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

    active_registry = registry or load_model_registry()
    affinity = resolve_model_affinity(
        affinity_events,
        workspace_key=(
            str(case.get("workspace_key")) if case.get("workspace_key") else None
        ),
        strategy=mode,
        selector_model=decision.model,
        target_tier=decision.target_tier,
        registry=active_registry,
        available_backends=backends or active_registry.backends,
        required_capabilities=decision.required_capabilities,
        mode=model_affinity_mode,
    )
    selected_model = str(affinity.get("selectedModel") or decision.model)
    selected_spec = active_registry.get(selected_model, role="direct")
    plan_decision = replace(decision, model=selected_spec.model_id)
    execution_plan = build_execution_plan(
        plan_decision,
        effort,
        orchestration_policy=orchestration_policy,
        confirm_high_risk_orchestration=confirm_high_risk_orchestration,
        model_affinity=affinity,
        selected_tier=selected_spec.tier,
        required_tier=decision.target_tier,
        explicit_variant=explicit_variant,
    )
    if selected_spec.tier != decision.target_tier:
        execution_plan["escalation"].update(
            {
                "eligible": False,
                "nextTier": None,
                "reason": "affinity-already-retained-stronger-tier",
            }
        )
    variant = str(execution_plan["variant"])
    recommended_variant = str(
        execution_plan["orchestrationRecommendation"]["recommendedVariant"]
    )

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
    if recommended_variant == "D":
        reasons.append("balanced-tier orchestration selected for a routine parallelizable task")
    if recommended_variant == "C":
        reasons.append("balanced-tier dispatch selected for ambiguous frontier-tier decomposition")
    if recommended_variant != variant:
        reasons.append(
            f"orchestration_{orchestration_policy}=direct_execution_with_{recommended_variant}_recommendation"
        )

    features = asdict(decision)
    for key in (
        "model", "target_tier", "required_capabilities", "reason", "strategy", "effort",
        "policy_version", "policy_digest", "registry_digest",
        "feature_schema_version",
    ):
        features.pop(key, None)

    matched_signals = matched_signal_terms(prompt)
    route_id = str(case.get("id") or "route")
    route_decision = build_route_decision(
        route_id=route_id,
        task_text=prompt,
        strategy=mode,
        effort=decision.effort,
        selected_model=selected_spec.model_id,
        selected_tier=selected_spec.tier,
        selector_model=decision.model,
        target_tier=decision.target_tier,
        reason_code=decision.reason,
        feature_schema_version=decision.feature_schema_version,
        features=features,
        matched_signals=matched_signals,
        repository_mode="features-only",
        repository_metadata=(
            repository_features if isinstance(repository_features, dict) else None
        ),
        execution_plan=execution_plan,
        policy_version=decision.policy_version,
        policy_digest=decision.policy_digest,
        registry_digest=decision.registry_digest,
        registry_source=registry.source if registry is not None else None,
        required_capabilities=tuple(decision.required_capabilities),
        workspace_key=(
            str(case.get("workspace_key")) if case.get("workspace_key") else None
        ),
        model_affinity=affinity,
    )
    return {
        "schema": ROUTE_DECISION_SCHEMA,
        "routeDecision": route_decision,
        "router_version": decision.policy_version,
        "policy_digest": decision.policy_digest,
        "registry_digest": decision.registry_digest,
        "feature_schema_version": decision.feature_schema_version,
        "mode": mode,
        "effort": decision.effort,
        "variant": variant,
        "recommended_variant": recommended_variant,
        "route": VARIANT_LABELS[variant],
        "selected_model": selected_spec.model_id,
        "selector_model": decision.model,
        "target_tier": decision.target_tier,
        "required_capabilities": list(decision.required_capabilities),
        "execution_plan": execution_plan,
        "repository_features": repository_features if isinstance(repository_features, dict) else None,
        "model_reason": decision.reason,
        "workspace_key": case.get("workspace_key"),
        "model_affinity": affinity,
        "matched_signals": matched_signals,
        "features": features,
        "reasons": reasons,
    }
