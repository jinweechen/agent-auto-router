#!/usr/bin/env python3
"""Build a Desktop-native execution plan without launching Codex CLI."""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
from typing import Any, Iterable


SCHEMA = "agent-auto-router.desktop-plan.v1"
DIRECT_VARIANTS = frozenset({"A", "E", "F"})


def _normalized_models(values: Iterable[str]) -> frozenset[str]:
    return frozenset(value.strip() for value in values if value.strip())


def _blocked(plan: dict[str, Any], code: str, message: str) -> dict[str, Any]:
    plan["status"] = "blocked"
    plan["plannedAgentCalls"] = 0
    plan["agent"] = None
    plan["blocked"] = {"code": code, "message": message}
    return plan


def build_desktop_plan(
    route: dict[str, Any],
    available_models: Iterable[str],
    *,
    workdir: pathlib.Path | str,
    requested_sandbox: str = "workspace-write",
    dry_run: bool = False,
) -> dict[str, Any]:
    """Return a host-consumable plan; never executes a process or handles credentials."""
    execution_plan = route.get("executionPlan")
    if not isinstance(execution_plan, dict):
        raise ValueError("route.executionPlan must be an object")

    selected_model = route.get("selectedModel")
    effort = execution_plan.get("effort")
    topology = execution_plan.get("topology")
    variant = execution_plan.get("variant")
    context = execution_plan.get("context")
    decision = route.get("decision") if isinstance(route.get("decision"), dict) else {}
    policy = route.get("policy") if isinstance(route.get("policy"), dict) else {}
    registry = route.get("registry") if isinstance(route.get("registry"), dict) else {}
    if not isinstance(selected_model, str) or not selected_model:
        raise ValueError("route.selectedModel must be a non-empty string")
    if not isinstance(effort, str) or not effort:
        raise ValueError("route.executionPlan.effort must be a non-empty string")
    resolved_workdir = pathlib.Path(workdir).resolve(strict=True)
    if not resolved_workdir.is_dir():
        raise ValueError(f"workdir must be a directory: {resolved_workdir}")

    plan: dict[str, Any] = {
        "schema": SCHEMA,
        "status": "ready",
        "executionBackend": "desktop",
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
        "plannedAgentCalls": 0 if dry_run else 1,
        "routing": {
            "strategy": decision.get("strategy"),
            "reason": decision.get("reason"),
            "targetTier": decision.get("target_tier"),
            "policyVersion": policy.get("version"),
            "policyDigest": policy.get("digest"),
            "registryDigest": registry.get("digest"),
        },
        "agent": {
            "role": "direct",
            "model": selected_model,
            "reasoningEffort": effort,
            "forkTurns": "none",
            "workdir": str(resolved_workdir),
            "writer": requested_sandbox == "workspace-write" and not dry_run,
            "wouldWrite": requested_sandbox == "workspace-write",
            "taskSource": "desktop-current-user-task",
        },
        "hostContract": {
            "action": "report_plan" if dry_run else "spawn_agent",
            "maxAgents": 0 if dry_run else 1,
            "onlyRole": "direct",
            "onlyWriter": (
                "direct" if requested_sandbox == "workspace-write" and not dry_run else None
            ),
            "permissions": "inherit-current-desktop-task",
            "fullHistoryForkAllowed": False,
            "silentModelOrProviderFallback": False,
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

    if topology != "direct" or variant not in DIRECT_VARIANTS:
        return _blocked(
            plan,
            "desktop_multi_role_topology_unsupported",
            "Desktop v1 supports exactly one direct child agent; the selected route requires multi-role orchestration.",
        )

    if selected_model not in _normalized_models(available_models):
        return _blocked(
            plan,
            "desktop_model_unavailable",
            f"Selected model is not declared available by the Desktop runtime: {selected_model}",
        )

    if requested_sandbox == "danger-full-access":
        return _blocked(
            plan,
            "desktop_sandbox_unsupported",
            "Desktop v1 cannot request danger-full-access; it inherits the current Desktop task permissions.",
        )
    return plan


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--available-model", action="append", default=[])
    parser.add_argument("--workdir", type=pathlib.Path, required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--sandbox",
        choices=("read-only", "workspace-write", "danger-full-access"),
        default="workspace-write",
    )
    args = parser.parse_args()
    try:
        route = json.load(sys.stdin)
        if not isinstance(route, dict):
            raise ValueError("route input must be a JSON object")
        plan = build_desktop_plan(
            route,
            args.available_model,
            workdir=args.workdir,
            requested_sandbox=args.sandbox,
            dry_run=args.dry_run,
        )
    except (json.JSONDecodeError, OSError, ValueError) as exc:
        parser.error(str(exc))
    print(json.dumps(plan, ensure_ascii=True))
    return 0 if plan["status"] == "ready" else 2


if __name__ == "__main__":
    raise SystemExit(main())
