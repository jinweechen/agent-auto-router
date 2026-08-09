#!/usr/bin/env python3
"""Select a GPT-5.6 model without changing Codex configuration."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import uuid
from dataclasses import asdict, replace
from pathlib import Path

from model_registry import load_model_registry, registry_digest
from benchmark_priors import benchmark_priors_digest, load_benchmark_priors
from execution_plan import build_execution_plan
from repository_context import inspect_repository
from routing_policy import (
    EFFORTS,
    STRATEGIES,
    ModelDecision,
    RoutingPolicy,
    load_policy_file,
    load_policy_for_route,
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
    parser.add_argument("--available-backends", default="auto")
    parser.add_argument("--validation-configured", action="store_true")
    route_input = parser.add_mutually_exclusive_group(required=True)
    route_input.add_argument("--text")
    route_input.add_argument("--stdin", action="store_true")
    args = parser.parse_args()
    prompt = sys.stdin.read() if args.stdin else args.text
    route_id = str(uuid.uuid4())
    try:
        registry = load_model_registry()
        benchmark_priors = load_benchmark_priors(registry=registry)
        if args.policy_file:
            policy = load_policy_file(args.policy_file)
            policy_source = str(args.policy_file)
        elif args.ignore_active_policy:
            policy = RoutingPolicy()
            policy_source = "builtin"
        else:
            policy, policy_source = load_policy_for_route(
                args.state_dir,
                route_id,
                registry_digest_value=registry_digest(registry),
                benchmark_priors_digest_value=benchmark_priors_digest(benchmark_priors),
            )

        # Resolve available backends
        if args.available_backends == "auto":
            found: list[str] = []
            for bname in registry.backends:
                if shutil.which(bname):
                    found.append(bname)
            if found:
                available_backends = found
            else:
                available_backends = list(registry.backends.keys())
        else:
            available_backends = [b.strip() for b in args.available_backends.split(",")]
            for b in available_backends:
                if b not in registry.backends:
                    parser.error(
                        f"backend {b} is not declared in the model registry"
                    )

        routing_effort = "medium" if args.effort == "auto" else args.effort
        # allow_explicit_only when exactly one backend is explicitly named
        allow_explicit = (
            args.available_backends != "auto"
            and len(available_backends) == 1
        )
        repository_features = (
            inspect_repository(args.workdir, prompt or "") if args.workdir else None
        )
        if repository_features:
            repository_features.pop("files", None)
        decision = select_model(
            prompt or "", args.strategy, routing_effort, policy=policy, registry=registry,
            repository_features=repository_features,
            backends=available_backends,
            allow_explicit_only=allow_explicit,
            validation_configured=args.validation_configured,
            benchmark_priors=benchmark_priors,
        )
        if args.model_choice == "auto":
            selected = registry.get(decision.model, role="direct")
            explicit_override = False
        else:
            selected = registry.get(args.model_choice, role="direct")
            if selected.backend not in available_backends:
                raise ValueError(
                    f"model {selected.model_id} is not available on the requested backends: "
                    f"{available_backends}"
                )
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
        "routeId": route_id,
        "decision": asdict(decision),
        "selectedModel": selected.model_id,
        "selectedTier": selected.tier,
        "selectedDefaultEffort": selected.default_effort,
        "executionPlan": execution_plan,
        "allowExplicitOnly": allow_explicit,
        "availableBackends": list(available_backends),
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
        "benchmarkPriors": {
            "version": benchmark_priors.version,
            "asOf": benchmark_priors.as_of,
            "source": benchmark_priors.source,
            "digest": benchmark_priors_digest(benchmark_priors),
            "runtimeNetworkAccess": benchmark_priors.runtime_network_access,
            "evidenceModels": sorted(benchmark_priors.model_evidence),
            "signalsApplied": list(decision.benchmark_signals),
        },
        "modelCalls": 0,
    }, ensure_ascii=True))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
