"""Deterministic execution-plan recommendations derived from a model decision."""

from __future__ import annotations

from typing import Any


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


def recommended_effort(target_tier: str, *, high_risk: bool) -> str:
    if high_risk or target_tier == "frontier":
        return "high"
    if target_tier == "fast":
        return "low"
    return "medium"


def variant_for_decision(decision: Any) -> str:
    if not bool(decision.orchestration_eligible):
        return {"frontier": "A", "balanced": "E", "fast": "F"}[decision.target_tier]
    if decision.target_tier == "frontier":
        return "C" if bool(decision.dependency_ambiguity) else "B"
    if decision.target_tier == "balanced":
        return "D"
    return "F"


def build_execution_plan(decision: Any, explicit_effort: str | None = None) -> dict[str, Any]:
    variant = variant_for_decision(decision)
    effort = explicit_effort or recommended_effort(
        decision.target_tier, high_risk=bool(decision.high_risk)
    )
    next_tier = {"fast": "balanced", "balanced": "frontier", "frontier": None}[
        decision.target_tier
    ]
    return {
        "model": decision.model,
        "tier": decision.target_tier,
        "effort": effort,
        "effortSource": "explicit" if explicit_effort else "auto",
        "topology": "orchestrated" if variant in {"B", "C", "D"} else "direct",
        "variant": variant,
        "context": dict(CONTEXT_BUDGETS[decision.target_tier]),
        "graderPolicy": "auto",
        "maxModelCalls": {"A": 2, "B": 6, "C": 7, "D": 5, "E": 1, "F": 1}[variant],
        "escalation": {
            "eligible": next_tier is not None and not bool(decision.high_risk),
            "nextTier": next_tier,
            "requiresExplicitOptIn": True,
        },
    }
