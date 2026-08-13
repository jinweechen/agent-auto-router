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
from execution_plan import (
    ORCHESTRATION_POLICIES,
    build_execution_plan,
)
from model_affinity import (
    MODEL_AFFINITY_MODES,
    resolve_model_affinity,
    workspace_identity,
)
from policy_learning import default_feedback_path, load_maintained_feedback
from route_contract import ROUTE_DECISION_SCHEMA, build_route_decision
from repository_context import (
    build_repository_context,
    disabled_repository_inspection,
    inspect_repository,
)
from routing_policy import (
    EFFORTS,
    STRATEGIES,
    DEFAULT_STATE_DIR,
    ModelDecision,
    RoutingPolicy,
    load_policy_file,
    load_policy_for_route,
    matched_signal_terms,
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
    parser.add_argument("--feedback-file", type=Path)
    parser.add_argument("--workdir", type=Path)
    parser.add_argument(
        "--repository-context", choices=("auto", "off"), default="off"
    )
    policy_source_group = parser.add_mutually_exclusive_group()
    policy_source_group.add_argument("--use-active-policy", action="store_true")
    policy_source_group.add_argument("--ignore-active-policy", action="store_true")
    parser.add_argument("--available-backends", default="auto")
    parser.add_argument("--validation-configured", action="store_true")
    parser.add_argument(
        "--orchestration-policy",
        choices=ORCHESTRATION_POLICIES,
        default="direct",
    )
    parser.add_argument("--confirm-high-risk-orchestration", action="store_true")
    parser.add_argument(
        "--model-affinity",
        choices=MODEL_AFFINITY_MODES,
        default="off",
    )
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
        elif args.use_active_policy:
            policy, policy_source = load_policy_for_route(
                args.state_dir,
                route_id,
                registry_digest_value=registry_digest(registry),
                benchmark_priors_digest_value=benchmark_priors_digest(benchmark_priors),
            )
        else:
            policy = RoutingPolicy()
            policy_source = "builtin"

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
        # An explicit backend constraint is not an explicit model choice. Auto
        # must continue to honor autoEligible even when only one backend is
        # available. Explicit-only models may participate only after the user
        # names one through --model-choice.
        allow_explicit = args.model_choice != "auto"
        repository_inspection = None
        if args.workdir and args.repository_context == "auto":
            repository_inspection = inspect_repository(args.workdir, prompt or "")
            repository_features = dict(repository_inspection)
        elif args.workdir:
            repository_inspection = disabled_repository_inspection()
            repository_features = dict(repository_inspection)
        else:
            repository_features = None
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
        workspace_key = workspace_identity(args.workdir)
        state_dir = args.state_dir or DEFAULT_STATE_DIR
        if args.model_choice != "auto":
            selected = registry.get(args.model_choice, role="direct")
            if selected.backend not in available_backends:
                raise ValueError(
                    f"model {selected.model_id} is not available on the requested backends: "
                    f"{available_backends}"
                )
            explicit_override = True
            affinity = resolve_model_affinity(
                (),
                workspace_key=workspace_key,
                strategy=args.strategy,
                selector_model=decision.model,
                target_tier=decision.target_tier,
                registry=registry,
                available_backends=available_backends,
                required_capabilities=selected.capabilities,
                mode="off",
            )
            affinity.update({
                "selectedModel": selected.model_id,
                "selectedTier": selected.tier,
                "reason": "explicit-model-override",
            })
        else:
            explicit_override = False
            if args.model_affinity == "off":
                events = []
                affinity_error = None
            else:
                try:
                    events, _ = load_maintained_feedback(
                        args.feedback_file or default_feedback_path(state_dir),
                        apply=False,
                    )
                except (OSError, ValueError, json.JSONDecodeError) as exc:
                    events = []
                    affinity_error = type(exc).__name__
                else:
                    affinity_error = None
            affinity = resolve_model_affinity(
                events,
                workspace_key=workspace_key,
                strategy=args.strategy,
                selector_model=decision.model,
                target_tier=decision.target_tier,
                registry=registry,
                available_backends=available_backends,
                required_capabilities=decision.required_capabilities,
                mode=args.model_affinity,
            )
            if affinity_error is not None:
                affinity["reason"] = "feedback-evidence-unavailable"
                affinity["errorType"] = affinity_error
            selected = registry.get(str(affinity["selectedModel"]), role="direct")
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        parser.error(str(exc))
    plan_decision = replace(
        decision,
        model=selected.model_id,
        target_tier=selected.tier if explicit_override else decision.target_tier,
    )
    if explicit_override:
        explicit_plan_effort = (
            selected.default_effort if args.effort == "auto" else args.effort
        )
        execution_plan = build_execution_plan(
            plan_decision,
            explicit_plan_effort,
            orchestration_policy=args.orchestration_policy,
            confirm_high_risk_orchestration=args.confirm_high_risk_orchestration,
            model_affinity=affinity,
            selected_tier=selected.tier,
            required_tier=decision.target_tier,
        )
        execution_plan["effortSource"] = (
            "registry-default" if args.effort == "auto" else "explicit"
        )
        execution_plan["escalation"]["eligible"] = False
    else:
        execution_plan = build_execution_plan(
            plan_decision,
            None if args.effort == "auto" else args.effort,
            orchestration_policy=args.orchestration_policy,
            confirm_high_risk_orchestration=args.confirm_high_risk_orchestration,
            model_affinity=affinity,
            selected_tier=selected.tier,
            required_tier=decision.target_tier,
        )
        if selected.tier != decision.target_tier:
            execution_plan["escalation"].update(
                {
                    "eligible": False,
                    "nextTier": None,
                    "reason": "affinity-already-retained-stronger-tier",
                }
            )
    repository_context = None
    if args.workdir and repository_inspection is not None:
        if args.repository_context == "auto":
            context_text, context_metadata = build_repository_context(
                args.workdir,
                prompt or "",
                max_candidate_files=int(execution_plan["context"]["maxCandidateFiles"]),
                repo_map_tokens=int(execution_plan["context"]["repoMapTokens"]),
                repository_inspection=repository_inspection,
            )
        else:
            context_text = ""
            context_metadata = dict(repository_features or {})
            context_metadata.update({
                "candidate_files": 0,
                "context_chars": 0,
                "context_useful": False,
            })
        repository_context = {
            "text": context_text if context_metadata.get("context_useful") else "",
            "metadata": context_metadata,
        }
    next_tier = (
        execution_plan["escalation"]["nextTier"]
        if execution_plan["escalation"]["eligible"]
        else None
    )
    if next_tier:
        try:
            next_model = registry.resolve_tier(
                next_tier,
                role="direct",
                backends=available_backends,
            )
        except ValueError:
            execution_plan["escalation"]["eligible"] = False
            execution_plan["escalation"]["nextTier"] = None
            execution_plan["escalation"]["nextModel"] = None
            execution_plan["escalation"]["nextEffort"] = None
            execution_plan["escalation"]["unavailableReason"] = (
                "no_auto_eligible_model_for_available_backends"
            )
        else:
            execution_plan["escalation"]["nextModel"] = next_model.model_id
            execution_plan["escalation"]["nextEffort"] = (
                "high" if next_tier == "frontier" else "medium"
            )
            execution_plan["escalation"]["unavailableReason"] = None
    else:
        execution_plan["escalation"]["nextModel"] = None
        execution_plan["escalation"]["nextEffort"] = None
        execution_plan["escalation"]["unavailableReason"] = None
    decision_features = asdict(decision)
    for key in (
        "model", "target_tier", "required_capabilities", "reason", "strategy",
        "effort", "policy_version", "policy_digest", "registry_digest",
        "feature_schema_version",
    ):
        decision_features.pop(key, None)
    matched_signals = matched_signal_terms(prompt or "")
    route_decision = build_route_decision(
        route_id=route_id,
        task_text=prompt or "",
        strategy=args.strategy,
        effort=str(execution_plan["effort"]),
        selected_model=selected.model_id,
        selected_tier=selected.tier,
        selector_model=decision.model,
        target_tier=decision.target_tier,
        reason_code="explicit_model" if explicit_override else decision.reason,
        feature_schema_version=decision.feature_schema_version,
        features=decision_features,
        matched_signals=matched_signals,
        repository_mode=(
            args.repository_context if args.workdir else "not-requested"
        ),
        repository_metadata=(
            repository_context["metadata"]
            if isinstance(repository_context, dict)
            else repository_features
        ),
        execution_plan=execution_plan,
        policy_version=policy.policy_version,
        policy_digest=policy_digest(policy),
        registry_digest=registry_digest(registry),
        policy_source=policy_source,
        registry_source=registry.source,
        explicit_override=explicit_override,
        required_capabilities=tuple(decision.required_capabilities),
        workspace_key=workspace_key,
        model_affinity=affinity,
    )
    print(json.dumps({
        "schema": ROUTE_DECISION_SCHEMA,
        "routeDecision": route_decision,
        "routeId": route_id,
        "decision": asdict(decision),
        "selectedModel": selected.model_id,
        "selectedTier": selected.tier,
        "selectedDefaultEffort": selected.default_effort,
        "executionPlan": execution_plan,
        "matchedSignals": matched_signals,
        "allowExplicitOnly": allow_explicit,
        "availableBackends": list(available_backends),
        "repository": repository_features,
        "repositoryContext": repository_context,
        "explicitOverride": explicit_override,
        "workspaceKey": workspace_key,
        "modelAffinity": affinity,
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
