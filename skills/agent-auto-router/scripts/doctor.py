#!/usr/bin/env python3
"""Run privacy-safe, zero-model-call installation and registry diagnostics."""

from __future__ import annotations

import argparse
import json
import pathlib
import platform
import shutil
import sys
from typing import Any

from benchmark_priors import load_benchmark_priors
from codex_cli_adapter import codex_cli_available, resolve_codex_command
from guarded_auto import DEFAULT_CONFIG
from model_registry import load_model_registry
from orchestration_profiles import load_orchestration_profiles
from protocol_schemas import DOCTOR_SCHEMA, QUICK_PROFILES_SCHEMA
from quick_profiles import load_quick_profiles
from validate_model_registry import validate_registry_and_profiles


def _privacy_safe_validation(
    validation: dict[str, Any], *, verbose_paths: bool
) -> dict[str, Any]:
    if verbose_paths:
        return validation
    result = json.loads(json.dumps(validation))
    for key in ("registrySource", "profilesSource"):
        value = result.get(key)
        if isinstance(value, str):
            result[key] = f"packaged:{pathlib.Path(value).name}"
    benchmark = result.get("benchmarkPriors")
    if isinstance(benchmark, dict) and isinstance(benchmark.get("source"), str):
        benchmark["source"] = f"packaged:{pathlib.Path(benchmark['source']).name}"
    quick = result.get("quickProfiles")
    if isinstance(quick, dict) and isinstance(quick.get("source"), str):
        quick["source"] = f"packaged:{pathlib.Path(quick['source']).name}"
    return result


def build_diagnostic(
    max_review_age_days: int = 90, *, verbose_paths: bool = False
) -> dict[str, Any]:
    python_supported = sys.version_info >= (3, 10)
    powershell_path = shutil.which("pwsh") or shutil.which("powershell")
    git_path = shutil.which("git")
    claude_path = shutil.which("claude")
    powershell_available = bool(powershell_path)
    git_available = bool(git_path)
    claude_available = bool(claude_path)
    issues: list[str] = []
    try:
        registry = load_model_registry()
        profiles = load_orchestration_profiles()
        priors = load_benchmark_priors(registry=registry)
        validation = validate_registry_and_profiles(
            registry,
            profiles,
            priors,
            max_review_age_days=max_review_age_days,
        )
        quick_profiles = load_quick_profiles()
        validation["quickProfiles"] = {
            "schema": QUICK_PROFILES_SCHEMA,
            "default": quick_profiles.default_profile,
            "available": sorted(quick_profiles.profiles),
            "source": quick_profiles.source,
        }
        default_quick_profile = quick_profiles.profiles[quick_profiles.default_profile]
        registry_valid = True
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        validation: dict[str, Any] = {
            "valid": False,
            "errorType": type(exc).__name__,
            "error": str(exc) if verbose_paths else "local_asset_validation_failed",
        }
        registry_valid = False
        default_quick_profile = None
        issues.append("registry_or_profile_invalid")
    if not python_supported:
        issues.append("python_3_10_required")
    if not powershell_available:
        issues.append("powershell_7_recommended_for_wrappers")
    if not git_available:
        issues.append("git_missing_repository_context_will_fallback")
    if registry_valid and validation["registryReview"]["status"] != "current":
        issues.append("model_registry_review_required")
    codex_available = codex_cli_available(
        include_environment_locations=verbose_paths
    )
    if not codex_available and not claude_available:
        issues.append("no_supported_cli_detected")
    result = {
        "schema": DOCTOR_SCHEMA,
        "platform": platform.system().lower(),
        "python": {
            "version": platform.python_version(),
            "supported": python_supported,
        },
        "commands": {
            "git": git_available,
            "powershell": powershell_available,
            "codex": codex_available,
            "claude": claude_available,
        },
        "registry": _privacy_safe_validation(
            validation, verbose_paths=verbose_paths
        ),
        "readyForLocalRouting": python_supported and registry_valid,
        "readyForCliExecution": (
            python_supported and registry_valid and (codex_available or claude_available)
        ),
        "issues": issues,
        "privacy": {
            "pathsIncluded": verbose_paths,
            "credentialInspection": False,
            "environmentValuesPrinted": False,
            "commandDiscoveryUsesPath": True,
        },
        "defaults": {
            "quickProfile": (
                default_quick_profile.name if default_quick_profile else None
            ),
            "learningMode": (
                "configured"
                if default_quick_profile and default_quick_profile.enableLearningPolicy
                else "off"
            ),
            "configuredLearningMode": DEFAULT_CONFIG["mode"],
            "feedback": (
                "on"
                if default_quick_profile and default_quick_profile.enableFeedback
                else "off"
            ),
            "repositoryContext": (
                default_quick_profile.repositoryContextMode
                if default_quick_profile else "off"
            ),
            "modelAffinity": (
                default_quick_profile.modelAffinity
                if default_quick_profile else "off"
            ),
            "orchestrationPolicy": "recommend",
            "quickExecutionTopology": "direct",
        },
        "modelCalls": 0,
    }
    if verbose_paths:
        codex_command: list[str] | None = None
        if codex_available:
            try:
                codex_command = resolve_codex_command(
                    include_environment_locations=True
                )
            except RuntimeError:
                codex_command = None
        result["commandPaths"] = {
            "git": git_path,
            "powershell": powershell_path,
            "codexCommand": codex_command,
            "claude": claude_path,
        }
    return result


def format_summary(result: dict[str, Any]) -> str:
    if result["readyForCliExecution"]:
        overall = "READY"
    elif result["readyForLocalRouting"]:
        overall = "LOCAL ROUTING ONLY"
    else:
        overall = "BLOCKED"
    commands = result["commands"]
    command_summary = ", ".join(
        f"{name}={'yes' if available else 'no'}"
        for name, available in commands.items()
    )
    quick = result.get("registry", {}).get("quickProfiles", {})
    available_profiles = ", ".join(quick.get("available", ())) or "unavailable"
    issues = result.get("issues") or []
    lines = [
        f"Agent Auto Router doctor: {overall}",
        f"Local routing: {'ready' if result['readyForLocalRouting'] else 'blocked'}",
        f"CLI execution: {'ready' if result['readyForCliExecution'] else 'blocked'}",
        f"Quick profiles: {available_profiles} (default: {quick.get('default', 'n/a')})",
        (
            "Defaults: learning="
            f"{result.get('defaults', {}).get('learningMode', 'n/a')}, "
            "feedback="
            f"{result.get('defaults', {}).get('feedback', 'n/a')}, "
            "orchestration="
            f"{result.get('defaults', {}).get('orchestrationPolicy', 'n/a')}, "
            "quick=direct"
        ),
        f"Commands: {command_summary}",
        f"Issues: {', '.join(issues) if issues else 'none'}",
        "Privacy: no task or credential inspection; modelCalls=0",
        "Details: rerun with --json; add --verbose-paths only for local troubleshooting.",
    ]
    command_paths = result.get("commandPaths")
    if isinstance(command_paths, dict):
        lines.append("Command paths:")
        lines.extend(
            f"  {name}: {value}"
            for name, value in command_paths.items()
        )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--max-review-age-days", type=int, default=90)
    parser.add_argument(
        "--verbose-paths",
        action="store_true",
        help="Include absolute packaged-asset paths and env-derived CLI locations.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit the complete machine-readable diagnostic instead of a summary.",
    )
    parser.add_argument("--fail-on-issues", action="store_true")
    args = parser.parse_args()
    result = build_diagnostic(
        args.max_review_age_days, verbose_paths=args.verbose_paths
    )
    if args.json:
        print(json.dumps(result, ensure_ascii=True, indent=2))
    else:
        print(format_summary(result))
    return 1 if args.fail_on_issues and result["issues"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
