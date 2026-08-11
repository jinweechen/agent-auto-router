#!/usr/bin/env python3
"""Execute one task through bounded registry-driven orchestration."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import subprocess
import sys
import threading
import time
from typing import Any

from auto_router import VARIANT_LABELS, route_case
from benchmark_priors import benchmark_priors_digest, load_benchmark_priors
from claude_cli_adapter import ClaudeCliAdapter
from cli_arguments import positive_int
from codex_cli_adapter import CodexCliAdapter
from host_permissions import (
    HostPermissions,
    cli_permission_issue,
    parse_host_permissions,
    workdir_is_writable,
)
from guarded_auto import learning_boundary_issue, process_recorded_outcome
from model_registry import load_model_registry, registry_digest
from orchestration_engine import run_variant
from orchestration_profiles import load_orchestration_profiles
from policy_learning import append_route_event, default_feedback_path
from repository_context import build_repository_context, inspect_repository
from routing_policy import DEFAULT_STATE_DIR, EFFORTS, STRATEGIES, load_policy_for_route


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


def write_report(results_dir: pathlib.Path | None, run_id: str, payload: dict[str, Any]) -> str | None:
    if results_dir is None:
        return None
    results_dir.mkdir(parents=True, exist_ok=True)
    report_path = results_dir / f"orchestration-{run_id}.json"
    payload["report_path"] = str(report_path.resolve())
    report_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return str(report_path.resolve())


def workspace_status(workdir: pathlib.Path) -> dict[str, Any]:
    resolved = workdir.resolve()
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
    )
    if completed.returncode != 0:
        return {
            "is_git_repo": False,
            "dirty": None,
            "entries": [],
            "error": completed.stderr.strip()[-1000:],
        }
    entries = [line for line in completed.stdout.splitlines() if line]
    return {"is_git_repo": True, "dirty": bool(entries), "entries": entries}


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


def record_route_feedback(
    args: argparse.Namespace,
    routing: dict[str, Any],
    route_id: str,
    started_at: float,
    exit_code: int,
    observed_tokens: dict[str, int] | None = None,
    execution_result: dict[str, Any] | None = None,
) -> None:
    if args.no_feedback:
        return
    feedback_path = args.feedback_file or default_feedback_path(args.state_dir)
    features = dict(routing["features"])
    repository_features = routing.get("repository_features")
    if isinstance(repository_features, dict):
        features.update(repository_features)
    selected_model, selected_effort = feedback_execution_identity(
        routing, execution_result
    )
    payload = {
        "route_id": route_id,
        "strategy": args.strategy,
        "effort": selected_effort,
        "selector_model": routing["selected_model"],
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
        "exit_code": exit_code,
        "duration_ms": int((time.monotonic() - started_at) * 1000),
        "observed_tokens": {
            "input": int(observed_tokens.get("input_tokens", 0)),
            "cached_input": int(observed_tokens.get("cached_input_tokens", 0)),
            "output": int(observed_tokens.get("output_tokens", 0)),
            "reasoning_output": int(observed_tokens.get("reasoning_output_tokens", 0)),
            "total": int(observed_tokens.get("total_tokens", 0)),
        } if observed_tokens is not None else None,
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
    parser.add_argument("--acceptance-criterion", action="append", default=[])
    parser.add_argument("--strategy", choices=STRATEGIES, default="balance")
    parser.add_argument("--variant", choices=("auto", *VARIANT_LABELS), default="auto")
    parser.add_argument("--effort", choices=EFFORTS, default=None)
    parser.add_argument("--max-workers", type=positive_int, default=2)
    parser.add_argument("--timeout", type=positive_int, default=600)
    parser.add_argument("--total-timeout", type=positive_int, default=1800)
    parser.add_argument("--max-model-calls", type=positive_int, default=7)
    parser.add_argument("--max-total-tokens", type=positive_int, default=None)
    parser.add_argument("--workdir", type=pathlib.Path, default=pathlib.Path.cwd())
    parser.add_argument("--results-dir", type=pathlib.Path, default=None)
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
            )
            if boundary_issue:
                raise ValueError(boundary_issue)
    started_at = time.monotonic()
    if (
        args.results_dir is not None
        and results_dir_is_inside_workdir(args.results_dir, args.workdir)
    ):
            resolved_results_dir = args.results_dir.resolve()
            resolved_workdir = args.workdir.resolve()
            print(
                json.dumps(
                    {
                        "status": "blocked",
                        "reason": "results_dir_inside_workdir",
                        "results_dir": str(resolved_results_dir),
                        "workdir": str(resolved_workdir),
                        "hint": "Choose a results directory outside the target workspace.",
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                file=sys.stderr,
            )
            return 2
    run_id = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    prompt = sys.stdin.read() if args.stdin else args.text
    if not prompt or not prompt.strip():
        raise ValueError("Task must not be empty")
    if not args.workdir.is_dir():
        raise ValueError(f"Workdir not found: {args.workdir}")
    before_status = workspace_status(args.workdir)

    criteria = args.acceptance_criterion or [
        "Complete the requested task without unrelated changes",
        "Report changed files and validation performed",
    ]
    case: dict[str, Any] = {
        "id": "orchestrated-task",
        "prompt": prompt.strip(),
        "acceptance_criteria": criteria,
    }
    repository_features = inspect_repository(args.workdir, prompt)
    repository_features.pop("files", None)
    case["repository_features"] = repository_features
    routing_effort = args.effort or args.planner_effort or "medium"
    registry = load_model_registry()
    benchmark_priors = load_benchmark_priors(registry=registry)
    active_policy, policy_source = load_policy_for_route(
        args.state_dir,
        run_id,
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
    allow_explicit = bool(args.backend)
    profiles = load_orchestration_profiles()
    routing = route_case(
        case, args.strategy, routing_effort, active_policy, registry,
        backends=requested_backends, allow_explicit_only=allow_explicit,
    )
    context_budget = routing["execution_plan"]["context"]
    repository_context, repository_context_metadata = build_repository_context(
        args.workdir,
        prompt,
        max_candidate_files=int(context_budget["maxCandidateFiles"]),
        repo_map_tokens=int(context_budget["repoMapTokens"]),
    )
    if repository_context_metadata["context_useful"]:
        case["repository_context"] = repository_context
    routing["policy_source"] = policy_source
    routing["registry_source"] = registry.source
    routing["profile_source"] = profiles.source
    routing["route_id"] = run_id
    variant = routing["variant"] if args.variant == "auto" else args.variant
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
        write_report(args.results_dir, run_id, failure)
        print(json.dumps(failure, ensure_ascii=False, indent=2), file=sys.stderr)
        record_route_feedback(
            args, routing, run_id, started_at, 1, client.observed_usage()
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
    write_report(args.results_dir, run_id, payload)
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
        result,
    )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
