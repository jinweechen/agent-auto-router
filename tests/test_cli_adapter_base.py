from __future__ import annotations

import pathlib
import sys
import unittest
from typing import Any


ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "agent-auto-router" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from claude_cli_adapter import ClaudeCliAdapter  # noqa: E402
from cli_adapter_base import BaseCliAdapter  # noqa: E402
from codex_cli_adapter import CodexCliAdapter  # noqa: E402
from execution_adapter import ExecutionAdapter  # noqa: E402
from execution_types import RunContext  # noqa: E402
from orchestration_engine import (  # noqa: E402
    OrchestrationClient,
    RunContext as ReexportedRunContext,
)


class TestAdapter(BaseCliAdapter):
    def create(self, **_: Any) -> tuple[str, dict[str, Any]]:
        return "ok", {}


def adapter(**overrides: Any) -> TestAdapter:
    options: dict[str, Any] = {
        "timeout_seconds": 60,
        "effort_override": None,
        "role_efforts": None,
        "workdir": ROOT,
        "execution_mode": True,
        "write_sandbox": "workspace-write",
        "total_timeout_seconds": 120,
        "max_model_calls": 3,
        "max_total_tokens": 10,
        "progress_callback": None,
        "host_permissions": None,
    }
    options.update(overrides)
    return TestAdapter(**options)


class CliAdapterBaseTests(unittest.TestCase):
    def test_provider_adapters_share_one_budget_and_telemetry_base(self) -> None:
        self.assertTrue(issubclass(CodexCliAdapter, BaseCliAdapter))
        self.assertTrue(issubclass(ClaudeCliAdapter, BaseCliAdapter))
        self.assertIs(CodexCliAdapter.observed_usage, BaseCliAdapter.observed_usage)
        self.assertIs(ClaudeCliAdapter.reserve_call, BaseCliAdapter.reserve_call)

    def test_execution_protocol_and_context_have_one_source_of_truth(self) -> None:
        self.assertIs(OrchestrationClient, ExecutionAdapter)
        self.assertIs(ReexportedRunContext, RunContext)

    def test_usage_and_budget_reserve_the_final_writer(self) -> None:
        client = adapter()
        self.assertIsNone(client.observed_usage())
        total = client.record_usage(
            {
                "input_tokens": 4,
                "cached_input_tokens": 1,
                "output_tokens": 2,
                "reasoning_output_tokens": 1,
            }
        )
        self.assertEqual(total, 6)
        self.assertEqual(client.observed_usage()["total_tokens"], 6)
        with self.assertRaisesRegex(RuntimeError, "Projected token budget"):
            client.reserve_call("planner", projected_tokens=5)
        call_index = client.reserve_call("reviewer", projected_tokens=100)
        self.assertEqual(call_index, 1)
        client.release_call_reservation(call_index)

    def test_prompt_builder_preserves_role_policy_and_task_boundary(self) -> None:
        client = adapter()
        prompt = client.build_prompt(
            role="planner",
            instructions="Inspect only",
            input_text="Task body",
            max_output_tokens=123,
        )
        self.assertIn("read-only orchestration role", prompt)
        self.assertIn("Workspace:", prompt)
        self.assertIn("Keep the response within 123 tokens", prompt)
        self.assertIn("INSTRUCTIONS:\nInspect only", prompt)
        self.assertIn("INPUT:\nTask body", prompt)

    def test_model_consuming_benchmark_is_not_packaged_with_runtime_skill(self) -> None:
        self.assertFalse((SCRIPTS / "codex_cli_orchestration_eval.py").exists())
        self.assertFalse((SCRIPTS / "eval_cases.json").exists())
        runtime = (SCRIPTS / "invoke_orchestrated_task.py").read_text(encoding="utf-8")
        self.assertNotIn("codex_cli_orchestration_eval", runtime)
        self.assertTrue(
            (ROOT / "benchmarks" / "tools" / "codex_cli_orchestration_eval.py").is_file()
        )
        self.assertTrue((ROOT / "benchmarks" / "cases" / "eval_cases.json").is_file())


if __name__ == "__main__":
    unittest.main()
