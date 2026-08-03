from __future__ import annotations

import json
import pathlib
import sys
import unittest
from typing import Any

SCRIPTS = pathlib.Path(__file__).resolve().parents[1] / "skills" / "codex-auto-router" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from orchestration_engine import CallRecord, RunContext, run_variant, run_workers  # noqa: E402


class FakeClient:
    def create(
        self,
        *,
        context: RunContext,
        role: str,
        model: str,
        effort: str,
        instructions: str,
        input_text: str,
        max_output_tokens: int = 4000,
    ) -> tuple[str, dict[str, Any]]:
        context.records.append(
            CallRecord(role, model, effort, 0.0, 0, 0, None, f"fake-{role}")
        )
        if role == "planner":
            output = {"summary": "plan", "tasks": [
                {"id": "one", "description": "first", "dependencies": [], "acceptance_criteria": []},
                {"id": "two", "description": "second", "dependencies": [], "acceptance_criteria": []},
            ]}
            return json.dumps(output), {}
        if role == "dispatcher":
            return json.dumps(json.loads(input_text)["plan"]), {}
        if role == "grader":
            return json.dumps({"score": 1, "passed": True, "unmet_criteria": [], "critical_errors": [], "rationale": "ok"}), {}
        return f"output from {role}", {}


class OrchestrationEngineTests(unittest.TestCase):
    case = {"id": "case", "prompt": "Do the work", "acceptance_criteria": ["done"]}

    def test_all_variants_execute_with_fake_client(self) -> None:
        for variant in "ABCDEF":
            with self.subTest(variant=variant):
                result = run_variant(FakeClient(), self.case, variant, 2)
                self.assertEqual(result["variant"], variant)
                self.assertTrue(result["grade"]["passed"])
                self.assertIsNone(result["estimated_cost_usd"])

    def test_max_workers_must_be_positive(self) -> None:
        with self.assertRaisesRegex(ValueError, "max_workers"):
            run_variant(FakeClient(), self.case, "A", 0)

    def test_unknown_dependency_is_rejected(self) -> None:
        plan = {"tasks": [{"id": "one", "dependencies": ["missing"]}]}
        with self.assertRaisesRegex(ValueError, "unknown dependencies"):
            run_workers(FakeClient(), RunContext(), self.case, plan, 1)

    def test_dependency_cycle_is_rejected(self) -> None:
        plan = {"tasks": [
            {"id": "one", "dependencies": ["two"]},
            {"id": "two", "dependencies": ["one"]},
        ]}
        with self.assertRaisesRegex(ValueError, "cycle"):
            run_workers(FakeClient(), RunContext(), self.case, plan, 2)


if __name__ == "__main__":
    unittest.main()
