from __future__ import annotations

import pathlib
import sys
import threading
import unittest

SCRIPTS = pathlib.Path(__file__).resolve().parents[1] / "skills" / "codex-auto-router" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from codex_cli_client import CodexCliClient, extract_usage_details  # noqa: E402
from execution_policy import ExecutionPolicy  # noqa: E402
from invoke_orchestrated_task import (  # noqa: E402
    bounded_worker_task_limit,
    estimate_model_calls,
    results_dir_is_inside_workdir,
    should_run_grader,
    workspace_was_modified,
)


def make_client(execution_mode: bool, sandbox: str = "workspace-write") -> CodexCliClient:
    client = object.__new__(CodexCliClient)
    client.policy = ExecutionPolicy(execution_mode, sandbox)
    return client


class OrchestratedExecutionPolicyTests(unittest.TestCase):
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

    def test_observed_usage_reports_all_client_totals(self) -> None:
        client = make_client(True)
        client.observed_input_tokens = 80
        client.observed_output_tokens = 20
        client._token_lock = threading.Lock()
        self.assertEqual(client.observed_usage(), {
            "input_tokens": 80,
            "output_tokens": 20,
            "total_tokens": 100,
        })

    def test_single_model_lean_mode_keeps_user_config_for_write_sandbox(self) -> None:
        script = (SCRIPTS / "invoke_auto_task.ps1").read_text(encoding="utf-8")
        self.assertIn(
            "$ContextMode -eq 'lean' -and $Sandbox -eq 'read-only'",
            script,
        )


if __name__ == "__main__":
    unittest.main()
