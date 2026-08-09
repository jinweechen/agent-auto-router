from __future__ import annotations

import pathlib
import sys
import unittest

SCRIPTS = pathlib.Path(__file__).resolve().parents[1] / "skills" / "agent-auto-router" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from auto_router import route_case  # noqa: E402
from routing_policy import (  # noqa: E402
    RoutingPolicy,
    policy_from_dict,
    policy_to_dict,
    select_model,
)


class RoutingPolicyTests(unittest.TestCase):
    def test_large_scope_rename_is_not_misclassified_as_constrained(self) -> None:
        decision = select_model(
            "Rename a public API across 200 packages and preserve backward compatibility",
            "balance",
        )
        self.assertFalse(decision.constrained)
        self.assertGreaterEqual(decision.scope_hits, 2)
        self.assertNotEqual(decision.target_tier, "fast")

    def test_algorithmic_work_contributes_complexity_without_generic_keywords(self) -> None:
        decision = select_model(
            "Implement a red-black tree, prove invariants, and add property-based tests",
            "balance",
        )
        self.assertGreaterEqual(decision.algorithm_hits, 3)
        self.assertEqual(decision.target_tier, "frontier")

    def test_balance_constrained_task_uses_luna_and_variant_f(self) -> None:
        prompt = "Rename this field"
        decision = select_model(prompt, "balance")
        self.assertEqual(decision.target_tier, "fast")
        self.assertEqual(decision.model, "codex:gpt-5.6-luna")
        self.assertEqual(route_case({"prompt": prompt}, "balance")["variant"], "F")

    def test_routine_parallel_task_makes_variant_d_reachable(self) -> None:
        case = {
            "prompt": "Implement API and tests for several independent components",
            "acceptance_criteria": ["API", "tests", "docs", "rollback"],
        }
        result = route_case(case, "balance")
        self.assertEqual(result["selected_model"], "codex:gpt-5.6-terra")
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

    def test_chinese_parallel_benchmark_signals_trigger_orchestration(self) -> None:
        case = {
            "prompt": "并行审查多个独立模块，覆盖调试、长上下文和多文件任务，最后统一审查",
        }
        result = route_case(case, "balance")
        self.assertTrue(result["features"]["parallelizable"])
        self.assertTrue(result["features"]["orchestration_eligible"])
        self.assertEqual(result["variant"], "D")

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
        self.assertEqual(decision.model, "codex:gpt-5.6-luna")

    def test_true_high_risk_task_uses_sol_in_every_strategy(self) -> None:
        prompt = "Deploy a production authentication migration and fix vulnerabilities"
        for strategy in ("intelligence", "balance", "cost"):
            with self.subTest(strategy=strategy):
                self.assertEqual(select_model(prompt, strategy).model, "codex:gpt-5.6-sol")

    def test_vulnerability_review_is_inherently_high_risk(self) -> None:
        prompt = "Review a production authentication migration for security vulnerabilities"
        for strategy in ("intelligence", "balance", "cost"):
            with self.subTest(strategy=strategy):
                self.assertEqual(select_model(prompt, strategy).model, "codex:gpt-5.6-sol")

    def test_authorization_bypass_and_privacy_leakage_are_inherently_high_risk(self) -> None:
        prompt = "Review production authentication to prevent authorization bypass and privacy leakage"
        for strategy in ("intelligence", "balance", "cost"):
            with self.subTest(strategy=strategy):
                decision = select_model(prompt, strategy)
                self.assertTrue(decision.high_risk)
                self.assertEqual(decision.target_tier, "frontier")
                self.assertEqual(decision.model, "codex:gpt-5.6-sol")

    def test_migration_documentation_is_not_high_risk(self) -> None:
        decision = select_model("整理迁移文档并修复错别字", "balance")
        self.assertFalse(decision.high_risk)
        self.assertEqual(decision.model, "codex:gpt-5.6-luna")

    def test_route_case_uses_explicit_effort(self) -> None:
        result = route_case({"prompt": "Implement a routine change"}, "balance", "xhigh")
        self.assertEqual(result["effort"], "xhigh")
        self.assertEqual(result["selected_model"], "codex:gpt-5.6-sol")

    def test_cost_strategy_uses_terra_for_complex_non_risk_work(self) -> None:
        prompt = "Redesign the distributed architecture and concurrency model"
        self.assertEqual(select_model(prompt, "cost").model, "codex:gpt-5.6-terra")

    def test_candidate_threshold_can_change_routine_boundary(self) -> None:
        policy = RoutingPolicy(policy_version="test", balance_frontier_threshold=4)
        prompt = "Refactor the integration workflow"
        self.assertEqual(select_model(prompt, "balance").model, "codex:gpt-5.6-sol")
        self.assertEqual(select_model(prompt, "balance", policy=policy).model, "codex:gpt-5.6-terra")

    def test_candidate_threshold_cannot_downgrade_high_risk_work(self) -> None:
        policy = RoutingPolicy(
            policy_version="test",
            intelligence_frontier_threshold=8,
            balance_frontier_threshold=8,
            cost_balanced_threshold=8,
        )
        prompt = "Fix vulnerabilities in production authentication"
        for strategy in ("intelligence", "balance", "cost"):
            with self.subTest(strategy=strategy):
                self.assertEqual(
                    select_model(prompt, strategy, policy=policy).model,
                    "codex:gpt-5.6-sol",
                )

    def test_legacy_policy_is_read_and_rewritten_as_model_agnostic_v2(self) -> None:
        legacy = {
            "schemaVersion": 1,
            "policyVersion": "legacy-v1",
            "thresholds": {
                "intelligenceSol": 4,
                "balanceSol": 5,
                "costTerra": 6,
            },
        }
        policy = policy_from_dict(legacy)
        rewritten = policy_to_dict(policy)
        self.assertEqual(policy.balance_frontier_threshold, 5)
        self.assertEqual(rewritten["schemaVersion"], 2)
        self.assertEqual(rewritten["targetTiers"]["highRisk"], "frontier")

    def test_select_model_backends_parameter(self) -> None:
        # Routine task with claude backends → claude:sonnet (balanced tier)
        decision = select_model(
            "Implement a feature to add user preferences",
            "balance",
            backends=["claude"],
        )
        self.assertEqual(decision.model, "claude:sonnet")

        # Simple constrained task with claude backends → claude:haiku (fast tier)
        decision = select_model(
            "Rename this field",
            "balance",
            backends=["claude"],
        )
        self.assertEqual(decision.model, "claude:haiku")

        # High-risk task with claude backends → ValueError (no autoEligible frontier claude model)
        with self.assertRaisesRegex(ValueError, "backends"):
            select_model(
                "Fix vulnerabilities in production authentication",
                "balance",
                backends=["claude"],
            )

    def test_allow_explicit_only_selects_claude_opus_for_high_risk(self) -> None:
        decision = select_model(
            "Fix vulnerabilities in production authentication",
            "balance",
            backends=["claude"],
            allow_explicit_only=True,
        )
        self.assertEqual(decision.model, "claude:opus")
        self.assertTrue(decision.high_risk)

    def test_validated_bounded_coding_uses_fast_benchmark_prior(self) -> None:
        decision = select_model(
            "Implement a small local helper with tests",
            "balance",
            validation_configured=True,
        )
        self.assertTrue(decision.validated_bounded)
        self.assertEqual(decision.target_tier, "fast")
        self.assertEqual(decision.reason, "benchmark_validated_bounded")
        self.assertIn("validatedBoundedCoding", decision.benchmark_signals)

    def test_complex_debugging_has_balanced_floor_even_in_cost_mode(self) -> None:
        decision = select_model("Diagnose a flaky failing test", "cost")
        self.assertTrue(decision.complex_debugging)
        self.assertEqual(decision.target_tier, "balanced")
        self.assertEqual(decision.reason, "benchmark_debugging_floor")

    def test_long_context_has_balanced_floor_even_in_cost_mode(self) -> None:
        decision = select_model("Inspect a large repository across many files", "cost")
        self.assertTrue(decision.long_context)
        self.assertIn(decision.target_tier, {"balanced", "frontier"})
        self.assertIn("longContext", decision.benchmark_signals)

    def test_coordinated_multi_file_change_has_balanced_floor_in_cost_mode(self) -> None:
        decision = select_model(
            "Implement this feature across multiple modules and coordinate the changes",
            "cost",
        )
        self.assertEqual(decision.target_tier, "balanced")
        self.assertEqual(decision.reason, "benchmark_multi_file_floor")
        self.assertTrue(decision.multi_file)
        self.assertIn("multiFile", decision.benchmark_signals)

    def test_computer_use_uses_frontier_benchmark_prior(self) -> None:
        decision = select_model("Use browser automation to click through this workflow", "cost")
        self.assertTrue(decision.computer_use)
        self.assertEqual(decision.target_tier, "frontier")
        self.assertEqual(decision.reason, "benchmark_computer_use")

    def test_benchmark_prior_metadata_is_auditable(self) -> None:
        decision = select_model("Rename this field", "balance")
        self.assertEqual(decision.benchmark_prior_as_of, "2026-08-09")
        self.assertTrue(decision.benchmark_prior_version)
        self.assertEqual(len(decision.benchmark_prior_digest), 64)


if __name__ == "__main__":
    unittest.main()
