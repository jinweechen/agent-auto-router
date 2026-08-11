from __future__ import annotations

import argparse
import pathlib
import concurrent.futures
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

SCRIPTS = pathlib.Path(__file__).resolve().parents[1] / "skills" / "agent-auto-router" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from claude_cli_adapter import ClaudeCliAdapter  # noqa: E402
from codex_cli_adapter import CodexCliAdapter, extract_usage_details  # noqa: E402
from single_task_runner import parse_json_lines, parse_runner_input, usage_is_available  # noqa: E402
from execution_policy import ExecutionPolicy  # noqa: E402
from execution_plan import build_execution_plan  # noqa: E402
from routing_policy import select_model  # noqa: E402
from host_permissions import HostPermissions  # noqa: E402
from model_affinity import workspace_identity  # noqa: E402
from invoke_orchestrated_task import (  # noqa: E402
    bounded_worker_task_limit,
    build_report_payload,
    build_adapter,
    estimate_model_calls,
    feedback_execution_identity,
    child_writable_roots,
    path_is_inside_any_root,
    prepare_results_directory,
    results_dir_is_inside_workdir,
    should_run_grader,
    write_report,
    workspace_status,
    workspace_was_modified,
)
import invoke_orchestrated_task  # noqa: E402


def make_client(execution_mode: bool, sandbox: str = "workspace-write") -> CodexCliAdapter:
    client = object.__new__(CodexCliAdapter)
    client.policy = ExecutionPolicy(execution_mode, sandbox)
    client.max_model_calls = None
    client.max_total_tokens = None
    client.calls_started = 0
    client._call_lock = threading.Lock()
    client._call_reservations = {}
    client._token_lock = threading.Lock()
    client.usage_events_observed = 0
    client.observed_input_tokens = 0
    client.observed_cached_input_tokens = 0
    client.observed_cache_write_input_tokens = 0
    client.observed_output_tokens = 0
    client.observed_reasoning_output_tokens = 0
    client._observed_usage_by_model = {}
    return client


class OrchestratedExecutionPolicyTests(unittest.TestCase):
    def test_execution_plan_jointly_selects_effort_topology_and_context(self) -> None:
        fast = build_execution_plan(select_model("Reply with exactly OK", "balance"))
        self.assertEqual(fast["effort"], "medium")
        self.assertEqual(fast["topology"], "direct")
        self.assertEqual(fast["context"]["profile"], "targeted")

        validated_fast = build_execution_plan(select_model(
            "Reply with exactly OK",
            "balance",
            validation_configured=True,
        ))
        self.assertEqual(validated_fast["effort"], "low")

        parallel = build_execution_plan(select_model(
            "Refactor multiple modules in parallel with independent API and tests workstreams",
            "balance",
            acceptance_criteria=["api", "tests", "docs"],
        ))
        self.assertEqual(parallel["topology"], "orchestrated")

    def test_explicit_effort_overrides_execution_plan_recommendation(self) -> None:
        decision = select_model("Reply with exactly OK", "balance")
        plan = build_execution_plan(decision, "high")
        self.assertEqual(plan["effort"], "high")
        self.assertEqual(plan["effortSource"], "explicit")

    def test_explicit_model_plan_uses_registry_defaults_without_escalation(self) -> None:
        completed = subprocess.run(
            [
                sys.executable,
                str(SCRIPTS / "select_auto_model.py"),
                "--text",
                "Reply with exactly OK",
                "--model-choice",
                "sol",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
        )
        route = json.loads(completed.stdout)
        self.assertEqual(route["executionPlan"]["model"], "codex:gpt-5.6-sol")
        self.assertEqual(route["executionPlan"]["requiredTier"], "fast")
        self.assertEqual(route["executionPlan"]["selectedTier"], "frontier")
        self.assertEqual(route["executionPlan"]["effort"], "high")
        self.assertEqual(route["executionPlan"]["effortSource"], "registry-default")
        self.assertFalse(route["executionPlan"]["escalation"]["eligible"])
        self.assertIsNone(route["executionPlan"]["escalation"]["nextModel"])

    def test_backend_constraint_never_enables_explicit_only_auto_models(self) -> None:
        completed = subprocess.run(
            [
                sys.executable,
                str(SCRIPTS / "select_auto_model.py"),
                "--text",
                "Fix an authorization bypass in production",
                "--available-backends",
                "claude",
                "--ignore-active-policy",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertNotIn("claude:opus", completed.stdout)

    def test_explicit_model_choice_can_use_explicit_only_model(self) -> None:
        completed = subprocess.run(
            [
                sys.executable,
                str(SCRIPTS / "select_auto_model.py"),
                "--text",
                "Fix an authorization bypass in production",
                "--available-backends",
                "claude",
                "--model-choice",
                "opus",
                "--ignore-active-policy",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
        )
        route = json.loads(completed.stdout)
        self.assertTrue(route["explicitOverride"])
        self.assertEqual(route["selectedModel"], "claude:opus")

    def test_validation_escalation_stays_on_selected_backend(self) -> None:
        completed = subprocess.run(
            [
                sys.executable,
                str(SCRIPTS / "select_auto_model.py"),
                "--text",
                "Implement a routine change",
                "--available-backends",
                "claude",
                "--ignore-active-policy",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
        )
        escalation = json.loads(completed.stdout)["executionPlan"]["escalation"]
        self.assertFalse(escalation["eligible"])
        self.assertIsNone(escalation["nextModel"])
        self.assertEqual(
            escalation["unavailableReason"],
            "no_auto_eligible_model_for_available_backends",
        )

    def test_selector_uses_cache_supported_stronger_workspace_affinity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workdir = pathlib.Path(temporary) / "workspace"
            workdir.mkdir()
            feedback_file = pathlib.Path(temporary) / "feedback.jsonl"
            feedback_file.write_text(
                json.dumps(
                    {
                        "eventType": "route_outcome",
                        "routeId": "prior-route",
                        "recordedAt": datetime.now(timezone.utc).isoformat(),
                        "workspaceKey": workspace_identity(workdir),
                        "strategy": "balance",
                        "selectedModel": "codex:gpt-5.6-sol",
                        "executionSucceeded": True,
                        "explicitOverride": False,
                        "observedTokens": {
                            "input": 100,
                            "cached_input": 20,
                            "cache_write": 0,
                        },
                        "selectedModelObservedTokens": {
                            "input": 100,
                            "cached_input": 20,
                            "cache_write": 0,
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            completed = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS / "select_auto_model.py"),
                    "--text",
                    "Implement a routine change",
                    "--workdir",
                    str(workdir),
                    "--repository-context",
                    "off",
                    "--feedback-file",
                    str(feedback_file),
                    "--ignore-active-policy",
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=True,
            )
        route = json.loads(completed.stdout)
        self.assertEqual(
            route["routeDecision"]["selectorModel"], "codex:gpt-5.6-terra"
        )
        self.assertEqual(route["routeDecision"]["targetTier"], "balanced")
        self.assertEqual(route["selectedModel"], "codex:gpt-5.6-sol")
        self.assertEqual(route["selectedTier"], "frontier")
        self.assertEqual(route["executionPlan"]["selectedTier"], "frontier")
        self.assertEqual(route["executionPlan"]["requiredTier"], "balanced")
        self.assertTrue(route["modelAffinity"]["applied"])
        self.assertTrue(route["modelAffinity"]["retainedStrongerTier"])
        self.assertFalse(route["modelAffinity"]["storesWorkspacePath"])
        self.assertFalse(route["executionPlan"]["escalation"]["eligible"])

    def test_affinity_off_does_not_read_corrupt_feedback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            feedback_file = pathlib.Path(temporary) / "feedback.jsonl"
            feedback_file.write_text("not-json\n", encoding="utf-8")
            completed = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS / "select_auto_model.py"),
                    "--text",
                    "Implement a routine change",
                    "--workdir",
                    temporary,
                    "--repository-context",
                    "off",
                    "--feedback-file",
                    str(feedback_file),
                    "--model-affinity",
                    "off",
                    "--ignore-active-policy",
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=True,
            )
        affinity = json.loads(completed.stdout)["modelAffinity"]
        self.assertEqual(affinity["mode"], "off")
        self.assertEqual(affinity["reason"], "disabled")
        self.assertNotIn("errorType", affinity)

    def test_orchestration_blocks_when_backend_has_no_auto_eligible_tier(self) -> None:
        completed = subprocess.run(
            [
                sys.executable,
                str(SCRIPTS / "invoke_orchestrated_task.py"),
                "--text",
                "Fix an authorization bypass in production",
                "--backend",
                "claude",
                "--dry-run",
                "--workdir",
                str(SCRIPTS.parents[2]),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        self.assertEqual(completed.returncode, 2)
        self.assertIn('"reason": "model_resolution_failed"', completed.stderr)
        self.assertNotIn("claude:opus", completed.stdout)

    def test_evaluation_mode_is_always_read_only(self) -> None:
        client = make_client(False)
        for role in ("planner", "dispatcher", "worker:one", "reviewer", "direct", "grader"):
            with self.subTest(role=role):
                self.assertEqual(client.sandbox_for_role(role), "read-only")
                self.assertEqual(client.configuration_flags(role), ["--ignore-user-config"])
                self.assertNotIn("--ignore-rules", client.configuration_flags(role))

    def test_only_final_execution_roles_receive_write_access(self) -> None:
        client = make_client(True)
        for role in ("planner", "dispatcher", "worker:one", "grader"):
            with self.subTest(role=role):
                self.assertEqual(client.sandbox_for_role(role), "read-only")
        self.assertEqual(client.sandbox_for_role("reviewer"), "workspace-write")
        self.assertEqual(client.sandbox_for_role("direct"), "workspace-write")

    def test_read_only_execution_keeps_final_roles_read_only(self) -> None:
        client = make_client(True, "read-only")
        self.assertEqual(client.sandbox_for_role("reviewer"), "read-only")
        self.assertEqual(client.sandbox_for_role("direct"), "read-only")

    def test_role_preambles_match_permissions(self) -> None:
        client = make_client(True)
        self.assertIn("single implementation role", client.preamble_for_role("reviewer"))
        self.assertIn("Do not modify files", client.preamble_for_role("worker:one"))
        self.assertIn("batch required reads", client.preamble_for_role("reviewer"))

    def test_context_mode_preserves_workspace_rules(self) -> None:
        client = make_client(True)
        client.context_mode = "lean"
        self.assertEqual(client.configuration_flags("planner"), ["--ignore-user-config"])
        self.assertEqual(client.configuration_flags("worker:one"), ["--ignore-user-config"])
        self.assertEqual(client.configuration_flags("direct"), [])
        self.assertEqual(client.configuration_flags("reviewer"), [])
        client.context_mode = "full"
        self.assertEqual(client.configuration_flags("planner"), [])

    def test_usage_details_include_cached_and_reasoning_tokens(self) -> None:
        usage = extract_usage_details([
            {"usage": {
                "input_tokens": 100,
                "output_tokens": 20,
                "input_tokens_details": {"cached_tokens": 60},
                "output_tokens_details": {"reasoning_tokens": 5},
            }}
        ])
        self.assertEqual(usage["cached_input_tokens"], 60)
        self.assertEqual(usage["cache_write_input_tokens"], 0)
        self.assertEqual(usage["reasoning_output_tokens"], 5)

    def test_workspace_change_detection_uses_git_entries(self) -> None:
        clean = {"is_git_repo": True, "dirty": False, "entries": []}
        changed = {"is_git_repo": True, "dirty": True, "entries": [" M app.py"]}
        self.assertTrue(workspace_was_modified(clean, changed))
        self.assertFalse(workspace_was_modified(clean, dict(clean)))

    def test_workspace_change_detection_is_unknown_for_dirty_or_non_git_baselines(self) -> None:
        dirty = {"is_git_repo": True, "dirty": True, "entries": [" M app.py"]}
        changed_dirty = {"is_git_repo": True, "dirty": True, "entries": [" M app.py"]}
        non_git = {"is_git_repo": False, "dirty": None, "entries": []}
        self.assertIsNone(workspace_was_modified(dirty, changed_dirty))
        self.assertIsNone(workspace_was_modified(non_git, non_git))

    def test_workspace_status_timeout_is_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, patch(
            "invoke_orchestrated_task.subprocess.run",
            side_effect=subprocess.TimeoutExpired(["git"], 5),
        ):
            status = workspace_status(pathlib.Path(temporary))
        self.assertEqual(status["status"], "unknown")
        self.assertIsNone(status["is_git_repo"])
        self.assertEqual(status["error"], "git_status_timeout")

    def test_failed_git_status_distinguishes_non_git_from_unknown(self) -> None:
        failed = subprocess.CompletedProcess(["git"], 128, "", "not a repo")
        with tempfile.TemporaryDirectory() as temporary, patch(
            "invoke_orchestrated_task.subprocess.run", return_value=failed
        ):
            root = pathlib.Path(temporary)
            self.assertEqual(workspace_status(root)["status"], "non_git")
            (root / ".git").mkdir()
            self.assertEqual(workspace_status(root)["status"], "unknown")

    def test_write_execution_blocks_unknown_workspace_before_adapter(self) -> None:
        unknown = {
            "status": "unknown", "is_git_repo": None, "dirty": None,
            "entries": [], "error": "git_status_timeout",
        }
        with tempfile.TemporaryDirectory() as temporary, patch.object(
            sys,
            "argv",
            [
                "invoke_orchestrated_task.py", "--text", "Implement change",
                "--workdir", temporary, "--sandbox", "workspace-write",
                "--no-feedback",
            ],
        ), patch(
            "invoke_orchestrated_task.workspace_status", return_value=unknown
        ), patch("invoke_orchestrated_task.build_adapter") as adapter, patch.object(
            sys, "stderr", io.StringIO()
        ):
            self.assertEqual(invoke_orchestrated_task.main(), 2)
        adapter.assert_not_called()

    def test_results_directory_must_be_outside_target_workspace(self) -> None:
        workspace = pathlib.Path("C:/workspace/project")
        self.assertTrue(results_dir_is_inside_workdir(workspace / "results", workspace))
        self.assertFalse(results_dir_is_inside_workdir(pathlib.Path("C:/reports"), workspace))

    def test_results_directory_must_be_outside_every_writable_root(self) -> None:
        workspace = pathlib.Path("C:/workspace/project")
        roots = (workspace, pathlib.Path("C:/shared-output"))
        self.assertTrue(path_is_inside_any_root(pathlib.Path("C:/shared-output/run"), roots))
        self.assertFalse(path_is_inside_any_root(pathlib.Path("C:/private-reports"), roots))
        self.assertIsNone(child_writable_roots("danger-full-access", workspace, None))
        self.assertEqual(
            child_writable_roots("workspace-write", workspace, None),
            (workspace.resolve(),),
        )
        full_host = HostPermissions(
            source="test", sandbox="danger-full-access",
            approval_policy="on-request", network_access=True,
            writable_roots=(), can_request_permissions=True,
        )
        self.assertEqual(
            child_writable_roots("workspace-write", workspace, full_host),
            (workspace.resolve(),),
        )

    def test_dry_run_blocks_results_under_a_secondary_host_writable_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            workspace = root / "workspace"
            reports_root = root / "host-writable-reports"
            workspace.mkdir()
            reports_root.mkdir()
            permissions = {
                "schema": "agent-auto-router.host-permissions.v1",
                "source": "test-host",
                "sandbox": "workspace-write",
                "approvalPolicy": "never",
                "networkAccess": False,
                "writableRoots": [str(workspace), str(reports_root)],
                "canRequestPermissions": False,
            }
            completed = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS / "invoke_orchestrated_task.py"),
                    "--text", "Inspect only",
                    "--dry-run",
                    "--workdir", str(workspace),
                    "--results-dir", str(reports_root / "run"),
                    "--host-permissions-json", json.dumps(permissions),
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
            )
        self.assertEqual(completed.returncode, 2)
        self.assertIn('"reason": "results_dir_writable_by_child"', completed.stderr)
        self.assertNotIn('"routing"', completed.stdout)

    def test_full_access_cannot_persist_a_child_writable_report(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            completed = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS / "invoke_orchestrated_task.py"),
                    "--text", "Inspect only",
                    "--dry-run",
                    "--sandbox", "danger-full-access",
                    "--workdir", temporary,
                    "--results-dir", str(pathlib.Path(temporary).parent / "reports"),
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
            )
        self.assertEqual(completed.returncode, 2)
        self.assertIn('"reason": "results_dir_writable_by_child"', completed.stderr)

    def test_sensitive_report_directory_fails_closed_on_unverified_windows_acl(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, patch(
            "invoke_orchestrated_task.IS_WINDOWS", True
        ), patch(
            "invoke_orchestrated_task.harden_windows_acl"
        ) as harden, patch(
            "invoke_orchestrated_task.windows_acl_is_private", return_value=False
        ):
            with self.assertRaises(PermissionError):
                prepare_results_directory(
                    pathlib.Path(temporary) / "reports",
                    include_model_output=True,
                )
        harden.assert_called_once()

    def test_default_report_removes_model_and_error_content(self) -> None:
        payload = {
            "run_id": "run-1",
            "error": "secret stderr",
            "workspace_before": {"entries": [" M secret-file.txt"]},
            "workdir": r"C:\\secret\\workspace",
            "routing": {
                "registry_source": r"C:\\secret\\model_registry.json",
                "policy": {"source": r"C:\\secret\\active-policy.json"},
            },
            "execution": {
                "final_output": "secret final answer",
                "responseId": "secret-response-id",
                "modelOutput": "secret camel-case output",
                "grade": {
                    "score": 0.75,
                    "passed": False,
                    "unmet_criteria": ["secret criterion"],
                    "critical_errors": ["secret error"],
                    "rationale": "secret rationale",
                },
                "calls": [{"input_tokens": 10, "output_tokens": 2}],
            },
        }
        report = build_report_payload(payload)
        serialized = json.dumps(report)
        self.assertNotIn("secret", serialized)
        self.assertNotIn("final_output", report["execution"])
        self.assertNotIn("entries", report["workspace_before"])
        self.assertNotIn("workdir", report)
        self.assertNotIn("registry_source", report["routing"])
        self.assertEqual(report["routing"]["policy"]["source"], "[redacted-path]")
        self.assertEqual(report["workspace_before"]["entryCount"], 1)
        self.assertEqual(report["execution"]["grade"]["unmetCriteriaCount"], 1)
        self.assertFalse(report["report_privacy"]["includesModelOutput"])
        self.assertTrue(report["report_privacy"]["exclusiveCreate"])
        self.assertEqual(report["report_privacy"]["requestedPosixFileMode"], "0600")
        self.assertEqual(payload["execution"]["final_output"], "secret final answer")

    def test_report_output_requires_explicit_opt_in(self) -> None:
        payload = {"execution": {"final_output": "explicit result"}}
        report = build_report_payload(payload, include_model_output=True)
        self.assertEqual(report["execution"]["final_output"], "explicit result")
        self.assertTrue(report["report_privacy"]["includesModelOutput"])

    def test_report_file_is_private_and_never_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            results_dir = pathlib.Path(temporary) / "reports"
            payload = {"execution": {"final_output": "secret"}}
            report_path = pathlib.Path(
                write_report(results_dir, "fixed", payload) or ""
            )
            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertNotIn("final_output", report["execution"])
            if os.name != "nt":
                self.assertEqual(report_path.stat().st_mode & 0o777, 0o600)
            with self.assertRaises(FileExistsError):
                write_report(results_dir, "fixed", payload)

    @unittest.skipUnless(os.name == "nt", "Windows DACL verification")
    def test_sensitive_report_is_written_only_after_private_dacl_verification(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            results_dir = pathlib.Path(temporary) / "sensitive-reports"
            report_path = pathlib.Path(
                write_report(
                    results_dir,
                    "sensitive",
                    {"execution": {"final_output": "explicit result"}},
                    include_model_output=True,
                ) or ""
            )
            self.assertTrue(report_path.is_file())
            self.assertTrue(invoke_orchestrated_task.windows_acl_is_private(results_dir))
            self.assertTrue(invoke_orchestrated_task.windows_acl_is_private(report_path))

    def test_powershell_report_output_switch_requires_results_directory(self) -> None:
        script = (SCRIPTS / "invoke_orchestrated_task.ps1").read_text(encoding="utf-8")
        self.assertIn("[switch]$IncludeOutputInReport", script)
        self.assertIn("-IncludeOutputInReport requires -ResultsDir", script)
        self.assertIn("--include-output-in-report", script)

    def test_model_call_estimates_cover_all_variants(self) -> None:
        self.assertEqual(estimate_model_calls("A"), (2, 2))
        self.assertEqual(estimate_model_calls("D"), (4, 5))
        self.assertEqual(estimate_model_calls("C"), (5, 7))
        self.assertEqual(estimate_model_calls("E", include_grader=False), (1, 1))
        self.assertEqual(estimate_model_calls("D", include_grader=False), (3, 4))

    def test_explicit_variant_rebuilds_all_effective_plan_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            workdir = root / "workspace"
            state_dir = root / "state"
            workdir.mkdir()
            state_dir.mkdir()
            completed = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS / "invoke_orchestrated_task.py"),
                    "--text", "Reply with exactly OK",
                    "--dry-run",
                    "--variant", "C",
                    "--workdir", str(workdir),
                    "--state-dir", str(state_dir),
                    "--repository-context", "off",
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=True,
            )
        plan = json.loads(completed.stdout)["routing"]["routeDecision"]["executionPlan"]
        self.assertEqual(plan["variant"], "C")
        self.assertEqual(plan["variantSource"], "explicit")
        self.assertEqual(plan["topology"], "orchestrated")
        self.assertEqual(plan["maxModelCalls"], 7)
        recommendation = plan["orchestrationRecommendation"]
        self.assertEqual(recommendation["recommendedVariant"], "F")
        self.assertEqual(recommendation["recommendedTopology"], "direct")
        self.assertEqual(recommendation["estimatedMaximumModelCalls"], 1)

    def test_worker_limit_reserves_budget_for_final_roles(self) -> None:
        self.assertEqual(bounded_worker_task_limit("D", False, 3, 2), 1)
        self.assertEqual(bounded_worker_task_limit("C", True, 5, 3), 1)
        self.assertEqual(bounded_worker_task_limit("B", True, 6, 3), 3)

    def test_auto_grader_policy_preserves_quality_boundaries(self) -> None:
        low_risk = {"features": {"high_risk": False}}
        high_risk = {"features": {"high_risk": True}}
        self.assertFalse(should_run_grader(low_risk, "E", "auto"))
        self.assertFalse(should_run_grader(low_risk, "D", "auto"))
        self.assertTrue(should_run_grader(low_risk, "C", "auto"))
        self.assertTrue(should_run_grader(high_risk, "A", "auto"))

    def test_model_call_budget_is_thread_safe(self) -> None:
        client = make_client(True)
        client.max_model_calls = 1
        client.max_total_tokens = None
        client.calls_started = 0
        client.observed_input_tokens = 0
        client.observed_output_tokens = 0
        client._call_lock = threading.Lock()
        self.assertEqual(client.reserve_call("planner"), 1)
        with self.assertRaisesRegex(RuntimeError, "budget exhausted"):
            client.reserve_call("worker:one")

    def test_role_effort_overrides_are_independent(self) -> None:
        client = make_client(True)
        client.effort_override = None
        client.role_efforts = {"worker": "low", "reviewer": "xhigh"}
        self.assertEqual(client.effective_effort("worker:one", "high"), "low")
        self.assertEqual(client.effective_effort("reviewer", "high"), "xhigh")

    def test_observed_token_budget_keeps_final_writer_available(self) -> None:
        client = make_client(True)
        client.max_model_calls = None
        client.max_total_tokens = 100
        client.calls_started = 0
        client.observed_input_tokens = 80
        client.observed_output_tokens = 20
        client._call_lock = threading.Lock()
        with self.assertRaisesRegex(RuntimeError, "token budget exhausted"):
            client.reserve_call("grader")
        self.assertEqual(client.reserve_call("reviewer"), 1)

    def test_projected_non_writer_call_is_rejected_before_overshoot(self) -> None:
        client = make_client(True)
        client.max_model_calls = None
        client.max_total_tokens = 1000
        client.calls_started = 0
        client.observed_input_tokens = 600
        client.observed_output_tokens = 100
        client._call_lock = threading.Lock()
        with self.assertRaisesRegex(RuntimeError, "Projected token budget"):
            client.reserve_call("planner", projected_tokens=400)
        self.assertEqual(client.reserve_call("reviewer", projected_tokens=400), 1)

    def test_concurrent_projected_calls_share_inflight_reservations(self) -> None:
        client = make_client(True)
        client.max_total_tokens = 1000
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(client.reserve_call, f"worker:{index}", 600)
                for index in range(2)
            ]
        outcomes = []
        for future in futures:
            try:
                outcomes.append(future.result())
            except RuntimeError:
                outcomes.append("rejected")
        self.assertEqual(sum(isinstance(item, int) for item in outcomes), 1)
        self.assertEqual(outcomes.count("rejected"), 1)

    def test_observed_usage_reports_all_client_totals(self) -> None:
        client = make_client(True)
        client.observed_input_tokens = 80
        client.observed_cached_input_tokens = 50
        client.observed_cache_write_input_tokens = 10
        client.observed_output_tokens = 20
        client.observed_reasoning_output_tokens = 5
        client.usage_events_observed = 1
        self.assertEqual(client.observed_usage(), {
            "input_tokens": 80,
            "cached_input_tokens": 50,
            "cache_write_input_tokens": 10,
            "output_tokens": 20,
            "reasoning_output_tokens": 5,
            "total_tokens": 100,
        })

    def test_observed_usage_is_unknown_without_usage_event(self) -> None:
        client = make_client(True)
        self.assertIsNone(client.observed_usage())

    def test_observed_usage_is_partitioned_by_backend_qualified_model(self) -> None:
        client = make_client(True)
        client.record_usage(
            {"input_tokens": 100, "cached_input_tokens": 20, "output_tokens": 10},
            model="codex:gpt-5.6-terra",
        )
        client.record_usage(
            {"input_tokens": 50, "cached_input_tokens": 0, "output_tokens": 5},
            model="codex:gpt-5.6-sol",
        )
        usage = client.observed_usage_by_model()
        self.assertEqual(usage["codex:gpt-5.6-terra"]["cached_input_tokens"], 20)
        self.assertEqual(usage["codex:gpt-5.6-sol"]["input_tokens"], 50)
        self.assertEqual(usage["codex:gpt-5.6-sol"]["total_tokens"], 55)

    def test_zero_token_usage_event_is_still_observed(self) -> None:
        self.assertTrue(usage_is_available([{"type": "turn.completed", "usage": {}}]))
        self.assertFalse(usage_is_available([{"type": "turn.started"}]))

    def test_feedback_uses_actual_final_role_model_and_effort(self) -> None:
        routing = {
            "selected_model": "codex:gpt-5.6-luna",
            "effort": "low",
            "selected_variant": "A",
        }
        result = {
            "variant": "A",
            "resolved_roles": {
                "direct": {"model": "codex:gpt-5.6-sol", "effort": "max"}
            },
            "calls": [
                {"role": "direct", "model": "codex:gpt-5.6-sol", "effort": "high"}
            ],
        }
        self.assertEqual(
            feedback_execution_identity(routing, result),
            ("codex:gpt-5.6-sol", "high"),
        )

    def test_single_model_lean_mode_keeps_user_config_for_write_sandbox(self) -> None:
        script = (SCRIPTS / "single_task_runner.py").read_text(encoding="utf-8")
        self.assertIn("codex_command = resolve_codex_command()", script)
        self.assertIn('[*codex_command, "exec", "--ephemeral"]', script)
        self.assertIn(
            'args.context_mode == "lean" and args.sandbox == "read-only"',
            script,
        )

    def test_single_model_execution_records_privacy_minimized_feedback(self) -> None:
        script = (SCRIPTS / "invoke_auto_task.ps1").read_text(encoding="utf-8")
        self.assertIn("policy_learning.py", script)
        self.assertIn("route_id = $routeId", script)
        self.assertNotIn("task = $Task", script.lower())
        self.assertNotIn("prompt = $Task", script.lower())
        self.assertIn("single_task_runner.py", script)
        self.assertNotIn("observed_tokens = $null", script)

    def test_single_runner_parses_only_json_object_events(self) -> None:
        events = parse_json_lines([
            '{"type":"started"}',
            "plain text",
            "[]",
            '{"usage":{"input_tokens":10,"output_tokens":2}}',
        ])
        self.assertEqual(len(events), 2)
        self.assertEqual(events[1]["usage"]["input_tokens"], 10)

    def test_single_runner_accepts_precomputed_context_over_stdin(self) -> None:
        task, context, metadata = parse_runner_input(
            json.dumps({
                "schema": "agent-auto-router.runner-input.v1",
                "task": "Implement the change",
                "repositoryContext": "files=10",
                "repositoryMetadata": {"context_useful": True},
            }),
            "route-envelope",
        )
        self.assertEqual(task, "Implement the change")
        self.assertEqual(context, "files=10")
        self.assertTrue(metadata and metadata["context_useful"])

    def test_single_runner_rejects_invalid_envelope_schema(self) -> None:
        with self.assertRaisesRegex(ValueError, "runner envelope schema"):
            parse_runner_input('{"task":"x"}', "route-envelope")

    def test_single_model_choice_is_validated_by_dynamic_registry(self) -> None:
        script = (SCRIPTS / "invoke_auto_task.ps1").read_text(encoding="utf-8")
        self.assertIn("'--state-dir', $StateDir, '--model-choice', $ModelChoice", script)
        self.assertIn("@('--available-backends', 'codex')", script)
        self.assertIn("'--repository-context', $RepositoryContextMode", script)
        self.assertIn("'--input-format', 'route-envelope'", script)
        self.assertNotIn("gpt-5.6-sol', 'gpt-5.6-terra", script)
        self.assertIn("agent-auto-router.route-decision.v2", script)
        self.assertIn("[string]$routeDecision.executionPlan.effort", script)
        for legacy_decision_access in (
            "$route.decision", "$route.executionPlan", "$route.policy",
            "$route.registry", "$route.repository.",
        ):
            self.assertNotIn(legacy_decision_access, script)

    def test_validation_escalation_is_explicit_bounded_and_argv_based(self) -> None:
        script = (SCRIPTS / "invoke_auto_task.ps1").read_text(encoding="utf-8")
        self.assertIn("[switch]$EscalateOnValidationFailure", script)
        self.assertIn("[string[]]$ValidationCommand", script)
        self.assertIn("$attemptCount = 2", script)
        self.assertIn("effort = $finalEffort", script)
        self.assertIn("$selectorArguments += '--validation-configured'", script)
        escalation_condition = script.split("$needsEscalation =", 1)[1].split(")\nif ($needsEscalation", 1)[0]
        self.assertIn("$codexExitCode -eq 0", escalation_condition)
        self.assertNotIn("$codexExitCode -ne 0", escalation_condition)
        self.assertNotIn("Invoke-Expression", script)

    def test_installer_keeps_backups_outside_skill_discovery_directory(self) -> None:
        script = (SCRIPTS / "install.ps1").read_text(encoding="utf-8")
        self.assertIn("skill-backups\\agent-auto-router", script)
        self.assertNotIn('$backupPath = "$targetPath.backup-', script)
        self.assertIn("__pycache__", script)
        self.assertIn("*.pyc", script)

    def test_build_adapter_default_is_codex(self) -> None:
        args = argparse.Namespace(
            timeout=600, effort=None, workdir=pathlib.Path.cwd(),
            sandbox="workspace-write", total_timeout=1800,
            max_model_calls=7, max_total_tokens=None, context_mode="lean",
        )
        adapter = build_adapter(None, args, {}, None)
        self.assertIsInstance(adapter, CodexCliAdapter)

    def test_build_adapter_claude_with_write_sandbox(self) -> None:
        args = argparse.Namespace(
            timeout=600, effort=None, workdir=pathlib.Path.cwd(),
            sandbox="workspace-write", total_timeout=1800,
            max_model_calls=7, max_total_tokens=None, context_mode="lean",
        )
        adapter = build_adapter("claude", args, {}, None)
        self.assertIsInstance(adapter, ClaudeCliAdapter)
        self.assertIn("Edit", adapter.allowed_tools)
        self.assertEqual(adapter.policy.write_sandbox, "workspace-write")

    def test_build_adapter_claude_inherits_danger_full_access_only_when_selected(self) -> None:
        args = argparse.Namespace(
            timeout=600, effort=None, workdir=pathlib.Path.cwd(),
            sandbox="danger-full-access", total_timeout=1800,
            max_model_calls=7, max_total_tokens=None, context_mode="lean",
        )
        adapter = build_adapter("claude", args, {}, None)
        self.assertEqual(adapter.policy.write_sandbox, "danger-full-access")
        self.assertIn("Edit", adapter.allowed_tools)

    def test_build_adapter_claude_read_only(self) -> None:
        args = argparse.Namespace(
            timeout=600, effort=None, workdir=pathlib.Path.cwd(),
            sandbox="read-only", total_timeout=1800,
            max_model_calls=7, max_total_tokens=None, context_mode="lean",
        )
        adapter = build_adapter("claude", args, {}, None)
        self.assertIsInstance(adapter, ClaudeCliAdapter)
        self.assertEqual(adapter.allowed_tools, ("Read",))
        self.assertEqual(adapter.policy.write_sandbox, "read-only")


if __name__ == "__main__":
    unittest.main()
