from __future__ import annotations

import json
import pathlib
import subprocess
import sys
import tempfile
import unittest

SCRIPTS = pathlib.Path(__file__).resolve().parents[1] / "skills" / "agent-auto-router" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from policy_learning import (  # noqa: E402
    _canonical_digest,
    append_label_event,
    append_route_event,
    approve_candidate,
    build_candidate,
    labeled_samples,
    predict_sample,
    rollback_policy,
)
from model_registry import DEFAULT_REGISTRY_PATH, registry_from_dict  # noqa: E402
from routing_policy import (  # noqa: E402
    RoutingPolicy,
    load_active_policy,
    policy_digest,
)


def route_payload(route_id: str) -> dict[str, object]:
    return {
        "route_id": route_id,
        "strategy": "balance",
        "effort": "medium",
        "selector_model": "codex:gpt-5.6-sol",
        "selected_model": "codex:gpt-5.6-sol",
        "reason": "complexity",
        "features": {
            "prompt_chars": 120,
            "criteria_count": 0,
            "complexity_score": 3,
            "risk_score": 0,
            "clarity_score": 0,
            "high_risk": False,
            "constrained": False,
            "parallelizable": False,
            "dependency_ambiguity": False,
            "orchestration_eligible": False,
        },
        "policy_version": "builtin-v2",
        "policy_digest": policy_digest(RoutingPolicy()),
        "explicit_override": False,
        "exit_code": 0,
        "duration_ms": 100,
    }


def labeled_sample(route_id: str) -> dict[str, object]:
    event = {
        "routeId": route_id,
        "strategy": "balance",
        "effort": "medium",
        "features": route_payload(route_id)["features"],
        "preferredModel": "codex:gpt-5.6-terra",
    }
    return event


class PolicyLearningTests(unittest.TestCase):
    def test_feedback_never_accepts_task_text(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            payload = route_payload("route-1")
            payload["prompt"] = "private task"
            with self.assertRaisesRegex(ValueError, "may not store field: prompt"):
                append_route_event(payload, pathlib.Path(temp) / "feedback.jsonl")

    def test_feedback_stores_features_without_task_text(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = pathlib.Path(temp) / "feedback.jsonl"
            append_route_event(route_payload("route-1"), path)
            stored = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(stored["routeId"], "route-1")
            self.assertEqual(stored["targetTier"], "frontier")
            self.assertTrue(stored["executionSucceeded"])
            self.assertIsNone(stored["observedTokens"])
            self.assertNotIn("prompt", stored)
            self.assertNotIn("task", stored)

    def test_feedback_preserves_cached_and_reasoning_token_details(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = pathlib.Path(temp) / "feedback.jsonl"
            payload = route_payload("route-token-details")
            payload["observed_tokens"] = {
                "input": 100,
                "cached_input": 60,
                "output": 20,
                "reasoning_output": 5,
                "total": 120,
            }
            append_route_event(payload, path)
            stored = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(stored["schemaVersion"], 2)
            self.assertEqual(stored["observedTokens"]["cached_input"], 60)
            self.assertEqual(stored["observedTokens"]["reasoning_output"], 5)

    def test_feedback_rejects_inconsistent_token_details(self) -> None:
        payload = route_payload("route-bad-token-details")
        payload["observed_tokens"] = {
            "input": 10,
            "cached_input": 11,
            "output": 2,
            "reasoning_output": 0,
            "total": 12,
        }
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaisesRegex(ValueError, "cached input"):
                append_route_event(payload, pathlib.Path(temp) / "feedback.jsonl")

    def test_feedback_accepts_actual_reviewer_only_model(self) -> None:
        registry_payload = json.loads(DEFAULT_REGISTRY_PATH.read_text(encoding="utf-8"))
        registry_payload["models"].append({
            "id": "gpt-example-reviewer",
            "aliases": ["example-reviewer"],
            "tier": "frontier",
            "priority": 50,
            "qualityRank": 2,
            "costRank": 2,
            "latencyRank": 2,
            "defaultEffort": "high",
            "capabilities": ["review"],
            "allowedRoles": ["reviewer"],
            "enabled": True,
            "autoEligible": True,
        })
        registry = registry_from_dict(registry_payload, "unit-test")
        payload = route_payload("route-reviewer-model")
        payload["selected_model"] = "gpt-example-reviewer"
        with tempfile.TemporaryDirectory() as temp:
            path = pathlib.Path(temp) / "feedback.jsonl"
            append_route_event(payload, path, registry)
            stored = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(stored["selectedModel"], "gpt-example-reviewer")

    def test_explicit_only_preferred_model_is_not_used_for_learning(self) -> None:
        payload = json.loads(DEFAULT_REGISTRY_PATH.read_text(encoding="utf-8"))
        payload["models"].append({
            "id": "gpt-example-fast",
            "aliases": ["example-fast"],
            "tier": "fast",
            "priority": 5,
            "qualityRank": 1,
            "costRank": 1,
            "latencyRank": 1,
            "defaultEffort": "low",
            "capabilities": ["coding"],
            "allowedRoles": ["direct"],
            "enabled": True,
            "autoEligible": False,
        })
        registry = registry_from_dict(payload, "unit-test")
        events = [
            {
                "eventType": "route_outcome",
                "routeId": "route-1",
                "selectorModel": "codex:gpt-5.6-luna",
                "explicitOverride": False,
                "strategy": "balance",
                "effort": "medium",
                "features": route_payload("route-1")["features"],
            },
            {
                "eventType": "human_label",
                "routeId": "route-1",
                "preferredModel": "gpt-example-fast",
                "outcome": "pass",
            },
        ]
        self.assertEqual(labeled_samples(events, registry), [])

    def test_escalated_route_is_not_used_to_train_initial_tier(self) -> None:
        events = [
            {
                "eventType": "route_outcome",
                "routeId": "route-escalated",
                "selectorModel": "codex:gpt-5.6-luna",
                "selectedModel": "codex:gpt-5.6-terra",
                "explicitOverride": False,
                "escalated": True,
                "strategy": "balance",
                "effort": "low",
                "features": route_payload("route-escalated")["features"],
            },
            {
                "eventType": "human_label",
                "routeId": "route-escalated",
                "preferredModel": "codex:gpt-5.6-terra",
                "outcome": "pass",
            },
        ]
        self.assertEqual(labeled_samples(events), [])

    def test_candidate_improves_validation_before_becoming_eligible(self) -> None:
        samples = [labeled_sample(f"route-{index}") for index in range(24)]
        candidate = build_candidate(samples, RoutingPolicy(), min_labels=20)
        self.assertTrue(candidate["eligibleForApproval"])
        self.assertEqual(candidate["policy"]["thresholds"]["balanceFrontier"], 4)
        self.assertGreater(candidate["evaluation"]["validationAccuracyGain"], 0)
        self.assertTrue(candidate["safetyChecks"]["highRiskAlwaysFrontier"])

    def test_high_risk_prediction_is_fixed_to_frontier(self) -> None:
        sample = labeled_sample("risk")
        sample["features"] = dict(sample["features"], high_risk=True)
        policy = RoutingPolicy(
            intelligence_frontier_threshold=8,
            balance_frontier_threshold=8,
            cost_balanced_threshold=8,
        )
        self.assertEqual(predict_sample(sample, policy), "frontier")

    def test_learning_prediction_matches_fixed_runtime_benchmark_rules(self) -> None:
        policy = RoutingPolicy(
            intelligence_frontier_threshold=8,
            balance_frontier_threshold=8,
            cost_balanced_threshold=8,
        )
        sample = labeled_sample("benchmark-signals")

        sample["strategy"] = "balance"
        sample["features"] = dict(sample["features"], validated_bounded=True)
        self.assertEqual(predict_sample(sample, policy), "fast")

        sample["strategy"] = "cost"
        sample["features"] = dict(
            sample["features"], validated_bounded=False, complex_debugging=True
        )
        self.assertEqual(predict_sample(sample, policy), "balanced")

        sample["features"] = dict(
            sample["features"], complex_debugging=False, long_context=True
        )
        self.assertEqual(predict_sample(sample, policy), "balanced")

        sample["features"] = dict(
            sample["features"], long_context=False, multi_file=True
        )
        self.assertEqual(predict_sample(sample, policy), "balanced")

        sample["features"] = dict(
            sample["features"], multi_file=False, computer_use=True
        )
        self.assertEqual(predict_sample(sample, policy), "frontier")

    def test_candidate_is_bound_to_the_benchmark_prior_snapshot(self) -> None:
        samples = [labeled_sample(f"route-{index}") for index in range(24)]
        candidate = build_candidate(samples, RoutingPolicy(), min_labels=20)
        self.assertRegex(candidate["benchmarkPriorsDigest"], r"^[0-9a-f]{64}$")

        candidate.pop("benchmarkPriorsDigest")
        candidate.pop("candidateId")
        candidate["candidateId"] = _canonical_digest(candidate)
        with tempfile.TemporaryDirectory() as temp:
            candidate_path = pathlib.Path(temp) / "candidate.json"
            candidate_path.write_text(json.dumps(candidate), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "benchmark priors have changed"):
                approve_candidate(candidate_path, pathlib.Path(temp), "unit-test")

    def test_approval_and_rollback_are_explicit_and_versioned(self) -> None:
        samples = [labeled_sample(f"route-{index}") for index in range(24)]
        candidate = build_candidate(samples, RoutingPolicy(), min_labels=20)
        with tempfile.TemporaryDirectory() as temp:
            state_dir = pathlib.Path(temp)
            feedback_path = state_dir / "feedback.jsonl"
            for index in range(24):
                route_id = f"route-{index}"
                append_route_event(route_payload(route_id), feedback_path)
                append_label_event(route_id, "codex:gpt-5.6-terra", "pass", feedback_path)
            candidate_path = state_dir / "candidate.json"
            candidate_path.write_text(json.dumps(candidate), encoding="utf-8")

            approval = approve_candidate(
                candidate_path, state_dir, "unit-test", feedback_path
            )
            active, _ = load_active_policy(state_dir)
            self.assertEqual(active.balance_frontier_threshold, 4)
            self.assertEqual(approval["eventType"], "policy_approved")

            rollback = rollback_policy(state_dir, "unit-test")
            restored, _ = load_active_policy(state_dir)
            self.assertEqual(restored, RoutingPolicy())
            self.assertEqual(rollback["eventType"], "policy_rolled_back")

    def test_ineligible_candidate_cannot_be_approved(self) -> None:
        samples = [labeled_sample(f"route-{index}") for index in range(24)]
        candidate = build_candidate(
            samples, RoutingPolicy(balance_frontier_threshold=4), min_labels=20
        )
        self.assertFalse(candidate["eligibleForApproval"])
        with tempfile.TemporaryDirectory() as temp:
            state_dir = pathlib.Path(temp)
            candidate_path = state_dir / "candidate.json"
            candidate_path.write_text(json.dumps(candidate), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "not eligible"):
                approve_candidate(candidate_path, state_dir, "unit-test")

    def test_forged_metrics_fail_live_revalidation_even_with_new_digest(self) -> None:
        samples = [labeled_sample(f"route-{index}") for index in range(24)]
        candidate = build_candidate(samples, RoutingPolicy(), min_labels=20)
        candidate["evaluation"]["validation"]["candidate"]["accuracy"] = 0.0
        candidate.pop("candidateId")
        candidate["candidateId"] = _canonical_digest(candidate)
        with tempfile.TemporaryDirectory() as temp:
            state_dir = pathlib.Path(temp)
            feedback_path = state_dir / "feedback.jsonl"
            for index in range(24):
                route_id = f"route-{index}"
                append_route_event(route_payload(route_id), feedback_path)
                append_label_event(route_id, "codex:gpt-5.6-terra", "pass", feedback_path)
            candidate_path = state_dir / "candidate.json"
            candidate_path.write_text(json.dumps(candidate), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "failed live revalidation"):
                approve_candidate(
                    candidate_path, state_dir, "unit-test", feedback_path
                )

    def test_registry_change_invalidates_outstanding_candidate(self) -> None:
        samples = [labeled_sample(f"route-{index}") for index in range(24)]
        candidate = build_candidate(samples, RoutingPolicy(), min_labels=20)
        payload = json.loads(DEFAULT_REGISTRY_PATH.read_text(encoding="utf-8"))
        payload["models"].append({
            "id": "gpt-example-explicit",
            "aliases": ["example-explicit"],
            "tier": "balanced",
            "priority": 50,
            "qualityRank": 2,
            "costRank": 2,
            "latencyRank": 2,
            "defaultEffort": "medium",
            "capabilities": ["coding"],
            "allowedRoles": ["direct"],
            "enabled": True,
            "autoEligible": False,
        })
        changed_registry = registry_from_dict(payload, "unit-test")
        with tempfile.TemporaryDirectory() as temp:
            state_dir = pathlib.Path(temp)
            candidate_path = state_dir / "candidate.json"
            candidate_path.write_text(json.dumps(candidate), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "registry has changed"):
                approve_candidate(
                    candidate_path,
                    state_dir,
                    "unit-test",
                    registry=changed_registry,
                )

    def test_twentieth_label_automatically_writes_candidate_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state_dir = pathlib.Path(temp)
            feedback_path = state_dir / "feedback.jsonl"
            for index in range(20):
                append_route_event(route_payload(f"route-{index}"), feedback_path)
            for index in range(19):
                append_label_event(
                    f"route-{index}", "codex:gpt-5.6-terra", "pass", feedback_path
                )
            completed = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS / "policy_learning.py"),
                    "label",
                    "--state-dir",
                    str(state_dir),
                    "--route-id",
                    "route-19",
                    "--preferred-model",
                    "codex:gpt-5.6-terra",
                    "--outcome",
                    "pass",
                ],
                check=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
            result = json.loads(completed.stdout)
            proposal = result["autoProposal"]
            self.assertTrue(proposal["eligibleForApproval"])
            self.assertTrue(pathlib.Path(proposal["path"]).is_file())
            self.assertFalse((state_dir / "active-policy.json").exists())


if __name__ == "__main__":
    unittest.main()
