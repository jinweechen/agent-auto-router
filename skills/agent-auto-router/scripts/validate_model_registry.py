#!/usr/bin/env python3
"""Validate a trusted model registry and every configured orchestration role."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from model_registry import ModelRegistry, load_model_registry, registry_digest
from benchmark_priors import (
    BenchmarkPriors,
    benchmark_priors_digest,
    load_benchmark_priors,
)
from orchestration_profiles import OrchestrationProfiles, load_orchestration_profiles


def validate_registry_and_profiles(
    registry: ModelRegistry,
    profiles: OrchestrationProfiles,
    priors: BenchmarkPriors | None = None,
) -> dict[str, Any]:
    active_priors = priors or load_benchmark_priors(registry=registry)
    resolved_profiles: dict[str, dict[str, dict[str, str]]] = {}
    for variant in sorted(profiles.profiles):
        resolved_profiles[variant] = {}
        for role, assignment in profiles.profiles[variant].items():
            model = assignment.resolve(registry, role)
            resolved_profiles[variant][role] = {
                "model": model.model_id,
                "tier": model.tier,
                "effort": assignment.effort,
            }
    high_risk = registry.resolve_tier(
        "frontier", role="direct", required_capabilities=("high-risk-primary",)
    )
    high_risk_final_roles: dict[str, dict[str, str]] = {}
    for variant, role in {"A": "direct", "B": "reviewer", "C": "reviewer"}.items():
        model = profiles.assignment(variant, role).resolve(
            registry,
            role,
            required_capabilities=("high-risk-primary",),
            required_tier="frontier",
        )
        high_risk_final_roles[variant] = {"role": role, "model": model.model_id}
    explicit_only = [
        model.model_id
        for model in registry.models
        if model.enabled and not model.auto_eligible
    ]

    # Validate every declared backend has at least one enabled model
    backends_with_models: set[str] = set()
    for model in registry.models:
        if model.enabled:
            backends_with_models.add(model.backend)
    for bname in registry.backends:
        if bname not in backends_with_models:
            raise ValueError(f"backend {bname} has no enabled models")

    # Find the default backend
    default_backend: str | None = None
    for bname, binfo in registry.backends.items():
        if binfo.get("default"):
            default_backend = bname
            break

    return {
        "schemaVersion": 2,
        "valid": True,
        "registrySource": registry.source,
        "registryDigest": registry_digest(registry),
        "profilesSource": profiles.source,
        "backends": sorted(registry.backends.keys()),
        "defaultBackend": default_backend,
        "enabledModels": list(registry.enabled_model_ids),
        "autoModels": list(registry.auto_model_ids),
        "explicitOnlyModels": explicit_only,
        "highRiskPrimaryModel": high_risk.model_id,
        "highRiskFinalRoles": high_risk_final_roles,
        "resolvedProfiles": resolved_profiles,
        "benchmarkPriors": {
            "version": active_priors.version,
            "asOf": active_priors.as_of,
            "source": active_priors.source,
            "digest": benchmark_priors_digest(active_priors),
            "runtimeNetworkAccess": active_priors.runtime_network_access,
            "evidenceModels": sorted(active_priors.model_evidence),
        },
        "modelCalls": 0,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", type=Path)
    parser.add_argument("--profiles", type=Path)
    parser.add_argument("--benchmark-priors", type=Path)
    args = parser.parse_args()
    registry = load_model_registry(args.registry)
    profiles = load_orchestration_profiles(args.profiles)
    priors = load_benchmark_priors(args.benchmark_priors, registry=registry)
    print(
        json.dumps(
            validate_registry_and_profiles(registry, profiles, priors),
            ensure_ascii=True,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
