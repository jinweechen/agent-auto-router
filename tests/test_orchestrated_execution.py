from __future__ import annotations

import pathlib
import concurrent.futures
import json
import subprocess
import sys
import threading
import unittest

SCRIPTS = pathlib.Path(__file__).resolve().parents[1] / "skills" / "agent-auto-router" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from codex_cli_adapter import CodexCliAdapter, extract_usage_details  # noqa: E402
from single_task_runner import parse_json_lines, usage_is_available  # noqa: E402
from execution_policy import ExecutionPolicy  # noqa: E402
from execution_plan import build_execution_plan  # noqa: E402
from routing_policy import select_model  # noqa: E402
from invoke_orchestrated_task import (  # noqa: E402
    bounded_worker_task_limit,
    estimate_model_calls,
    feedback_execution_identity,
    results_dir_is_inside_workdir,
    should_run_grader,
    workspace_was_modified,
)


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
    client.observed_output_tokens = 0
    client.observed_reasoning_output_tokens = 0
    return client


class OrchestratedExecutionPolicyTests(unittest.TestCase):
    def test_execution_plan_jointly_selects_effort_topology_and_context(self) -> None:
        fast = build_execution_plan(select_model("Reply with exactly OK", "balance"))
        self.assertEqual(fast["effort"], "low")
        self.assertEqual(fast["topology"], "direct")
        self.assertEqual(fast["context"]["profile"], "targeted")

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
        self.assertEqual(route["executionPlan"]["model"], "gpt-5.6-sol")
        self.assertEqual(route["executionPlan"]["tier"], "frontier")
        self.assertEqual(route["executionPlan"]["effort"], "high")
        self.assertEqual(route["executionPlan"]["effortSource"], "registry-default")
        self.assertFalse(route["executionPlan"]["escalation"]["eligible"])
        self.assertIsNone(route["executionPlan"]["escalation"]["nextModel"])

    def test_evaluation_mode_is_always_read_only(self) -> None:
        client = make_client(False)
        for role in ("planner", "dispatcher", "worker:one", "reviewer", "direct", "grader"):
            with self.subTest(role=role):
                self.assertEqual(client.sandbox_for_role(role), "read-only")

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

    def test_results_directory_must_be_outside_target_workspace(self) -> None:
        workspace = pathlib.Path("C:/workspace/project")
        self.assertTrue(results_dir_is_inside_workdir(workspace / "results", workspace))
        self.assertFalse(results_dir_is_inside_workdir(pathlib.Path("C:/reports"), workspace))

    def test_model_call_estimates_cover_all_variants(self) -> None:
        self.assertEqual(estimate_model_calls("A"), (2, 2))
        self.assertEqual(estimate_model_calls("D"), (4, 5))
        self.assertEqual(estimate_model_calls("C"), (5, 7))
        self.assertEqual(estimate_model_calls("E", include_grader=False), (1, 1))
        self.assertEqual(estimate_model_calls("D", include_grader=False), (3, 4))

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
        client.observed_output_tokens = 20
        client.observed_reasoning_output_tokens = 5
        client.usage_events_observed = 1
        self.assertEqual(client.observed_usage(), {
            "input_tokens": 80,
            "cached_input_tokens": 50,
            "output_tokens": 20,
            "reasoning_output_tokens": 5,
            "total_tokens": 100,
        })

    def test_observed_usage_is_unknown_without_usage_event(self) -> None:
        client = make_client(True)
        self.assertIsNone(client.observed_usage())

    def test_zero_token_usage_event_is_still_observed(self) -> None:
        self.assertTrue(usage_is_available([{"type": "turn.completed", "usage": {}}]))
        self.assertFalse(usage_is_available([{"type": "turn.started"}]))

    def test_feedback_uses_actual_final_role_model_and_effort(self) -> None:
        routing = {
            "selected_model": "gpt-5.6-luna",
            "effort": "low",
            "selected_variant": "A",
        }
        result = {
            "variant": "A",
            "resolved_roles": {
                "direct": {"model": "gpt-5.6-sol", "effort": "max"}
            },
            "calls": [
                {"role": "direct", "model": "gpt-5.6-sol", "effort": "high"}
            ],
        }
        self.assertEqual(
            feedback_execution_identity(routing, result),
            ("gpt-5.6-sol", "high"),
        )

    def test_single_model_lean_mode_keeps_user_config_for_write_sandbox(self) -> None:
        script = (SCRIPTS / "single_task_runner.py").read_text(encoding="utf-8")
        self.assertIn('[*resolve_codex_command(), "exec", "--ephemeral"]', script)
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

    def test_single_model_choice_is_validated_by_dynamic_registry(self) -> None:
        script = (SCRIPTS / "invoke_auto_task.ps1").read_text(encoding="utf-8")
        self.assertIn("--model-choice $ModelChoice", script)
        self.assertNotIn("gpt-5.6-sol', 'gpt-5.6-terra", script)
        self.assertIn("[string]$route.selectedDefaultEffort", script)

    def test_validation_escalation_is_explicit_bounded_and_argv_based(self) -> None:
        script = (SCRIPTS / "invoke_auto_task.ps1").read_text(encoding="utf-8")
        self.assertIn("[switch]$EscalateOnValidationFailure", script)
        self.assertIn("[string[]]$ValidationCommand", script)
        self.assertIn("$attemptCount = 2", script)
        self.assertIn("effort = $finalEffort", script)
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


if __name__ == "__main__":
    unittest.main()
