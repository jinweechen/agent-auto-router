#!/usr/bin/env python3
"""Build a generic host execution plan without launching any process."""

from __future__ import annotations

import argparse
import json
import pathlib
import shutil
import sys
from typing import Any, Iterable

from codex_cli_adapter import codex_cli_available
from model_registry import load_model_registry


SCHEMA = "agent-auto-router.host-plan.v1"
DIRECT_VARIANTS = frozenset({"A", "E", "F"})
ORCHESTRATED_VARIANTS = frozenset({"B", "C", "D"})


def _blocked(plan: dict[str, Any], code: str, message: str) -> dict[str, Any]:
    plan["status"] = "blocked"
    plan["plannedCalls"] = 0
    plan["agent"] = None
    plan["blocked"] = {"code": code, "message": message}
    plan["hostContract"]["action"] = "blocked"
    return plan


def detect_available_backends(
    route: dict[str, Any], known_backends: Iterable[str] | None = None
) -> list[str]:
    """Probe locally available CLIs for the declared backend set."""
    del route  # The route never controls executable discovery.
    declared = tuple(known_backends or load_model_registry().backends)
    found: list[str] = []
    for name in declared:
        if name == "codex":
            if codex_cli_available():
                found.append(name)
        elif shutil.which(name):
            found.append(name)
    return found


def _orchestration_roles(variant: str) -> list[str]:
    roles = {
        "B": ["planner", "worker", "reviewer", "grader"],
        "C": ["planner", "dispatcher", "worker", "reviewer", "grader"],
        "D": ["planner", "worker", "reviewer", "grader"],
    }
    return roles.get(variant, [])


def build_host_plan(
    route: dict[str, Any],
    *,
    workdir: pathlib.Path | str,
    available_backends: Iterable[str] | None = None,
    dry_run: bool = False,
    known_backends: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Return a host-neutral plan; the caller executes only ``action.kind``."""
    selected_model = route.get("selectedModel")
    if not isinstance(selected_model, str) or not selected_model:
        raise ValueError("route.selectedModel must be a non-empty string")

    execution_plan = route.get("executionPlan")
    if not isinstance(execution_plan, dict):
        raise ValueError("route.executionPlan must be an object")

    effort = execution_plan.get("effort")
    topology = execution_plan.get("topology")
    variant = execution_plan.get("variant")
    context = execution_plan.get("context")
    if not isinstance(effort, str) or not effort:
        raise ValueError("route.executionPlan.effort must be a non-empty string")

    resolved_workdir = pathlib.Path(workdir).resolve(strict=True)
    if not resolved_workdir.is_dir():
        raise ValueError(f"workdir must be a directory: {resolved_workdir}")

    registry = load_model_registry()
    declared_backends = tuple(known_backends or registry.backends)
    selected_spec = registry.get(selected_model)
    selected_backend = selected_spec.backend
    backends = (
        list(available_backends)
        if available_backends is not None
        else detect_available_backends(route, declared_backends)
    )
    unknown_backends = sorted(set(backends) - set(declared_backends))

    decision = route.get("decision") if isinstance(route.get("decision"), dict) else {}
    policy = route.get("policy") if isinstance(route.get("policy"), dict) else {}
    route_registry = route.get("registry") if isinstance(route.get("registry"), dict) else {}

    direct = topology == "direct" and variant in DIRECT_VARIANTS
    orchestrated = topology == "orchestrated" and variant in ORCHESTRATED_VARIANTS
    roles = _orchestration_roles(str(variant)) if orchestrated else ["direct"]
    final_writer = "reviewer" if orchestrated else "direct"

    plan: dict[str, Any] = {
        "schema": SCHEMA,
        "status": "ready",
        "executionBackend": "host",
        "routeId": route.get("routeId"),
        "selectedModel": selected_model,
        "effort": effort,
        "topology": topology,
        "variant": variant,
        "context": context if isinstance(context, dict) else None,
        "modelCalls": 0,
        "modelCallsScope": "routing",
        "routingModelCalls": 0,
        "executionRequested": not dry_run,
        "plannedCalls": 0 if dry_run else 1,
        "routing": {
            "strategy": decision.get("strategy"),
            "reason": decision.get("reason"),
            "targetTier": decision.get("target_tier"),
            "policyVersion": policy.get("version"),
            "policyDigest": policy.get("digest"),
            "registryDigest": route_registry.get("digest"),
        },
        "agent": (
            {
                "role": "direct",
                "model": selected_model,
                "reasoningEffort": effort,
                "workdir": str(resolved_workdir),
                "writer": not dry_run,
                "wouldWrite": True,
                "taskSource": "host-current-user-task",
            }
            if direct
            else None
        ),
        "orchestration": (
            {
                "roles": roles,
                "readOnlyRoles": [role for role in roles if role != final_writer],
                "onlyWriter": final_writer,
                "workdir": str(resolved_workdir),
                "taskSource": "host-current-user-task",
            }
            if orchestrated
            else None
        ),
        "hostContract": {
            "action": "report_plan" if dry_run else "pending",
            "maxAgents": 0 if orchestrated or dry_run else 1,
            "onlyRole": "direct" if direct else None,
            "onlyWriter": final_writer if not dry_run else None,
            "permissions": "inherit-current-host-task",
            "fullHistoryForkAllowed": False,
            "silentModelOrProviderFallback": False,
            "modelAccuracy": "exact",
        },
        "privacy": {
            "semantics": "planner-guarantees",
            "taskIncludedInPlan": False,
            "credentialsRead": False,
            "credentialsForwarded": False,
            "hostAppServerAttached": False,
        },
        "action": None,
        "blocked": None,
    }

    if unknown_backends:
        return _blocked(
            plan,
            "host_unknown_backend",
            f"Host plan received undeclared backends: {unknown_backends}",
        )
    if selected_backend not in declared_backends:
        return _blocked(
            plan,
            "host_selected_backend_unknown",
            f"Selected model uses an undeclared backend: {selected_backend}",
        )
    if not direct and not orchestrated:
        return _blocked(
            plan,
            "host_topology_unsupported",
            f"Host plan does not support topology={topology} variant={variant}",
        )

    if dry_run:
        plan["action"] = {"kind": "report_plan"}
        return plan

    if direct:
        if selected_backend in backends:
            action = {
                "kind": "cli",
                "backend": selected_backend,
                "model": selected_model,
                "effort": effort,
                "taskSource": "host-current-user-task",
            }
        else:
            action = {
                "kind": "host_execute",
                "modelAccuracy": "approximate",
                "effort": effort,
                "taskSource": "host-current-user-task",
                "note": (
                    "selected backend is unavailable; the host may execute with its own "
                    "model only after surfacing approximate model accuracy"
                ),
            }
    elif selected_backend in backends:
        action = {
            "kind": "orchestrate",
            "backend": selected_backend,
            "variant": variant,
            "taskSource": "host-current-user-task",
            "entrypoint": "invoke_orchestrated_task.py",
            "argv": [
                "--stdin",
                "--backend",
                selected_backend,
                "--variant",
                variant,
                "--workdir",
                str(resolved_workdir),
            ],
        }
    else:
        return _blocked(
            plan,
            "host_selected_backend_unavailable",
            "Orchestration requires the selected CLI backend; cross-backend fallback is forbidden.",
        )

    plan["action"] = action
    plan["hostContract"]["action"] = action["kind"]
    plan["hostContract"]["modelAccuracy"] = action.get("modelAccuracy", "exact")
    return plan


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workdir", type=pathlib.Path, required=True)
    parser.add_argument("--available-backends", default="auto")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    registry = load_model_registry()
    explicit_backends: list[str] | None
    if args.available_backends == "auto":
        explicit_backends = None
    else:
        explicit_backends = [
            item.strip() for item in args.available_backends.split(",") if item.strip()
        ]
        unknown = sorted(set(explicit_backends) - set(registry.backends))
        if unknown:
            parser.error(f"unknown backend: {', '.join(unknown)}")

    try:
        route = json.load(sys.stdin)
        if not isinstance(route, dict):
            raise ValueError("route input must be a JSON object")
        plan = build_host_plan(
            route,
            workdir=args.workdir,
            available_backends=explicit_backends,
            dry_run=args.dry_run,
            known_backends=registry.backends,
        )
    except (json.JSONDecodeError, OSError, ValueError) as exc:
        parser.error(str(exc))
    print(json.dumps(plan, ensure_ascii=True))
    return 0 if plan["status"] == "ready" else 2


if __name__ == "__main__":
    raise SystemExit(main())
