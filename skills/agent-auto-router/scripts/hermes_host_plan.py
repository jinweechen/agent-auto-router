#!/usr/bin/env python3
"""Build a Hermes-host execution plan without launching any process."""

from __future__ import annotations

import argparse
import json
import pathlib
import shutil
import sys
from typing import Any, Iterable


SCHEMA = "agent-auto-router.host-plan.v1"
DIRECT_VARIANTS = frozenset({"A", "E", "F"})
KNOWN_BACKENDS = ("codex", "claude")


def _normalized_models(values: Iterable[str]) -> frozenset[str]:
    return frozenset(value.strip() for value in values if value.strip())


def _blocked(plan: dict[str, Any], code: str, message: str) -> dict[str, Any]:
    plan["status"] = "blocked"
    plan["plannedCalls"] = 0
    plan["agent"] = None
    plan["blocked"] = {"code": code, "message": message}
    return plan


def detect_available_backends(route: dict[str, Any]) -> list[str]:
    """Probe PATH for CLI backends; returns those found.

    The route JSON from select_auto_model.py includes a registry section
    but may not enumerate backend names.  We probe PATH with shutil.which
    for each backend in KNOWN_BACKENDS instead.
    """
    found: list[str] = []
    for name in KNOWN_BACKENDS:
        if shutil.which(name):
            found.append(name)
    return found


def build_hermes_plan(
    route: dict[str, Any],
    *,
    workdir: pathlib.Path | str,
    available_backends: Iterable[str] | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Return a host-consumable Hermes plan; never executes a process."""

    # --- validate inputs ----------------------------------------------------
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

    decision = route.get("decision") if isinstance(route.get("decision"), dict) else {}
    policy = route.get("policy") if isinstance(route.get("policy"), dict) else {}
    registry = route.get("registry") if isinstance(route.get("registry"), dict) else {}

    # --- resolve backends ---------------------------------------------------
    if available_backends is not None:
        backends = list(available_backends)
    else:
        backends = detect_available_backends(route)

    # --- resolve selected backend from model ID ------------------------------
    if ":" in selected_model:
        selected_backend = selected_model.split(":", 1)[0]
    else:
        selected_backend = "codex"

    # --- build action --------------------------------------------------------
    action: dict[str, Any] | None = None

    if topology == "direct":
        if selected_backend in backends:
            action = {
                "kind": "cli",
                "backend": selected_backend,
                "model": selected_model,
                "command": (
                    f"invoke_auto_task.ps1 -ExecutionBackend cli"
                    f" -Model {selected_model}"
                    f" -Effort {effort}"
                ),
            }
        else:
            action = {
                "kind": "host_execute",
                "modelAccuracy": "approximate",
                "note": (
                    "selected backend unavailable to Hermes;"
                    " host executes with its own model,"
                    " honoring the routed effort"
                ),
            }
    elif topology == "orchestrated":
        if backends:
            use_backend = (
                selected_backend if selected_backend in backends
                else next(iter(backends))
            )
            action = {
                "kind": "orchestrate",
                "backend": use_backend,
                "variant": variant,
                "command": (
                    f"invoke_orchestrated_task.py"
                    f" --backend {use_backend}"
                    f" --Variant {variant}"
                ),
            }
        else:
            # Helper to return early
            pass  # handled below
    else:
        pass  # handled below

    # --- construct base plan ------------------------------------------------
    plan: dict[str, Any] = {
        "schema": SCHEMA,
        "status": "ready",
        "executionBackend": "hermes",
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
            "registryDigest": registry.get("digest"),
        },
        "agent": {
            "role": "direct",
            "model": selected_model,
            "reasoningEffort": effort,
            "workdir": str(resolved_workdir),
            "writer": True,
            "taskSource": "hermes-current-user-task",
        },
        "hostContract": {
            "action": action["kind"] if action else "blocked",
            "maxAgents": 1,
            "onlyRole": "direct",
            "permissions": "inherit-current-hermes-task",
            "fullHistoryForkAllowed": False,
            "silentModelOrProviderFallback": False,
            "modelAccuracy": action.get("modelAccuracy", "exact") if action else "exact",
        },
        "privacy": {
            "semantics": "planner-guarantees",
            "taskIncludedInPlan": False,
            "credentialsRead": False,
            "credentialsForwarded": False,
            "hermesAppServerAttached": False,
        },
        "blocked": None,
    }

    # --- block unknown topology ----------------------------------------------
    if topology not in ("direct", "orchestrated"):
        return _blocked(
            plan,
            "hermes_unknown_topology",
            f"Hermes host plan does not recognise topology: {topology}",
        )

    # --- block orchestrated without backends ---------------------------------
    if topology == "orchestrated" and not backends:
        return _blocked(
            plan,
            "hermes_no_cli_backend",
            "Orchestration requires a CLI backend,"
            " but none of the routed backends are available on PATH.",
        )

    # --- wire action into the plan -------------------------------------------
    assert action is not None
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

    # Resolve explicit backends or "auto"
    explicit_backends: list[str] | None = None
    if args.available_backends == "auto":
        explicit_backends = None
    else:
        explicit_backends = [b.strip() for b in args.available_backends.split(",")]
        for b in explicit_backends:
            if b not in KNOWN_BACKENDS:
                parser.error(f"unknown backend: {b}")

    try:
        route = json.load(sys.stdin)
        if not isinstance(route, dict):
            raise ValueError("route input must be a JSON object")
        plan = build_hermes_plan(
            route,
            workdir=args.workdir,
            available_backends=explicit_backends,
            dry_run=args.dry_run,
        )
    except (json.JSONDecodeError, OSError, ValueError) as exc:
        parser.error(str(exc))
    print(json.dumps(plan, ensure_ascii=True))
    return 0 if plan["status"] == "ready" else 2


if __name__ == "__main__":
    raise SystemExit(main())
