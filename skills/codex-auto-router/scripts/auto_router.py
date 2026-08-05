from __future__ import annotations

from dataclasses import asdict
from typing import Any

from routing_policy import LUNA_MODEL, SOL_MODEL, STRATEGIES, TERRA_MODEL, select_model

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
) -> dict[str, Any]:
    if mode not in VALID_MODES:
        raise ValueError(f"Unknown routing mode: {mode}")

    prompt = str(case.get("prompt", ""))
    criteria = case.get("acceptance_criteria", [])
    acceptance_criteria = criteria if isinstance(criteria, list) else []
    decision = select_model(prompt, mode, effort, acceptance_criteria)

    if decision.model == SOL_MODEL:
        if decision.orchestration_eligible:
            variant = "C" if decision.dependency_ambiguity else "B"
        else:
            variant = "A"
    elif decision.model == TERRA_MODEL:
        variant = "D" if decision.orchestration_eligible else "E"
    else:
        variant = "F"

    reasons = [
        f"model={decision.model}",
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
    for key in ("model", "reason", "strategy", "effort"):
        features.pop(key, None)

    return {
        "router_version": "shared-policy",
        "mode": mode,
        "effort": decision.effort,
        "variant": variant,
        "route": VARIANT_LABELS[variant],
        "selected_model": decision.model,
        "features": features,
        "reasons": reasons,
    }
