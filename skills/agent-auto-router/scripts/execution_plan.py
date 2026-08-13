"""Deterministic execution-plan recommendations derived from a model decision."""

from __future__ import annotations

from typing import Any

from model_affinity import ROLE_MODEL_POLICY_AFFINITY, ROLE_MODEL_POLICY_PROFILE


CONTEXT_BUDGETS = {
    "fast": {
        "profile": "targeted",
        "repoMapTokens": 1500,
        "maxCandidateFiles": 8,
        "maxToolOutputChars": 12000,
    },
    "balanced": {
        "profile": "standard",
        "repoMapTokens": 3000,
        "maxCandidateFiles": 16,
        "maxToolOutputChars": 24000,
    },
    "frontier": {
        "profile": "expanded",
        "repoMapTokens": 6000,
        "maxCandidateFiles": 32,
        "maxToolOutputChars": 48000,
    },
}
ORCHESTRATION_POLICIES = ("direct", "recommend", "auto")
DEFAULT_ORCHESTRATION_POLICY = "recommend"
ORCHESTRATED_VARIANTS = frozenset({"B", "C", "D"})
MAX_MODEL_CALLS = {"A": 2, "B": 6, "C": 7, "D": 5, "E": 1, "F": 1}
MINIMUM_ORCHESTRATION_UTILITY = 1
ROLE_TIER_SWITCHES = {"A": 0, "B": 3, "C": 4, "D": 2, "E": 0, "F": 0}


def recommended_effort(
    target_tier: str,
    *,
    high_risk: bool,
    validation_configured: bool = False,
) -> str:
    if high_risk or target_tier == "frontier":
        return "high"
    if target_tier == "fast":
        return "low" if validation_configured else "medium"
    return "medium"


def variant_for_decision(decision: Any) -> str:
    if not bool(decision.orchestration_eligible):
        return {"frontier": "A", "balanced": "E", "fast": "F"}[decision.target_tier]
    if decision.target_tier == "frontier":
        return "C" if bool(decision.dependency_ambiguity) else "B"
    if decision.target_tier == "balanced":
        return "D"
    return "F"


def direct_variant_for_tier(target_tier: str) -> str:
    return {"frontier": "A", "balanced": "E", "fast": "F"}[target_tier]


def orchestration_utility(
    decision: Any,
    variant: str,
    role_model_policy: str = ROLE_MODEL_POLICY_AFFINITY,
    cache_signal_ratio: float | None = None,
) -> dict[str, Any]:
    """Estimate deterministic coordination benefit before selecting a worker topology."""
    if variant not in ORCHESTRATED_VARIANTS:
        return {
            "score": 0,
            "minimumScore": MINIMUM_ORCHESTRATION_UTILITY,
            "passes": False,
            "benefitPoints": 0,
            "overheadPoints": 0,
            "estimatedAdditionalModelCalls": 0,
            "estimatedRoleTierSwitches": 0,
            "estimatedProfileTierSwitches": 0,
            "roleModelPolicy": role_model_policy,
            "cacheSignalRatio": cache_signal_ratio,
            "sessionBoundaryOverheadPoints": 0,
            "billingCostEstimated": False,
            "components": {},
        }

    components = {
        "independentParallelScale": 2,
        "complexity": min(4, max(0, int(decision.complexity_score))),
        "acceptanceCriteria": min(3, max(0, int(decision.criteria_count)) // 2),
        "debugging": 2 if bool(decision.complex_debugging) else 0,
        "longContext": 2 if bool(decision.long_context) else 0,
        "multiFile": 2 if bool(decision.multi_file) else 0,
        "dependencyCoordination": 1 if bool(decision.dependency_ambiguity) else 0,
        "largePrompt": 2 if int(decision.prompt_chars) >= 6000 else 1 if int(decision.prompt_chars) >= 1800 else 0,
        "scope": min(2, max(0, int(decision.scope_hits))),
    }
    benefit_points = sum(components.values())
    additional_calls = MAX_MODEL_CALLS[variant] - 1
    if role_model_policy not in {ROLE_MODEL_POLICY_AFFINITY, ROLE_MODEL_POLICY_PROFILE}:
        raise ValueError("unsupported role model policy")
    profile_tier_switches = ROLE_TIER_SWITCHES[variant]
    tier_switches = 0 if role_model_policy == ROLE_MODEL_POLICY_AFFINITY else profile_tier_switches
    call_overhead = max(1, (additional_calls + 1) // 2)
    session_boundary_overhead = (profile_tier_switches + 1) // 2
    overhead_points = call_overhead + session_boundary_overhead
    score = benefit_points - overhead_points
    return {
        "score": score,
        "minimumScore": MINIMUM_ORCHESTRATION_UTILITY,
        "passes": score >= MINIMUM_ORCHESTRATION_UTILITY,
        "benefitPoints": benefit_points,
        "overheadPoints": overhead_points,
        "estimatedAdditionalModelCalls": additional_calls,
        "estimatedRoleTierSwitches": tier_switches,
        "estimatedProfileTierSwitches": profile_tier_switches,
        "roleModelPolicy": role_model_policy,
        "cacheSignalRatio": cache_signal_ratio,
        "sessionBoundaryOverheadPoints": session_boundary_overhead,
        "billingCostEstimated": False,
        "components": components,
    }


def build_execution_plan(
    decision: Any,
    explicit_effort: str | None = None,
    orchestration_policy: str = DEFAULT_ORCHESTRATION_POLICY,
    confirm_high_risk_orchestration: bool = False,
    model_affinity: dict[str, Any] | None = None,
    selected_tier: str | None = None,
    required_tier: str | None = None,
    explicit_variant: str | None = None,
) -> dict[str, Any]:
    if orchestration_policy not in ORCHESTRATION_POLICIES:
        raise ValueError(f"unknown orchestration policy: {orchestration_policy}")
    recommended_variant = variant_for_decision(decision)
    recommendation_eligible = recommended_variant in ORCHESTRATED_VARIANTS
    affinity = dict(model_affinity or {})
    role_model_policy = str(
        affinity.get("roleModelPolicy") or ROLE_MODEL_POLICY_AFFINITY
    )
    cache_evidence = affinity.get("evidence")
    cache_signal_ratio = (
        cache_evidence.get("cacheSignalRatio")
        if isinstance(cache_evidence, dict) else None
    )
    utility = orchestration_utility(
        decision,
        recommended_variant,
        role_model_policy=role_model_policy,
        cache_signal_ratio=cache_signal_ratio,
    )
    utility_passes = recommendation_eligible and bool(utility["passes"])
    blocked_by_utility_gate = recommendation_eligible and not utility_passes
    blocked_by_risk_gate = bool(
        orchestration_policy == "auto"
        and utility_passes
        and bool(decision.high_risk)
        and not confirm_high_risk_orchestration
    )
    if explicit_variant is not None:
        if explicit_variant not in MAX_MODEL_CALLS:
            raise ValueError(f"unknown explicit orchestration variant: {explicit_variant}")
        variant = explicit_variant
    else:
        variant = (
            recommended_variant
            if orchestration_policy == "auto" and utility_passes and not blocked_by_risk_gate
            else direct_variant_for_tier(decision.target_tier)
        )
    effort = explicit_effort or recommended_effort(
        decision.target_tier,
        high_risk=bool(decision.high_risk),
        validation_configured=bool(getattr(decision, "validation_configured", False)),
    )
    next_tier = {"fast": "balanced", "balanced": "frontier", "frontier": None}[
        decision.target_tier
    ]
    return {
        "model": decision.model,
        "requiredTier": required_tier or decision.target_tier,
        "selectedTier": selected_tier or decision.target_tier,
        "effort": effort,
        "effortSource": "explicit" if explicit_effort else "auto",
        "topology": "orchestrated" if variant in ORCHESTRATED_VARIANTS else "direct",
        "variant": variant,
        "variantSource": "explicit" if explicit_variant is not None else "policy",
        "orchestrationPolicy": orchestration_policy,
        "roleModelPolicy": role_model_policy,
        "modelAffinity": affinity,
        "orchestrationRecommendation": {
            "eligible": recommendation_eligible,
            "recommended": (
                utility_passes
                and orchestration_policy != "direct"
            ),
            "recommendedTopology": (
                "orchestrated" if utility_passes else "direct"
            ),
            "recommendedVariant": recommended_variant,
            "estimatedMaximumModelCalls": MAX_MODEL_CALLS[recommended_variant],
            "requiresExplicitOptIn": (
                utility_passes
                and (orchestration_policy == "recommend" or blocked_by_risk_gate)
            ),
            "utility": utility,
            "blockedByUtilityGate": blocked_by_utility_gate,
            "blockedByRiskGate": blocked_by_risk_gate,
            "highRiskConfirmationProvided": bool(confirm_high_risk_orchestration),
            "reason": (
                "orchestration-overhead-exceeds-benefit"
                if blocked_by_utility_gate
                else "high-risk-confirmation-required"
                if blocked_by_risk_gate
                else "parallel-signals-scale-and-utility"
                if utility_passes
                else "insufficient-independent-parallel-scale"
            ),
        },
        "context": dict(CONTEXT_BUDGETS[decision.target_tier]),
        "graderPolicy": "auto",
        "maxModelCalls": MAX_MODEL_CALLS[variant],
        "escalation": {
            "eligible": next_tier is not None and not bool(decision.high_risk),
            "nextTier": next_tier,
            "requiresExplicitOptIn": True,
        },
    }
