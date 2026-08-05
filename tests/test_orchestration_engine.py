from __future__ import annotations

import json
import pathlib
import sys
import unittest
from typing import Any

SCRIPTS = pathlib.Path(__file__).resolve().parents[1] / "skills" / "codex-auto-router" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from orchestration_engine import CallRecord, RunContext, run_variant, run_workers  # noqa: E402
from model_registry import DEFAULT_REGISTRY_PATH, registry_from_dict  # noqa: E402


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


class FailingGraderClient(FakeClient):
    def create(self, **kwargs: Any) -> tuple[str, dict[str, Any]]:
        if kwargs["role"] == "grader":
            raise ValueError("invalid grader output")
        return super().create(**kwargs)


class ExpandingDispatcherClient(FakeClient):
    def create(self, **kwargs: Any) -> tuple[str, dict[str, Any]]:
        if kwargs["role"] == "dispatcher":
            plan = json.loads(kwargs["input_text"])["plan"]
            plan["tasks"].append({
                "id": "three",
                "description": "unexpected expansion",
                "dependencies": [],
                "acceptance_criteria": [],
            })
            return json.dumps(plan), {}
        return super().create(**kwargs)


class OrchestrationEngineTests(unittest.TestCase):
    case = {"id": "case", "prompt": "Do the work", "acceptance_criteria": ["done"]}

    def test_all_variants_execute_with_fake_client(self) -> None:
        for variant in "ABCDEF":
            with self.subTest(variant=variant):
                result = run_variant(FakeClient(), self.case, variant, 2)
                self.assertEqual(result["variant"], variant)
                self.assertTrue(result["grade"]["passed"])
                self.assertIsNone(result["estimated_cost_usd"])

    def test_default_profiles_preserve_existing_role_models(self) -> None:
        expected = {
            "A": {"direct": "gpt-5.6-sol", "grader": "gpt-5.6-terra"},
            "B": {"planner": "gpt-5.6-sol", "worker": "gpt-5.6-luna", "reviewer": "gpt-5.6-sol", "grader": "gpt-5.6-terra"},
            "C": {"planner": "gpt-5.6-sol", "dispatcher": "gpt-5.6-terra", "worker": "gpt-5.6-luna", "reviewer": "gpt-5.6-sol", "grader": "gpt-5.6-terra"},
            "D": {"planner": "gpt-5.6-terra", "worker": "gpt-5.6-luna", "reviewer": "gpt-5.6-terra", "grader": "gpt-5.6-sol"},
            "E": {"direct": "gpt-5.6-terra", "grader": "gpt-5.6-sol"},
            "F": {"direct": "gpt-5.6-luna", "grader": "gpt-5.6-sol"},
        }
        for variant, roles in expected.items():
            with self.subTest(variant=variant):
                result = run_variant(FakeClient(), self.case, variant, 2)
                self.assertEqual(
                    {role: value["model"] for role, value in result["resolved_roles"].items()},
                    roles,
                )

    def test_high_risk_final_role_cannot_be_replaced_by_unqualified_model(self) -> None:
        payload = json.loads(DEFAULT_REGISTRY_PATH.read_text(encoding="utf-8"))
        payload["models"].append({
            "id": "gpt-frontier-lite",
            "aliases": ["frontier-lite"],
            "tier": "frontier",
            "priority": 1,
            "qualityRank": 2,
            "costRank": 2,
            "latencyRank": 1,
            "defaultEffort": "medium",
            "capabilities": ["coding"],
            "allowedRoles": ["direct", "reviewer"],
            "enabled": True,
            "autoEligible": True,
        })
        result = run_variant(
            FakeClient(),
            self.case,
            "A",
            1,
            grade_enabled=False,
            registry=registry_from_dict(payload, "unit-test"),
            required_capabilities=("high-risk-primary",),
        )
        self.assertEqual(result["resolved_roles"]["direct"]["model"], "gpt-5.6-sol")

    def test_max_workers_must_be_positive(self) -> None:
        with self.assertRaisesRegex(ValueError, "max_workers"):
            run_variant(FakeClient(), self.case, "A", 0)

    def test_grader_failure_preserves_implementation_status(self) -> None:
        result = run_variant(FailingGraderClient(), self.case, "A", 1)
        self.assertEqual(result["implementation_status"], "completed")
        self.assertEqual(result["grading_status"], "failed")
        self.assertFalse(result["grade"]["passed"])

    def test_grader_can_be_skipped_for_token_saving(self) -> None:
        result = run_variant(FakeClient(), self.case, "E", 1, grade_enabled=False)
        self.assertEqual(result["implementation_status"], "completed")
        self.assertEqual(result["grading_status"], "skipped")
        self.assertIsNone(result["grade"]["passed"])
        self.assertEqual([call["role"] for call in result["calls"]], ["direct"])

    def test_dispatcher_cannot_expand_beyond_worker_task_limit(self) -> None:
        with self.assertRaisesRegex(ValueError, "Dispatcher returned more than 2 tasks"):
            run_variant(
                ExpandingDispatcherClient(),
                self.case,
                "C",
                2,
                grade_enabled=False,
                worker_task_limit=2,
            )

    def test_token_report_separates_cached_input(self) -> None:
        context = RunContext(records=[CallRecord(
            "direct", "model", "medium", 0.0, 100, 20, None, "id", 60, 5
        )])
        self.assertEqual(context.total_cached_input_tokens, 60)
        self.assertEqual(context.total_uncached_input_tokens, 40)
        self.assertEqual(context.total_reasoning_output_tokens, 5)

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
