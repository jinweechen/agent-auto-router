#!/usr/bin/env python3
"""Build a Desktop-native staged agent plan without launching a process."""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
from typing import Any, Iterable

from host_permissions import HostPermissions, parse_host_permissions, workdir_is_writable
from model_registry import TIER_RANK, load_model_registry, registry_digest, strip_backend_prefix
from orchestration_profiles import load_orchestration_profiles
from policy_learning import ROUTE_FEATURES


SCHEMA = "agent-auto-router.desktop-plan.v3"
EXECUTION_REPORT_SCHEMA = "agent-auto-router.execution-report.v1"
DIRECT_VARIANTS = frozenset({"A", "E", "F"})
ORCHESTRATED_VARIANTS = frozenset({"B", "C", "D"})
DEFAULT_WORKER_LIMIT = {"B": 3, "C": 3, "D": 2}
DEFAULT_STAGE_TIMEOUT_MS = 30 * 60 * 1000
EXTENDED_STAGE_TIMEOUT_MS = 60 * 60 * 1000
DEFAULT_TOTAL_TIMEOUT_MS = 4 * 60 * 60 * 1000
INTERRUPT_GRACE_TIMEOUT_MS = 30 * 1000
EXTENDED_REASONING_EFFORTS = frozenset({"xhigh", "max", "ultra"})
TERMINAL_STATES = (
    "succeeded",
    "failed",
    "blocked",
    "cancelled",
    "timed_out",
    "orphaned",
)


def _normalized_models(values: Iterable[str]) -> frozenset[str]:
    return frozenset(value.strip() for value in values if value.strip())


def _blocked(plan: dict[str, Any], code: str, message: str) -> dict[str, Any]:
    plan["status"] = "blocked"
    plan["executionRequested"] = False
    plan["plannedAgentCalls"] = 0
    plan["agents"] = []
    plan["stages"] = []
    plan["hostContract"].update({
        "action": "blocked",
        "maxAgents": 0,
        "maxParallelAgents": 0,
        "onlyWriter": None,
    })
    plan["blocked"] = {"code": code, "message": message}
    return plan


def _positive_parallel_children(value: int) -> int:
    if isinstance(value, bool) or value < 1:
        raise ValueError("max_parallel_children must be at least 1")
    return value


def _grader_enabled(decision: dict[str, Any], variant: str) -> bool:
    return bool(decision.get("high_risk")) or variant in {"B", "C"}


def _stage_timeout_ms(reasoning_effort: str) -> int:
    if reasoning_effort in EXTENDED_REASONING_EFFORTS:
        return EXTENDED_STAGE_TIMEOUT_MS
    return DEFAULT_STAGE_TIMEOUT_MS


def _role_order(variant: str, include_grader: bool) -> tuple[str, ...]:
    if variant in DIRECT_VARIANTS:
        roles = ["direct"]
    elif variant == "C":
        roles = ["planner", "dispatcher", "worker", "reviewer"]
    elif variant in {"B", "D"}:
        roles = ["planner", "worker", "reviewer"]
    else:
        raise ValueError(f"unsupported Desktop variant: {variant}")
    if include_grader:
        roles.append("grader")
    return tuple(roles)


def _dependencies(variant: str, role: str) -> list[str]:
    if role in {"direct", "planner"}:
        return []
    if role == "dispatcher":
        return ["planner"]
    if role == "worker":
        return ["dispatcher" if variant == "C" else "planner"]
    if role == "reviewer":
        return ["worker"]
    if role == "grader":
        return ["direct" if variant in DIRECT_VARIANTS else "reviewer"]
    raise ValueError(f"unsupported Desktop role: {role}")


def _task_source(role: str) -> str:
    return {
        "direct": "desktop-current-user-task",
        "planner": "desktop-current-user-task",
        "dispatcher": "planner-result",
        "worker": "coordinator-assigned-independent-subtask",
        "reviewer": "desktop-task-and-upstream-results",
        "grader": "desktop-task-and-final-writer-result",
    }[role]


def _runtime_role_model(
    registry: Any,
    preferred: Any,
    available: frozenset[str],
    *,
    role: str,
    required_capabilities: Iterable[str] = (),
) -> tuple[Any, str]:
    preferred_bare = strip_backend_prefix(preferred.model_id, "codex")
    if preferred_bare in available:
        return preferred, "profile-exact"
    required = frozenset(required_capabilities)
    candidates = [
        model
        for model in registry.models
        if model.enabled
        and model.auto_eligible
        and model.backend == "codex"
        and role in model.allowed_roles
        and required.issubset(model.capabilities)
        and TIER_RANK[model.tier] >= TIER_RANK[preferred.tier]
        and strip_backend_prefix(model.model_id, "codex") in available
    ]
    if not candidates:
        raise ValueError(
            f"no declared Desktop model can satisfy role={role} at tier>={preferred.tier}"
        )
    resolved = sorted(
        candidates,
        key=lambda model: (TIER_RANK[model.tier], model.priority, model.model_id),
    )[0]
    return resolved, "runtime-tier-upgrade"


def build_desktop_plan(
    route: dict[str, Any],
    available_models: Iterable[str],
    *,
    workdir: pathlib.Path | str,
    host_permissions: HostPermissions | dict[str, Any] | str | None,
    max_parallel_children: int,
    requested_sandbox: str = "inherit",
    dry_run: bool = False,
) -> dict[str, Any]:
    """Return a host-consumable plan; never executes a process or handles credentials."""
    execution_plan = route.get("executionPlan")
    if not isinstance(execution_plan, dict):
        raise ValueError("route.executionPlan must be an object")

    selected_model = route.get("selectedModel")
    route_id = route.get("routeId")
    effort = execution_plan.get("effort")
    topology = execution_plan.get("topology")
    variant = execution_plan.get("variant")
    context = execution_plan.get("context")
    decision = route.get("decision") if isinstance(route.get("decision"), dict) else {}
    policy = route.get("policy") if isinstance(route.get("policy"), dict) else {}
    registry_payload = route.get("registry") if isinstance(route.get("registry"), dict) else {}
    if not isinstance(selected_model, str) or not selected_model:
        raise ValueError("route.selectedModel must be a non-empty string")
    if not isinstance(route_id, str) or not route_id:
        raise ValueError("route.routeId must be a non-empty string")
    if not isinstance(effort, str) or not effort:
        raise ValueError("route.executionPlan.effort must be a non-empty string")
    max_model_calls = execution_plan.get("maxModelCalls")
    if isinstance(max_model_calls, bool) or not isinstance(max_model_calls, int) or max_model_calls < 1:
        raise ValueError("route.executionPlan.maxModelCalls must be a positive integer")
    child_capacity = _positive_parallel_children(max_parallel_children)
    resolved_workdir = pathlib.Path(workdir).resolve(strict=True)
    if not resolved_workdir.is_dir():
        raise ValueError(f"workdir must be a directory: {resolved_workdir}")
    permissions = (
        host_permissions
        if isinstance(host_permissions, HostPermissions)
        else parse_host_permissions(host_permissions)
        if host_permissions is not None
        else None
    )
    permission_plan = permissions.as_plan(requested_sandbox) if permissions else None
    effective_sandbox = permission_plan["effectiveSandbox"] if permission_plan else "read-only"
    would_write = effective_sandbox != "read-only"

    plan: dict[str, Any] = {
        "schema": SCHEMA,
        "status": "ready",
        "executionBackend": "desktop",
        "routeId": route_id,
        "selectedModel": selected_model,
        "effort": effort,
        "topology": topology,
        "variant": variant,
        "context": context if isinstance(context, dict) else None,
        "modelCalls": 0,
        "modelCallsScope": "routing",
        "routingModelCalls": 0,
        "executionRequested": not dry_run,
        "plannedAgentCalls": 0,
        "wouldPlanAgentCalls": {"minimum": 0, "maximum": 0},
        "callBudget": {
            "maximum": max_model_calls,
            "hardStopBeforeSpawn": True,
        },
        "routing": {
            "strategy": decision.get("strategy"),
            "reason": decision.get("reason"),
            "targetTier": decision.get("target_tier"),
            "policyVersion": policy.get("version"),
            "policyDigest": policy.get("digest"),
            "registryDigest": registry_payload.get("digest"),
        },
        "agents": [],
        "stages": [],
        "hostContract": {
            "action": "report_plan" if dry_run else "spawn_agents",
            "maxAgents": 0,
            "maxParallelAgents": 0,
            "declaredMaxParallelChildren": child_capacity,
            "onlyWriter": None,
            "permissions": permission_plan,
            "fullHistoryForkAllowed": False,
            "silentModelOrProviderFallback": False,
            "roleModelResolution": "profile-exact-or-declared-runtime-tier-upgrade",
            "workspaceSharing": "shared",
            "readOnlyRoleEnforcement": "instructions-plus-workspace-state-check",
            "terminalReconciliation": "required",
            "workspaceChangeReporting": "coordinator-authoritative",
        },
        "coordination": {
            "mode": "staged-dag",
            "singleWriter": True,
            "parallelRoles": ["worker"],
            "stopOnUnexpectedWorkspaceChange": True,
            "stopOnRoleFailure": True,
            "retryTimedOutMaxEffortRoles": False,
            "runStateMachine": [
                "pending",
                "ready",
                "running",
                "succeeded",
                "failed",
                "blocked",
                "cancelled",
                "timed_out",
                "orphaned",
            ],
            "timeoutPolicy": {
                "defaultStageTimeoutMs": DEFAULT_STAGE_TIMEOUT_MS,
                "extendedStageTimeoutMs": EXTENDED_STAGE_TIMEOUT_MS,
                "extendedReasoningEfforts": sorted(EXTENDED_REASONING_EFFORTS),
                "totalTimeoutMs": DEFAULT_TOTAL_TIMEOUT_MS,
                "totalDeadlineStartsAt": "first-agent-spawn",
                "onStageTimeout": "mark_timed_out_then_interrupt",
                "onTotalTimeout": "mark_run_timed_out_interrupt_all_active_block_dependents",
                "activeStageOutcome": "timed_out",
                "unstartedDependentStageOutcome": "blocked",
                "automaticRetry": False,
            },
            "terminalReconciliation": {
                "trackLaunchedAgentIds": True,
                "authoritativeSignals": [
                    "final_status_notification",
                    "child_thread_completed",
                    "terminal_tool_result",
                ],
                "advisorySignals": ["list_agents"],
                "conflictPolicy": "authoritative-terminal-wins",
                "staleRunningPolicy": "record_without_relaunch",
                "lateTerminalAfterTimeoutPolicy": "record_child_terminal_preserve_timeout_outcome",
                "requiredBeforeDependentStage": True,
                "requiredBeforeFinalResponse": True,
            },
            "cleanupPolicy": {
                "mode": "try-finally",
                "interruptUnresolvedAgents": True,
                "skipAuthoritativelyTerminalAgents": True,
                "releaseWriterClaim": True,
                "reconcileAfterInterrupt": True,
                "interruptGraceTimeoutMs": INTERRUPT_GRACE_TIMEOUT_MS,
                "interruptTerminalSignals": [
                    "final_status_notification",
                    "child_thread_completed",
                    "terminal_tool_result",
                ],
                "orphanedOnlyAfterGraceTimeout": True,
                "unresolvedAfterInterruptState": "orphaned",
            },
            "workspaceChangeReconciliation": {
                "sourceOfTruth": "coordinator-workdir",
                "captureBaselineBeforeFirstSpawn": True,
                "captureFinalAfterCleanup": True,
                "snapshotTool": "scripts/desktop_workspace_snapshot.py",
                "baselineStorage": "outside-child-writable-roots",
                "forbiddenRootsSource": "effective-permissions-writableRoots",
                "protectedPathValidation": True,
                "manifestFormat": "path-type-mode-size-sha256-plus-git-status-v1",
                "gitPathEnumeration": "ls-files-cached-others-exclude-standard-z",
                "gitStatusFormat": "porcelain-v1-z-untracked-files-all",
                "nonGitFallback": "deterministic-path-content-manifest",
                "comparison": "baseline-to-final-content-identity",
                "childPatchEvents": "advisory-only",
                "authoritativeChangedPaths": "runChangedPaths",
                "authoritativeChangedFileCount": "runChangedFileCount",
                "reportPreexistingDirtyPaths": True,
                "reportFinalDirtyPaths": True,
                "failOnUnexpectedReadOnlyChange": True,
            },
            "writerClaim": {
                "mode": "exclusive",
                "claimId": f"{route_id}:workspace-writer",
                "ownerRole": None,
                "acquireAfterDependencies": True,
                "conflictPolicy": "block",
            },
            "auditEventTypes": [
                "stage_started",
                "stage_succeeded",
                "stage_failed",
                "stage_timed_out",
                "run_timed_out",
                "stage_orphaned",
                "agent_interrupted",
                "post_interrupt_reconciled",
                "terminal_reconciled",
                "workspace_reconciled",
                "writer_claim_acquired",
                "writer_claim_released",
            ],
            "auditPayloadPolicy": "metadata-only-no-task-or-child-output",
        },
        "privacy": {
            "semantics": "planner-guarantees",
            "taskIncludedInPlan": False,
            "credentialsRead": False,
            "credentialsForwarded": False,
            "desktopAppServerAttached": False,
        },
        "blocked": None,
    }

    if permissions is None:
        return _blocked(
            plan,
            "desktop_host_permissions_required",
            "Automatic Desktop execution requires a trusted current-turn host permission snapshot.",
        )
    if would_write and not workdir_is_writable(resolved_workdir, permissions):
        return _blocked(
            plan,
            "desktop_workdir_not_writable",
            "The selected workdir is outside the writable roots declared by the host.",
        )
    if not (
        (topology == "direct" and variant in DIRECT_VARIANTS)
        or (topology == "orchestrated" and variant in ORCHESTRATED_VARIANTS)
    ):
        return _blocked(
            plan,
            "desktop_topology_invalid",
            "The selected topology and A-F variant are inconsistent.",
        )

    try:
        selected_bare = strip_backend_prefix(selected_model, "codex")
    except ValueError:
        return _blocked(
            plan,
            "desktop_backend_unsupported",
            f"Desktop v3 supports only the codex backend; selected model: {selected_model}",
        )
    available = _normalized_models(available_models)
    if selected_bare not in available:
        return _blocked(
            plan,
            "desktop_model_unavailable",
            f"Selected model is not declared available by the Desktop runtime: {selected_model}",
        )

    registry = load_model_registry()
    expected_registry_digest = registry_payload.get("digest")
    if not isinstance(expected_registry_digest, str) or not expected_registry_digest:
        return _blocked(
            plan,
            "desktop_registry_digest_required",
            "Desktop v3 requires the selector's trusted model-registry digest.",
        )
    if expected_registry_digest != registry_digest(registry):
        return _blocked(
            plan,
            "desktop_registry_changed",
            "The model registry changed after routing; rerun selection before spawning agents.",
        )
    required_capabilities = tuple(decision.get("required_capabilities") or ())
    if bool(decision.get("high_risk")) and "high-risk-primary" not in required_capabilities:
        required_capabilities = (*required_capabilities, "high-risk-primary")
    try:
        selected_spec = registry.get(selected_model, role="direct", backend="codex")
    except ValueError as exc:
        return _blocked(plan, "desktop_selected_model_invalid", str(exc))
    if bool(decision.get("high_risk")) and (
        selected_spec.tier != "frontier"
        or not frozenset(required_capabilities).issubset(selected_spec.capabilities)
    ):
        return _blocked(
            plan,
            "desktop_high_risk_model_invalid",
            "High-risk Desktop execution requires a frontier high-risk-primary model.",
        )

    include_grader = _grader_enabled(decision, str(variant))
    roles = _role_order(str(variant), include_grader)
    final_role = "direct" if variant in DIRECT_VARIANTS else "reviewer"
    minimum_calls = len(roles)
    if minimum_calls > max_model_calls:
        return _blocked(
            plan,
            "desktop_agent_call_budget_insufficient",
            f"The route requires at least {minimum_calls} agent calls but allows {max_model_calls}.",
        )
    worker_limit = 1
    if variant in ORCHESTRATED_VARIANTS:
        fixed_calls = minimum_calls - 1
        worker_limit = min(
            DEFAULT_WORKER_LIMIT[str(variant)],
            child_capacity,
            max_model_calls - fixed_calls,
        )
    profiles = load_orchestration_profiles()

    agents: list[dict[str, Any]] = []
    for role in roles:
        if role == "direct":
            model_id = selected_model
            role_effort = effort
        else:
            assignment = profiles.assignment(str(variant), role)
            final_requirements = required_capabilities if role == final_role else ()
            try:
                preferred_spec = assignment.resolve(
                    registry,
                    role,
                    required_capabilities=final_requirements,
                    required_tier="frontier" if final_requirements else None,
                    backends=("codex",),
                )
                spec, model_resolution = _runtime_role_model(
                    registry,
                    preferred_spec,
                    available,
                    role=role,
                    required_capabilities=final_requirements,
                )
            except ValueError as exc:
                return _blocked(plan, "desktop_role_resolution_failed", str(exc))
            model_id = spec.model_id
            role_effort = assignment.effort
        if role == "direct":
            preferred_model_id = model_id
            model_resolution = "selected-exact"
        else:
            preferred_model_id = preferred_spec.model_id
        try:
            bare_model = strip_backend_prefix(model_id, "codex")
        except ValueError:
            return _blocked(
                plan,
                "desktop_role_backend_unsupported",
                f"Desktop role {role} resolved to a non-Codex model: {model_id}",
            )
        if bare_model not in available:
            return _blocked(
                plan,
                "desktop_role_model_unavailable",
                f"Desktop role {role} requires a model not declared available: {model_id}",
            )
        role_would_write = role == final_role and would_write
        maximum_instances = worker_limit if role == "worker" else 1
        agents.append({
            "id": role,
            "role": role,
            "model": model_id,
            "preferredModel": preferred_model_id,
            "modelResolution": model_resolution,
            "reasoningEffort": role_effort,
            "forkTurns": "none",
            "workdir": str(resolved_workdir),
            "writer": role_would_write and not dry_run,
            "wouldWrite": role_would_write,
            "permissionIntent": effective_sandbox if role_would_write else "read-only",
            "taskSource": _task_source(role),
            "dependsOn": _dependencies(str(variant), role),
            "minimumInstances": 1,
            "maximumInstances": maximum_instances,
            "idempotencyKeyTemplate": f"{route_id}:{role}:{{instance}}",
            "maxAttempts": 1,
            "timeoutMs": _stage_timeout_ms(role_effort),
        })

    minimum_calls = len(agents)
    maximum_calls = sum(agent["maximumInstances"] for agent in agents)
    maximum_parallel = max(agent["maximumInstances"] for agent in agents)
    stages = [
        {
            "id": agent["id"],
            "mode": "parallel" if agent["id"] == "worker" and maximum_parallel > 1 else "serial",
            "agent": agent["id"],
            "dependsOn": list(agent["dependsOn"]),
            "minimumInstances": agent["minimumInstances"],
            "maximumInstances": agent["maximumInstances"],
            "timeoutMs": agent["timeoutMs"],
            "initialState": "pending",
            "terminalStates": list(TERMINAL_STATES),
        }
        for agent in agents
    ]
    plan["agents"] = agents
    plan["stages"] = stages
    plan["plannedAgentCalls"] = 0 if dry_run else maximum_calls
    plan["wouldPlanAgentCalls"] = {"minimum": minimum_calls, "maximum": maximum_calls}
    plan["hostContract"].update({
        "action": "report_plan" if dry_run else ("spawn_agent" if maximum_calls == 1 else "spawn_agents"),
        "maxAgents": 0 if dry_run else maximum_parallel,
        "maxParallelAgents": 0 if dry_run else maximum_parallel,
        "onlyWriter": final_role if would_write and not dry_run else None,
    })
    plan["coordination"]["writerClaim"]["ownerRole"] = final_role if would_write else None
    final_agent = next(agent for agent in agents if agent["role"] == final_role)
    report_features: dict[str, int | bool] = {}
    for source_payload in (decision, route.get("repository")):
        if not isinstance(source_payload, dict):
            continue
        for key, value in source_payload.items():
            if key in ROUTE_FEATURES and isinstance(value, (int, bool)):
                report_features[key] = value
    plan["learning"] = {
        "mode": "host-reported-guarded-auto",
        "reportSchema": EXECUTION_REPORT_SCHEMA,
        "submitAfterExecution": not dry_run,
        "submitWith": "python guarded_auto.py report --stdin",
        "route": {
            "routeId": route_id,
            "strategy": decision.get("strategy"),
            "effort": final_agent["reasoningEffort"],
            "selectorModel": decision.get("model"),
            "selectedModel": final_agent["model"],
            "targetTier": decision.get("target_tier"),
            "reason": decision.get("reason"),
            "features": report_features,
            "policyVersion": policy.get("version"),
            "policyDigest": policy.get("digest"),
            "modelRegistryDigest": registry_payload.get("digest"),
            "featureSchemaVersion": decision.get("feature_schema_version", 1),
            "explicitOverride": bool(route.get("explicitOverride", False)),
        },
        "resultRequired": [
            "status",
            "durationMs",
            "verification",
            "validationConfigured",
            "escalated",
            "attemptCount",
        ],
        "privacy": "metadata-only-no-task-or-agent-output",
    }
    return plan


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--available-model", action="append", default=[])
    parser.add_argument("--workdir", type=pathlib.Path, required=True)
    parser.add_argument("--max-parallel-children", type=int, required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--sandbox",
        choices=("inherit", "read-only", "workspace-write", "danger-full-access"),
        default="inherit",
    )
    parser.add_argument("--host-permissions-json", required=True)
    args = parser.parse_args()
    try:
        route = json.load(sys.stdin)
        if not isinstance(route, dict):
            raise ValueError("route input must be a JSON object")
        plan = build_desktop_plan(
            route,
            args.available_model,
            workdir=args.workdir,
            host_permissions=args.host_permissions_json,
            max_parallel_children=args.max_parallel_children,
            requested_sandbox=args.sandbox,
            dry_run=args.dry_run,
        )
    except (json.JSONDecodeError, OSError, ValueError) as exc:
        parser.error(str(exc))
    print(json.dumps(plan, ensure_ascii=True))
    return 0 if plan["status"] == "ready" else 2


if __name__ == "__main__":
    raise SystemExit(main())
