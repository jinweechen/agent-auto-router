from __future__ import annotations

import pathlib
import sys
import unittest

SCRIPTS = pathlib.Path(__file__).resolve().parents[1] / "skills" / "codex-auto-router" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from auto_router import route_case  # noqa: E402
from routing_policy import select_model  # noqa: E402


class RoutingPolicyTests(unittest.TestCase):
    def test_balance_constrained_task_uses_luna_and_variant_f(self) -> None:
        prompt = "Rename this field"
        self.assertEqual(select_model(prompt, "balance").model, "gpt-5.6-luna")
        self.assertEqual(route_case({"prompt": prompt}, "balance")["variant"], "F")

    def test_routine_parallel_task_makes_variant_d_reachable(self) -> None:
        case = {
            "prompt": "Implement API and tests for several independent components",
            "acceptance_criteria": ["API", "tests", "docs", "rollback"],
        }
        result = route_case(case, "balance")
        self.assertEqual(result["selected_model"], "gpt-5.6-terra")
        self.assertEqual(result["variant"], "D")

    def test_small_parallel_task_does_not_trigger_orchestration(self) -> None:
        case = {
            "prompt": "Implement API and tests for independent modules",
            "acceptance_criteria": ["implementation", "tests"],
        }
        result = route_case(case, "balance")
        self.assertTrue(result["features"]["parallelizable"])
        self.assertFalse(result["features"]["orchestration_eligible"])
        self.assertEqual(result["variant"], "E")

    def test_acceptance_criteria_alone_do_not_imply_parallel_work(self) -> None:
        case = {
            "prompt": "Implement a routine sequential change",
            "acceptance_criteria": ["one", "two", "three", "four"],
        }
        result = route_case(case, "balance")
        self.assertFalse(result["features"]["parallelizable"])
        self.assertEqual(result["variant"], "E")

    def test_incidental_security_word_does_not_force_sol(self) -> None:
        decision = select_model("Rename the security label", "balance")
        self.assertFalse(decision.high_risk)
        self.assertEqual(decision.model, "gpt-5.6-luna")

    def test_true_high_risk_task_uses_sol_in_every_strategy(self) -> None:
        prompt = "Deploy a production authentication migration and fix vulnerabilities"
        for strategy in ("intelligence", "balance", "cost"):
            with self.subTest(strategy=strategy):
                self.assertEqual(select_model(prompt, strategy).model, "gpt-5.6-sol")

    def test_vulnerability_review_is_inherently_high_risk(self) -> None:
        prompt = "Review a production authentication migration for security vulnerabilities"
        for strategy in ("intelligence", "balance", "cost"):
            with self.subTest(strategy=strategy):
                self.assertEqual(select_model(prompt, strategy).model, "gpt-5.6-sol")

    def test_migration_documentation_is_not_high_risk(self) -> None:
        decision = select_model("整理迁移文档并修复错别字", "balance")
        self.assertFalse(decision.high_risk)
        self.assertEqual(decision.model, "gpt-5.6-luna")

    def test_route_case_uses_explicit_effort(self) -> None:
        result = route_case({"prompt": "Implement a routine change"}, "balance", "xhigh")
        self.assertEqual(result["effort"], "xhigh")
        self.assertEqual(result["selected_model"], "gpt-5.6-sol")

    def test_cost_strategy_uses_terra_for_complex_non_risk_work(self) -> None:
        prompt = "Redesign the distributed architecture and concurrency model"
        self.assertEqual(select_model(prompt, "cost").model, "gpt-5.6-terra")


if __name__ == "__main__":
    unittest.main()
