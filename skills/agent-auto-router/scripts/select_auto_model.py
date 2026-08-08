#!/usr/bin/env python3
"""Select a GPT-5.6 model without changing Codex configuration."""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from dataclasses import asdict, replace
from pathlib import Path

from model_registry import load_model_registry, registry_digest
from execution_plan import build_execution_plan
from repository_context import inspect_repository
from routing_policy import (
    EFFORTS,
    STRATEGIES,
    ModelDecision,
    RoutingPolicy,
    load_active_policy,
    load_policy_file,
    policy_digest,
    select_model,
)

Decision = ModelDecision

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--strategy", choices=STRATEGIES, default="balance")
    parser.add_argument("--effort", choices=("auto", *EFFORTS), default="auto")
    parser.add_argument("--model-choice", default="auto")
    parser.add_argument("--policy-file", type=Path)
    parser.add_argument("--state-dir", type=Path)
    parser.add_argument("--workdir", type=Path)
    parser.add_argument("--ignore-active-policy", action="store_true")
    route_input = parser.add_mutually_exclusive_group(required=True)
    route_input.add_argument("--text")
    route_input.add_argument("--stdin", action="store_true")
    args = parser.parse_args()
    prompt = sys.stdin.read() if args.stdin else args.text
    try:
        if args.policy_file:
            policy = load_policy_file(args.policy_file)
            policy_source = str(args.policy_file)
        elif args.ignore_active_policy:
            policy = RoutingPolicy()
            policy_source = "builtin"
        else:
            policy, policy_source = load_active_policy(args.state_dir)
        registry = load_model_registry()
        routing_effort = "medium" if args.effort == "auto" else args.effort
        repository_features = (
            inspect_repository(args.workdir, prompt or "") if args.workdir else None
        )
        if repository_features:
            repository_features.pop("files", None)
        decision = select_model(
            prompt or "", args.strategy, routing_effort, policy=policy, registry=registry,
            repository_features=repository_features,
        )
        if args.model_choice == "auto":
            selected = registry.get(decision.model, role="direct")
            explicit_override = False
        else:
            selected = registry.get(args.model_choice, role="direct")
            explicit_override = True
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        parser.error(str(exc))
    if explicit_override:
        plan_decision = replace(
            decision, model=selected.model_id, target_tier=selected.tier
        )
        explicit_plan_effort = (
            selected.default_effort if args.effort == "auto" else args.effort
        )
        execution_plan = build_execution_plan(plan_decision, explicit_plan_effort)
        execution_plan["effortSource"] = (
            "registry-default" if args.effort == "auto" else "explicit"
        )
        execution_plan["escalation"]["eligible"] = False
    else:
        execution_plan = build_execution_plan(
            decision, None if args.effort == "auto" else args.effort
        )
    next_tier = (
        execution_plan["escalation"]["nextTier"]
        if execution_plan["escalation"]["eligible"]
        else None
    )
    if next_tier:
        next_model = registry.resolve_tier(next_tier, role="direct")
        execution_plan["escalation"]["nextModel"] = next_model.model_id
        execution_plan["escalation"]["nextEffort"] = (
            "high" if next_tier == "frontier" else "medium"
        )
    else:
        execution_plan["escalation"]["nextModel"] = None
        execution_plan["escalation"]["nextEffort"] = None
    print(json.dumps({
        "routeId": str(uuid.uuid4()),
        "decision": asdict(decision),
        "selectedModel": selected.model_id,
        "selectedTier": selected.tier,
        "selectedDefaultEffort": selected.default_effort,
        "executionPlan": execution_plan,
        "repository": repository_features,
        "explicitOverride": explicit_override,
        "policy": {
            "version": policy.policy_version,
            "digest": policy_digest(policy),
            "source": policy_source,
        },
        "registry": {
            "source": registry.source,
            "digest": registry_digest(registry),
            "enabledModels": list(registry.enabled_model_ids),
            "autoModels": list(registry.auto_model_ids),
        },
        "modelCalls": 0,
    }, ensure_ascii=True))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
