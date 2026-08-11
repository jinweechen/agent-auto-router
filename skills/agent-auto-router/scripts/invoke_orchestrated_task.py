#!/usr/bin/env python3
"""Execute one task through bounded registry-driven orchestration."""

from __future__ import annotations

import argparse
import copy
import datetime as dt
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from typing import Any

from auto_router import VARIANT_LABELS, route_case
from benchmark_priors import benchmark_priors_digest, load_benchmark_priors
from claude_cli_adapter import ClaudeCliAdapter
from cli_arguments import positive_int
from codex_cli_adapter import CodexCliAdapter
from execution_plan import ORCHESTRATION_POLICIES
from model_affinity import (
    DEFAULT_MODEL_AFFINITY_MODE,
    MODEL_AFFINITY_MODES,
    workspace_identity,
)
from host_permissions import (
    HostPermissions,
    cli_permission_issue,
    parse_host_permissions,
    workdir_is_writable,
)
from guarded_auto import (
    feedback_recording_enabled,
    learning_boundary_issue,
    process_recorded_outcome,
)
from model_registry import load_model_registry, registry_digest
from orchestration_engine import run_variant
from orchestration_profiles import load_orchestration_profiles
from policy_learning import append_route_event, default_feedback_path, load_maintained_feedback
from repository_context import (
    build_repository_context,
    disabled_repository_inspection,
    inspect_repository,
)
from route_contract import (
    enrich_route_decision,
    extract_execution_envelope,
    extract_route_decision,
)
from routing_policy import (
    DEFAULT_STATE_DIR,
    EFFORTS,
    STRATEGIES,
    load_policy_for_route,
    policy_digest,
)


def should_run_grader(routing: dict[str, Any], variant: str, policy: str) -> bool:
    if policy == "always":
        return True
    if policy == "never":
        return False
    return bool(routing["features"]["high_risk"]) or variant in {"B", "C"}


def estimate_model_calls(
    variant: str,
    include_grader: bool = True,
    worker_task_limit: int | None = None,
) -> tuple[int, int]:
    grader_calls = 1 if include_grader else 0
    if variant in {"A", "E", "F"}:
        calls = 1 + grader_calls
        return calls, calls
    if variant == "B":
        maximum_workers = worker_task_limit or 3
        return 3 + grader_calls, 2 + maximum_workers + grader_calls
    if variant == "D":
        maximum_workers = worker_task_limit or 2
        return 3 + grader_calls, 2 + maximum_workers + grader_calls
    if variant == "C":
        maximum_workers = worker_task_limit or 3
        return 4 + grader_calls, 3 + maximum_workers + grader_calls
    raise ValueError(f"Unknown variant: {variant}")


def bounded_worker_task_limit(
    variant: str,
    include_grader: bool,
    max_model_calls: int,
    requested_limit: int,
) -> int:
    if variant not in {"B", "C", "D"}:
        return requested_limit
    _, maximum_calls = estimate_model_calls(
        variant, include_grader, requested_limit
    )
    fixed_calls = maximum_calls - requested_limit
    return max(1, min(requested_limit, max_model_calls - fixed_calls))


REPORT_CONTENT_KEYS = frozenset({
    "task", "tasktext", "prompt", "request", "input", "inputtext", "output",
    "modeloutput", "finaloutput", "candidate", "rationale", "unmetcriteria",
    "criticalerrors", "tooloutput", "toolresult", "error", "stderr", "stdout",
    "message", "content", "response", "responseid", "threadid", "entries",
    "path", "workdir", "workspacepath", "reportpath", "registrysource",
    "profilesource",
})
GIT_STATUS_TIMEOUT_SECONDS = 5.0
WINDOWS_ACL_TIMEOUT_SECONDS = 10.0
IS_WINDOWS = os.name == "nt"


def _report_key(key: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(key).lower())


def _remove_report_content(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _remove_report_content(nested)
            for key, nested in value.items()
            if _report_key(key) not in REPORT_CONTENT_KEYS
        }
    if isinstance(value, list):
        return [_remove_report_content(item) for item in value]
    if isinstance(value, str) and (
        re.match(r"^[A-Za-z]:[\\/]", value)
        or value.startswith("/")
        or value.startswith("\\\\")
    ):
        return "[redacted-path]"
    return value


def build_report_payload(
    payload: dict[str, Any], *, include_model_output: bool = False
) -> dict[str, Any]:
    report = copy.deepcopy(payload)
    if not include_model_output:
        for workspace_key in ("workspace_before", "workspace_after"):
            workspace = report.get(workspace_key)
            if isinstance(workspace, dict):
                entries = workspace.get("entries")
                if isinstance(entries, list):
                    workspace["entryCount"] = len(entries)
        execution = report.get("execution")
        if isinstance(execution, dict):
            grade = execution.get("grade")
            if isinstance(grade, dict):
                execution["grade"] = {
                    "score": grade.get("score"),
                    "passed": grade.get("passed"),
                    "unmetCriteriaCount": len(grade.get("unmet_criteria") or ()),
                    "criticalErrorCount": len(grade.get("critical_errors") or ()),
                }
        report = _remove_report_content(report)
    report["report_privacy"] = {
        "includesModelOutput": include_model_output,
        "exclusiveCreate": True,
        "requestedPosixFileMode": "0600",
    }
    return report


def write_report(
    results_dir: pathlib.Path | None,
    run_id: str,
    payload: dict[str, Any],
    *,
    include_model_output: bool = False,
) -> str | None:
    if results_dir is None:
        return None
    directory_existed = results_dir.exists()
    results_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    if include_model_output and IS_WINDOWS:
        if not directory_existed:
            harden_windows_acl(results_dir, is_directory=True)
        if not windows_acl_is_private(results_dir):
            raise PermissionError(
                "content-bearing report directory does not have a verified private DACL"
            )
    report_path = results_dir / f"orchestration-{run_id}.json"
    payload["report_path"] = str(report_path.resolve())
    report = build_report_payload(
        payload,
        include_model_output=include_model_output,
    )
    open_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOINHERIT"):
        open_flags |= os.O_NOINHERIT
    descriptor = os.open(report_path, open_flags, 0o600)
    if include_model_output and IS_WINDOWS:
        os.close(descriptor)
        try:
            harden_windows_acl(report_path, is_directory=False)
            if not windows_acl_is_private(report_path):
                raise PermissionError(
                    "content-bearing report file does not have a verified private DACL"
                )
            with report_path.open("w", encoding="utf-8", newline="\n") as handle:
                json.dump(report, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
        except Exception:
            report_path.unlink(missing_ok=True)
            raise
    else:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(report, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
    return str(report_path.resolve())


def prepare_results_directory(
    results_dir: pathlib.Path | None, *, include_model_output: bool
) -> None:
    """Create and preflight a report directory before any model can run."""
    if results_dir is None:
        return
    directory_existed = results_dir.exists()
    results_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    if include_model_output and IS_WINDOWS:
        if not directory_existed:
            harden_windows_acl(results_dir, is_directory=True)
        if not windows_acl_is_private(results_dir):
            raise PermissionError(
                "content-bearing report directory does not have a verified private DACL"
            )


def _powershell_executable() -> str | None:
    return (
        shutil.which("pwsh")
        or shutil.which("pwsh.exe")
        or shutil.which("powershell.exe")
        or shutil.which("powershell")
    )


def _run_acl_script(script: str, path: pathlib.Path, is_directory: bool) -> str:
    powershell = _powershell_executable()
    if not powershell:
        raise PermissionError("PowerShell is required to verify a private Windows DACL")
    environment = {
        key: value
        for key in (
            "PATH", "PATHEXT", "PSModulePath", "SystemRoot", "WINDIR", "TEMP", "TMP"
        )
        if (value := os.environ.get(key)) is not None
    }
    environment["AGENT_AUTO_ROUTER_ACL_PATH"] = str(path.resolve())
    environment["AGENT_AUTO_ROUTER_ACL_IS_DIRECTORY"] = str(is_directory)
    completed = subprocess.run(
        [
            powershell, "-NoProfile", "-NonInteractive", "-Command", script,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        timeout=WINDOWS_ACL_TIMEOUT_SECONDS,
        env=environment,
    )
    if completed.returncode != 0:
        raise PermissionError("Windows DACL operation failed")
    return completed.stdout.strip()


def harden_windows_acl(path: pathlib.Path, *, is_directory: bool) -> None:
    """Replace inheritance with private current-user/System/Admin rules."""
    script = r"""
$ErrorActionPreference = 'Stop'
$target = [Environment]::GetEnvironmentVariable('AGENT_AUTO_ROUTER_ACL_PATH')
$isDirectory = [System.Boolean]::Parse(
    [Environment]::GetEnvironmentVariable('AGENT_AUTO_ROUTER_ACL_IS_DIRECTORY'))
$acl = if ($isDirectory) {
    [System.Security.AccessControl.DirectorySecurity]::new()
} else {
    [System.Security.AccessControl.FileSecurity]::new()
}
$acl.SetAccessRuleProtection($true, $false)
$current = [System.Security.Principal.WindowsIdentity]::GetCurrent().User
$system = [System.Security.Principal.SecurityIdentifier]::new('S-1-5-18')
$admins = [System.Security.Principal.SecurityIdentifier]::new('S-1-5-32-544')
foreach ($sid in @($current, $system, $admins)) {
    if ($isDirectory) {
        $rule = [System.Security.AccessControl.FileSystemAccessRule]::new(
            $sid, 'FullControl', 'ContainerInherit,ObjectInherit', 'None', 'Allow')
    } else {
        $rule = [System.Security.AccessControl.FileSystemAccessRule]::new(
            $sid, 'FullControl', 'Allow')
    }
    [void]$acl.AddAccessRule($rule)
}
if ($isDirectory) {
    [System.IO.FileSystemAclExtensions]::SetAccessControl(
        [System.IO.DirectoryInfo]::new($target), $acl)
} else {
    [System.IO.FileSystemAclExtensions]::SetAccessControl(
        [System.IO.FileInfo]::new($target), $acl)
}
"""
    _run_acl_script(script, path, is_directory)


def windows_acl_is_private(path: pathlib.Path) -> bool:
    """Verify a protected DACL grants access only to trusted local principals."""
    script = r"""
$ErrorActionPreference = 'Stop'
$target = [Environment]::GetEnvironmentVariable('AGENT_AUTO_ROUTER_ACL_PATH')
$acl = Get-Acl -LiteralPath $target
$current = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$allowed = @($current, 'S-1-5-18', 'S-1-5-32-544')
$unsafe = 0
$seen = @{}
foreach ($rule in @($acl.Access)) {
    if ($rule.AccessControlType -ne 'Allow') { continue }
    try { $sid = $rule.IdentityReference.Translate([System.Security.Principal.SecurityIdentifier]).Value }
    catch { $unsafe += 1; continue }
    if ($allowed -notcontains $sid) { $unsafe += 1 }
    else { $seen[$sid] = $true }
}
if ($acl.AreAccessRulesProtected -and $unsafe -eq 0 -and
    $seen.ContainsKey($current) -and $seen.ContainsKey('S-1-5-18') -and
    $seen.ContainsKey('S-1-5-32-544')) { 'private' } else { 'unsafe' }
"""
    try:
        return _run_acl_script(script, path, path.is_dir()).splitlines()[-1] == "private"
    except (OSError, PermissionError, subprocess.SubprocessError, IndexError):
        return False


def _workdir_has_git_metadata(workdir: pathlib.Path) -> bool | None:
    resolved = workdir.resolve()
    for candidate in (resolved, *resolved.parents):
        try:
            (candidate / ".git").stat()
        except FileNotFoundError:
            continue
        except OSError:
            return None
        else:
            return True
    return False


def workspace_status(
    workdir: pathlib.Path, *, timeout_seconds: float = GIT_STATUS_TIMEOUT_SECONDS
) -> dict[str, Any]:
    resolved = workdir.resolve()
    try:
        completed = subprocess.run(
            [
                "git", "-c", f"safe.directory={resolved}", "-C", str(resolved),
                "status", "--porcelain=v1",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired:
        return {
            "status": "unknown", "is_git_repo": None, "dirty": None,
            "entries": [], "error": "git_status_timeout",
        }
    except OSError:
        return {
            "status": "unknown", "is_git_repo": None, "dirty": None,
            "entries": [], "error": "git_status_unavailable",
        }
    if completed.returncode != 0:
        metadata_state = _workdir_has_git_metadata(resolved)
        if metadata_state is False:
            return {
                "status": "non_git", "is_git_repo": False, "dirty": None,
                "entries": [], "error": None,
            }
        return {
            "status": "unknown", "is_git_repo": None, "dirty": None,
            "entries": [], "error": "git_status_failed",
        }
    entries = [line for line in completed.stdout.splitlines() if line]
    return {
        "status": "dirty" if entries else "clean",
        "is_git_repo": True,
        "dirty": bool(entries),
        "entries": entries,
        "error": None,
    }


def workspace_was_modified(before: dict[str, Any], after: dict[str, Any]) -> bool | None:
    if before.get("is_git_repo") and after.get("is_git_repo"):
        if before.get("dirty"):
            return None
        return before.get("entries", []) != after.get("entries", [])
    return None


def results_dir_is_inside_workdir(
    results_dir: pathlib.Path, workdir: pathlib.Path
) -> bool:
    resolved_results_dir = results_dir.resolve()
    resolved_workdir = workdir.resolve()
    return (
        resolved_results_dir == resolved_workdir
        or resolved_workdir in resolved_results_dir.parents
    )


def path_is_inside_any_root(
    path: pathlib.Path, roots: tuple[pathlib.Path, ...]
) -> bool:
    resolved_path = path.resolve(strict=False)
    for root in roots:
        resolved_root = root.resolve(strict=False)
        if resolved_path == resolved_root or resolved_root in resolved_path.parents:
            return True
    return False


def child_writable_roots(
    sandbox: str,
    workdir: pathlib.Path,
    host_permissions: HostPermissions | None,
) -> tuple[pathlib.Path, ...] | None:
    """Return child-writable roots, or None when the entire filesystem is writable."""
    if sandbox == "danger-full-access":
        return None
    if sandbox != "workspace-write":
        return ()
    if host_permissions is not None and host_permissions.writable_roots:
        return tuple(pathlib.Path(value) for value in host_permissions.writable_roots)
    return (workdir.resolve(),)


def feedback_execution_identity(
    routing: dict[str, Any],
    execution_result: dict[str, Any] | None = None,
) -> tuple[str, str]:
    selected_model = str(routing["selected_model"])
    selected_effort = str(routing["effort"])
    if not execution_result:
        return selected_model, selected_effort
    variant = str(execution_result.get("variant", routing.get("selected_variant", "")))
    final_role = "direct" if variant in {"A", "E", "F"} else "reviewer"
    resolved_role = execution_result.get("resolved_roles", {}).get(final_role, {})
    if isinstance(resolved_role, dict) and resolved_role.get("model"):
        selected_model = str(resolved_role["model"])
        selected_effort = str(resolved_role.get("effort") or selected_effort)
    for call in reversed(execution_result.get("calls", [])):
        if isinstance(call, dict) and call.get("role") == final_role:
            selected_model = str(call.get("model") or selected_model)
            selected_effort = str(call.get("effort") or selected_effort)
            break
    return selected_model, selected_effort


def routing_from_route_decision(route: dict[str, Any]) -> dict[str, Any]:
    """Restore the legacy runtime view without making another routing decision."""
    execution_plan = copy.deepcopy(route["executionPlan"])
    variant = str(execution_plan["variant"])
    return {
        "schema": route["schema"],
        "routeDecision": copy.deepcopy(route),
        "router_version": str(route["policy"]["version"]),
        "policy_digest": str(route["policy"]["digest"]),
        "registry_digest": str(route["registry"]["digest"]),
        "feature_schema_version": int(route["featureSchemaVersion"]),
        "mode": str(route["strategy"]),
        "effort": str(route["effort"]),
        "variant": variant,
        "recommended_variant": variant,
        "route": VARIANT_LABELS[variant],
        "selected_model": str(route["selectedModel"]),
        "selector_model": str(route["selectorModel"]),
        "target_tier": str(route["targetTier"]),
        "required_capabilities": list(route["requiredCapabilities"]),
        "execution_plan": execution_plan,
        "repository_features": copy.deepcopy(route["repository"]["metadata"]),
        "model_reason": str(route["reasonCode"]),
        "workspace_key": route["workspaceKey"],
        "model_affinity": copy.deepcopy(route["modelAffinity"]),
        "matched_signals": copy.deepcopy(route["matchedSignals"]),
        "features": copy.deepcopy(route["features"]),
        "reasons": ["trusted-route-decision-reused"],
    }


def record_route_feedback(
    args: argparse.Namespace,
    routing: dict[str, Any],
    route_id: str,
    started_at: float,
    exit_code: int,
    observed_tokens: dict[str, int] | None = None,
    observed_tokens_by_model: dict[str, dict[str, int]] | None = None,
    execution_result: dict[str, Any] | None = None,
) -> None:
    if args.no_feedback or not feedback_recording_enabled(args.state_dir):
        return
    feedback_path = args.feedback_file or default_feedback_path(args.state_dir)
    features = dict(routing["features"])
    repository_features = routing.get("repository_features")
    if isinstance(repository_features, dict):
        features.update(repository_features)
    selected_model, selected_effort = feedback_execution_identity(
        routing, execution_result
    )
    selected_observed_tokens = (
        observed_tokens_by_model.get(selected_model)
        if observed_tokens_by_model is not None else None
    )

    def token_payload(value: dict[str, int] | None) -> dict[str, int] | None:
        if value is None:
            return None
        return {
            "input": int(value.get("input_tokens", 0)),
            "cached_input": int(value.get("cached_input_tokens", 0)),
            "cache_write": int(value.get("cache_write_input_tokens", 0)),
            "output": int(value.get("output_tokens", 0)),
            "reasoning_output": int(value.get("reasoning_output_tokens", 0)),
            "total": int(value.get("total_tokens", 0)),
        }
    payload = {
        "route_id": route_id,
        "strategy": args.strategy,
        "effort": selected_effort,
        "selector_model": routing.get("selector_model") or routing["selected_model"],
        "selected_model": selected_model,
        "target_tier": routing["target_tier"],
        "reason": routing["model_reason"],
        "features": {
            key: features[key]
            for key in (
                "prompt_chars", "criteria_count", "complexity_score", "risk_score",
                "clarity_score", "high_risk", "constrained", "parallelizable",
                "dependency_ambiguity", "orchestration_eligible", "scope_hits",
                "algorithm_hits", "repo_files", "source_files", "test_files",
                "language_count", "manifest_count", "large_repo", "monorepo",
                "dirty_worktree", "is_git_repo", "task_has_path_hint",
            )
            if key in features
        },
        "policy_version": routing["router_version"],
        "policy_digest": routing["policy_digest"],
        "registry_digest": routing["registry_digest"],
        "feature_schema_version": routing["feature_schema_version"],
        "explicit_override": args.variant != "auto",
        "backend": args.backend,
        "workspace_key": routing.get("workspace_key"),
        "topology": routing["execution_plan"]["topology"],
        "variant": routing.get("selected_variant") or routing["variant"],
        "role_model_policy": routing["execution_plan"]["roleModelPolicy"],
        "estimated_role_tier_switches": (
            execution_result.get("planned_model_switches", 0)
            if execution_result else routing["execution_plan"]
            ["orchestrationRecommendation"]["utility"]["estimatedRoleTierSwitches"]
        ),
        "exit_code": exit_code,
        "duration_ms": int((time.monotonic() - started_at) * 1000),
        "observed_tokens": token_payload(observed_tokens),
        "selected_model_observed_tokens": token_payload(selected_observed_tokens),
    }
    try:
        append_route_event(payload, feedback_path)
        learning = process_recorded_outcome(args.state_dir, feedback_path)
        if learning.get("status") == "error":
            print(
                "warning: route feedback was recorded, but guarded automatic "
                f"learning did not advance: {learning.get('errorType')}",
                file=sys.stderr,
            )
    except Exception as exc:
        print(
            f"warning: route feedback was not recorded for {route_id}: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    task_input = parser.add_mutually_exclusive_group(required=True)
    task_input.add_argument("--text")
    task_input.add_argument("--stdin", action="store_true")
    task_input.add_argument("--execution-envelope-stdin", action="store_true")
    parser.add_argument("--acceptance-criterion", action="append", default=[])
    parser.add_argument("--strategy", choices=STRATEGIES, default="balance")
    parser.add_argument("--variant", choices=("auto", *VARIANT_LABELS), default="auto")
    parser.add_argument(
        "--orchestration-policy",
        choices=ORCHESTRATION_POLICIES,
        default="auto",
    )
    parser.add_argument("--confirm-high-risk-orchestration", action="store_true")
    parser.add_argument(
        "--model-affinity",
        choices=MODEL_AFFINITY_MODES,
        default=DEFAULT_MODEL_AFFINITY_MODE,
    )
    parser.add_argument("--effort", choices=EFFORTS, default=None)
    parser.add_argument("--max-workers", type=positive_int, default=2)
    parser.add_argument("--timeout", type=positive_int, default=600)
    parser.add_argument("--total-timeout", type=positive_int, default=1800)
    parser.add_argument("--max-model-calls", type=positive_int, default=7)
    parser.add_argument("--max-total-tokens", type=positive_int, default=None)
    parser.add_argument("--workdir", type=pathlib.Path, default=pathlib.Path.cwd())
    parser.add_argument("--results-dir", type=pathlib.Path, default=None)
    parser.add_argument("--include-output-in-report", action="store_true")
    parser.add_argument("--state-dir", type=pathlib.Path, default=DEFAULT_STATE_DIR)
    parser.add_argument("--feedback-file", type=pathlib.Path, default=None)
    parser.add_argument("--no-feedback", action="store_true")
    parser.add_argument(
        "--sandbox",
        choices=("inherit", "read-only", "workspace-write", "danger-full-access"),
        default="inherit",
    )
    parser.add_argument("--host-permissions-json")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--allow-dirty", action="store_true")
    parser.add_argument("--allow-no-changes", action="store_true")
    parser.add_argument("--explain", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--no-progress", action="store_true")
    parser.add_argument("--context-mode", choices=("lean", "full"), default="lean")
    parser.add_argument(
        "--repository-context", choices=("auto", "off"), default="auto"
    )
    parser.add_argument("--backend", default=None, help="Execute the whole orchestration on one backend (e.g. codex, claude). Default: all declared backends, codex-first.")
    parser.add_argument(
        "--grader-policy", choices=("auto", "always", "never"), default="auto"
    )
    for role in ("planner", "dispatcher", "worker", "reviewer", "grader"):
        parser.add_argument(f"--{role}-effort", choices=EFFORTS, default=None)
    return parser.parse_args()


def build_adapter(backend: str | None, args: argparse.Namespace, role_efforts: dict[str, str], progress: Any) -> Any:
    if backend in (None, "codex"):
        return CodexCliAdapter(
            timeout_seconds=args.timeout,
            effort_override=args.effort,
            role_efforts=role_efforts,
            workdir=args.workdir,
            execution_mode=True,
            write_sandbox=args.sandbox,
            total_timeout_seconds=args.total_timeout,
            max_model_calls=args.max_model_calls,
            max_total_tokens=args.max_total_tokens,
            progress_callback=progress,
            context_mode=args.context_mode,
            host_permissions=getattr(args, "host_permissions", None),
        )
    if backend == "claude":
        return ClaudeCliAdapter(
            timeout_seconds=args.timeout,
            effort_override=args.effort,
            role_efforts=role_efforts,
            workdir=args.workdir,
            execution_mode=True,
            write_sandbox=args.sandbox,
            allowed_tools=("Read", "Edit", "Write", "Bash") if args.sandbox != "read-only" else ("Read",),
            max_turns=30,
            total_timeout_seconds=args.total_timeout,
            max_model_calls=args.max_model_calls,
            max_total_tokens=args.max_total_tokens,
            progress_callback=progress,
            host_permissions=getattr(args, "host_permissions", None),
        )
    raise ValueError(f"unknown backend: {backend}")


def main() -> int:
    args = parse_args()
    locked_route_input: dict[str, Any] | None = None
    envelope_prompt: str | None = None
    if args.execution_envelope_stdin:
        envelope = json.load(sys.stdin)
        if not isinstance(envelope, dict):
            raise ValueError("execution envelope must be a JSON object")
        envelope_prompt, locked_route_input, envelope_permissions = (
            extract_execution_envelope(envelope)
        )
        args.host_permissions_json = json.dumps(
            envelope_permissions, ensure_ascii=True, separators=(",", ":")
        )
    if locked_route_input is not None:
        locked_affinity = locked_route_input.get("modelAffinity")
        if isinstance(locked_affinity, dict):
            locked_mode = locked_affinity.get("mode")
            if locked_mode not in MODEL_AFFINITY_MODES:
                raise ValueError("locked route has an invalid model-affinity mode")
            args.model_affinity = str(locked_mode)
    if args.include_output_in_report and args.results_dir is None:
        print(
            json.dumps(
                {
                    "status": "blocked",
                    "reason": "report_output_requires_results_dir",
                },
                ensure_ascii=False,
                indent=2,
            ),
            file=sys.stderr,
        )
        return 2
    host_permissions = None
    if args.host_permissions_json:
        host_permissions = parse_host_permissions(args.host_permissions_json)
        args.host_permissions = host_permissions
        args.sandbox = host_permissions.effective_sandbox(args.sandbox)
        if args.sandbox == "external-sandbox":
            raise ValueError("CLI orchestration cannot directly represent external-sandbox inheritance")
        issue = cli_permission_issue(host_permissions, args.sandbox)
        if issue:
            raise ValueError(issue)
        if args.sandbox != "read-only" and not workdir_is_writable(args.workdir, host_permissions):
            raise ValueError("Workdir is outside the writable roots declared by the host")
        if not args.dry_run:
            boundary_issue = learning_boundary_issue(
                args.state_dir,
                args.feedback_file or default_feedback_path(args.state_dir),
                host_permissions,
                args.sandbox,
                args.model_affinity,
            )
            if boundary_issue:
                raise ValueError(boundary_issue)
    elif args.sandbox == "inherit":
        if args.dry_run:
            args.sandbox = "read-only"
        else:
            raise ValueError("--sandbox inherit requires --host-permissions-json")
    else:
        args.host_permissions = None
        if not args.dry_run:
            explicit_permissions = HostPermissions(
                source="router-explicit-sandbox",
                sandbox=args.sandbox,
                approval_policy="on-request",
                network_access=None,
                writable_roots=(str(args.workdir.resolve()),)
                if args.sandbox == "workspace-write"
                else (),
                can_request_permissions=True,
            )
            boundary_issue = learning_boundary_issue(
                args.state_dir,
                args.feedback_file or default_feedback_path(args.state_dir),
                explicit_permissions,
                args.sandbox,
                args.model_affinity,
            )
            if boundary_issue:
                raise ValueError(boundary_issue)
    started_at = time.monotonic()
    if args.results_dir is not None:
        resolved_results_dir = args.results_dir.resolve(strict=False)
        resolved_workdir = args.workdir.resolve(strict=False)
        writable_roots = child_writable_roots(
            args.sandbox, args.workdir, host_permissions
        )
        writable_by_child = (
            writable_roots is None
            or path_is_inside_any_root(
                args.results_dir,
                writable_roots,
            )
        )
        inside_read_only_workdir = (
            args.sandbox == "read-only"
            and results_dir_is_inside_workdir(args.results_dir, args.workdir)
        )
        if writable_by_child or inside_read_only_workdir:
            reason = (
                "results_dir_writable_by_child"
                if writable_by_child
                else "results_dir_inside_workdir"
            )
            print(
                json.dumps(
                    {
                        "status": "blocked",
                        "reason": reason,
                        "results_dir": str(resolved_results_dir),
                        "workdir": str(resolved_workdir),
                        "hint": (
                            "Choose a results directory outside every child-writable root."
                        ),
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                file=sys.stderr,
            )
            return 2
        try:
            prepare_results_directory(
                args.results_dir,
                include_model_output=args.include_output_in_report,
            )
        except (OSError, PermissionError, subprocess.SubprocessError):
            print(
                json.dumps(
                    {
                        "status": "blocked",
                        "reason": "report_privacy_boundary_unverified",
                        "results_dir": str(resolved_results_dir),
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                file=sys.stderr,
            )
            return 2
    run_id = (
        dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ-")
        + uuid.uuid4().hex
    )
    prompt = (
        envelope_prompt
        if envelope_prompt is not None
        else sys.stdin.read()
        if args.stdin
        else args.text
    )
    if not prompt or not prompt.strip():
        raise ValueError("Task must not be empty")
    if not args.workdir.is_dir():
        raise ValueError(f"Workdir not found: {args.workdir}")
    before_status = workspace_status(args.workdir)
    if (
        not args.dry_run
        and args.sandbox in {"workspace-write", "danger-full-access"}
        and before_status["status"] == "unknown"
    ):
        print(
            json.dumps(
                {
                    "status": "blocked",
                    "reason": "workspace_status_unknown",
                    "workspace_before": before_status,
                    "hint": "Restore bounded Git status checks or use read-only execution.",
                    "modelCalls": 0,
                },
                ensure_ascii=False,
                indent=2,
            ),
            file=sys.stderr,
        )
        return 2

    criteria = args.acceptance_criterion or [
        "Complete the requested task without unrelated changes",
        "Report changed files and validation performed",
    ]
    case: dict[str, Any] = {
        "id": run_id,
        "prompt": prompt.strip(),
        "acceptance_criteria": criteria,
        "workspace_key": workspace_identity(args.workdir),
    }
    repository_inspection = (
        inspect_repository(args.workdir, prompt)
        if args.repository_context == "auto"
        else disabled_repository_inspection()
    )
    repository_features = dict(repository_inspection)
    repository_features.pop("files", None)
    case["repository_features"] = repository_features
    routing_effort = args.effort or args.planner_effort or "medium"
    registry = load_model_registry()
    benchmark_priors = load_benchmark_priors(registry=registry)
    policy_route_id = (
        str(locked_route_input.get("routeId"))
        if locked_route_input is not None else run_id
    )
    active_policy, policy_source = load_policy_for_route(
        args.state_dir,
        policy_route_id,
        registry_digest_value=registry_digest(registry),
        benchmark_priors_digest_value=benchmark_priors_digest(benchmark_priors),
    )
    if args.backend and args.backend not in registry.backends:
        print(
            json.dumps(
                {
                    "status": "blocked",
                    "reason": "unknown_backend",
                    "backend": args.backend,
                    "known_backends": sorted(registry.backends.keys()),
                },
                ensure_ascii=False,
                indent=2,
            ),
            file=sys.stderr,
        )
        return 2
    requested_backends: tuple[str, ...] | None = (args.backend,) if args.backend else None
    allow_explicit = False
    profiles = load_orchestration_profiles()
    if locked_route_input is not None:
        locked_route = extract_route_decision(
            locked_route_input, registry=registry, task_text=prompt
        )
        if locked_route["workspaceKey"] != workspace_identity(args.workdir):
            raise ValueError("locked route workspaceKey does not match the execution workdir")
        if locked_route["policy"]["digest"] != policy_digest(active_policy):
            raise ValueError("locked route policy digest does not match the active policy")
        locked_variant = str(locked_route["executionPlan"]["variant"])
        if args.variant != "auto" and args.variant != locked_variant:
            raise ValueError("locked route variant does not match --variant")
        selected_backend = registry.get(
            str(locked_route["selectedModel"]), role="direct"
        ).backend
        if args.backend is not None and args.backend != selected_backend:
            raise ValueError("locked route backend does not match --backend")
        args.backend = selected_backend
        requested_backends = (selected_backend,)
        args.strategy = str(locked_route["strategy"])
        args.effort = str(locked_route["effort"])
        args.model_affinity = str(locked_route["modelAffinity"].get("mode") or "off")
        args.variant = locked_variant
        allow_explicit = bool(locked_route["explicitOverride"])
        affinity_error = None
        routing = routing_from_route_decision(locked_route)
    else:
        # Selecting a backend does not authorize models marked explicit-only.
        # This path has no explicit model parameter, so Auto honors autoEligible.
        if args.model_affinity == "off":
            affinity_events = []
            affinity_error = None
        else:
            try:
                affinity_events, _ = load_maintained_feedback(
                    args.feedback_file or default_feedback_path(args.state_dir),
                    apply=False,
                )
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                affinity_events = []
                affinity_error = type(exc).__name__
            else:
                affinity_error = None
        try:
            routing = route_case(
                case, args.strategy, routing_effort, active_policy, registry,
                backends=requested_backends, allow_explicit_only=allow_explicit,
                orchestration_policy=args.orchestration_policy,
                confirm_high_risk_orchestration=args.confirm_high_risk_orchestration,
                affinity_events=affinity_events,
                model_affinity_mode=args.model_affinity,
                explicit_variant=None if args.variant == "auto" else args.variant,
            )
        except ValueError as exc:
            print(
                json.dumps(
                    {
                        "status": "blocked",
                        "reason": "model_resolution_failed",
                        "backend": args.backend,
                        "error": str(exc),
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                file=sys.stderr,
            )
            return 2
    context_budget = routing["execution_plan"]["context"]
    if args.repository_context == "auto":
        repository_context, repository_context_metadata = build_repository_context(
            args.workdir,
            prompt,
            max_candidate_files=int(context_budget["maxCandidateFiles"]),
            repo_map_tokens=int(context_budget["repoMapTokens"]),
            repository_inspection=repository_inspection,
        )
    else:
        repository_context = ""
        repository_context_metadata = dict(repository_features)
        repository_context_metadata.update({
            "candidate_files": 0,
            "context_chars": 0,
            "context_useful": False,
        })
    if repository_context_metadata["context_useful"]:
        case["repository_context"] = repository_context
    if affinity_error is not None:
        routing["model_affinity"]["reason"] = "feedback-evidence-unavailable"
        routing["model_affinity"]["errorType"] = affinity_error
    routing["route_id"] = run_id
    routing["repository_context"] = {
        "mode": args.repository_context,
        "metadata": repository_context_metadata,
    }
    variant = routing["variant"]
    routing["selected_variant"] = variant
    routing["selected_route"] = VARIANT_LABELS[variant]
    grader_enabled = should_run_grader(routing, variant, args.grader_policy)
    requested_worker_task_limit = 2 if variant == "D" else 3
    worker_task_limit = bounded_worker_task_limit(
        variant,
        grader_enabled,
        args.max_model_calls,
        requested_worker_task_limit,
    )
    minimum_calls, maximum_calls = estimate_model_calls(
        variant, grader_enabled, worker_task_limit
    )
    routing["grader_enabled"] = grader_enabled
    routing["grader_policy"] = args.grader_policy
    routing["worker_task_limit"] = worker_task_limit
    routing["estimated_model_calls"] = {"minimum": minimum_calls, "maximum": maximum_calls}
    routing["selected_backend"] = args.backend or "all"
    canonical_route = copy.deepcopy(routing["routeDecision"])
    canonical_route["modelAffinity"] = copy.deepcopy(routing["model_affinity"])
    canonical_route["executionPlan"] = copy.deepcopy(routing["execution_plan"])
    routing["routeDecision"] = enrich_route_decision(
        canonical_route,
        repository_mode=args.repository_context,
        repository_metadata=repository_context_metadata,
        policy_source=policy_source,
        registry_source=registry.source,
    )
    routing["policy_source"] = routing["routeDecision"]["policy"]["source"]
    routing["registry_source"] = routing["routeDecision"]["registry"]["source"]
    routing["profile_source"] = (
        "file:" + str(profiles.source).replace("\\", "/").rsplit("/", 1)[-1]
    )

    if args.explain:
        print(json.dumps({"routing": routing}, ensure_ascii=False, indent=2), file=sys.stderr)
    if args.dry_run:
        payload = {"run_id": run_id, "routing": routing, "modelCalls": 0}
        write_report(args.results_dir, run_id, payload)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    if before_status["is_git_repo"] and before_status["dirty"] and not args.allow_dirty:
        print(
            json.dumps(
                {
                    "status": "blocked",
                    "reason": "workspace_not_clean",
                    "workspace_before": before_status,
                    "hint": "Commit/stash existing changes or pass --allow-dirty explicitly.",
                },
                ensure_ascii=False,
                indent=2,
            ),
            file=sys.stderr,
        )
        return 2
    if minimum_calls > args.max_model_calls:
        print(
            json.dumps(
                {
                    "status": "blocked",
                    "reason": "model_call_budget_too_small",
                    "variant": variant,
                    "minimum_calls": minimum_calls,
                    "max_model_calls": args.max_model_calls,
                },
                ensure_ascii=False,
                indent=2,
            ),
            file=sys.stderr,
        )
        return 2

    role_efforts = {
        role: value
        for role in ("planner", "dispatcher", "worker", "reviewer", "grader")
        if (value := getattr(args, f"{role}_effort"))
    }
    progress_lock = threading.Lock()

    def progress(event: dict[str, Any]) -> None:
        if args.no_progress:
            return
        event["timestamp"] = dt.datetime.now(dt.timezone.utc).isoformat()
        with progress_lock:
            print(json.dumps(event, ensure_ascii=False), file=sys.stderr, flush=True)

    client = build_adapter(args.backend, args, role_efforts, progress)
    try:
        result = run_variant(
            client,
            case,
            variant,
            args.max_workers,
            execution_mode=True,
            grade_enabled=grader_enabled,
            worker_task_limit=worker_task_limit,
            registry=registry,
            profiles=profiles,
            required_capabilities=tuple(routing["required_capabilities"]),
            backends=requested_backends,
            allow_explicit_only=allow_explicit,
            selected_model=routing["selected_model"],
            role_model_policy=routing["execution_plan"]["roleModelPolicy"],
        )
    except Exception as exc:
        after_status = workspace_status(args.workdir)
        failure = {
            "run_id": run_id,
            "status": "failed",
            "failed_stage": "orchestration",
            "error": f"{type(exc).__name__}: {exc}",
            "workspace_before": before_status,
            "workspace_after": after_status,
            "workspace_may_be_modified": workspace_was_modified(before_status, after_status),
            "observed_token_usage": client.observed_usage(),
        }
        write_report(
            args.results_dir,
            run_id,
            failure,
            include_model_output=args.include_output_in_report,
        )
        print(json.dumps(failure, ensure_ascii=False, indent=2), file=sys.stderr)
        record_route_feedback(
            args,
            routing,
            run_id,
            started_at,
            1,
            client.observed_usage(),
            client.observed_usage_by_model(),
        )
        return 1
    after_status = workspace_status(args.workdir)
    workspace_modified = workspace_was_modified(before_status, after_status)
    payload = {
        "run_id": run_id,
        "mode": "orchestrated-execution",
        "sandbox": args.sandbox,
        "routing": routing,
        "execution": result,
        "workspace_before": before_status,
        "workspace_after": after_status,
        "workspace_modified": workspace_modified,
        "observed_token_usage": client.observed_usage(),
    }
    no_change_failure = (
        args.sandbox in {"workspace-write", "danger-full-access"}
        and before_status.get("is_git_repo")
        and after_status.get("is_git_repo")
        and workspace_modified is False
        and not args.allow_no_changes
    )
    if no_change_failure:
        payload["status"] = "failed"
        payload["failure_reason"] = "workspace_write_completed_without_changes"
        result["implementation_status"] = "failed_no_workspace_changes"
    write_report(
        args.results_dir,
        run_id,
        payload,
        include_model_output=args.include_output_in_report,
    )
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(result["final_output"])
        print(
            f"route={routing['selected_route']} calls={len(result['calls'])} "
            f"grade_passed={result['grade'].get('passed')} report={payload.get('report_path')}",
            file=sys.stderr,
        )
    exit_code = 1 if no_change_failure else 0
    record_route_feedback(
        args,
        routing,
        run_id,
        started_at,
        exit_code,
        client.observed_usage(),
        client.observed_usage_by_model(),
        result,
    )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
