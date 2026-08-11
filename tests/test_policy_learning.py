from __future__ import annotations

import json
import pathlib
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
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
    maintain_feedback,
    predict_sample,
    read_feedback,
    rollback_policy,
    shadow_policy_comparison,
)
from guarded_auto import configure  # noqa: E402
from model_registry import DEFAULT_REGISTRY_PATH, registry_from_dict  # noqa: E402
from routing_policy import (  # noqa: E402
    FEATURE_SCHEMA_VERSION,
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
        "feature_schema_version": FEATURE_SCHEMA_VERSION,
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
        "featureSchemaVersion": FEATURE_SCHEMA_VERSION,
        "strategy": "balance",
        "effort": "medium",
        "features": route_payload(route_id)["features"],
        "preferredModel": "codex:gpt-5.6-terra",
    }
    return event


class PolicyLearningTests(unittest.TestCase):
    def test_shadow_comparison_detects_holdout_improvement_without_route_ids(self) -> None:
        baseline = RoutingPolicy()
        candidate = RoutingPolicy(
            policy_version="shadow-candidate",
            intelligence_frontier_threshold=baseline.intelligence_frontier_threshold,
            balance_frontier_threshold=baseline.balance_frontier_threshold - 1,
            cost_balanced_threshold=baseline.cost_balanced_threshold,
        )
        complexity = baseline.balance_frontier_threshold - 1
        samples = [
            {
                "routeId": f"private-route-{index}",
                "featureSchemaVersion": FEATURE_SCHEMA_VERSION,
                "strategy": "balance",
                "effort": "medium",
                "features": {"complexity_score": complexity, "high_risk": False},
                "preferredModel": "codex:gpt-5.6-sol",
                "preferredTier": "frontier",
            }
            for index in range(8)
        ]

        result = shadow_policy_comparison(samples, baseline, candidate)

        self.assertEqual(result["assessment"], "candidate-favorable")
        self.assertEqual(result["dataset"]["changedRoutes"], 8)
        self.assertGreater(result["delta"]["holdoutAccuracy"], 0)
        self.assertTrue(result["confidence"]["minimumEffectMet"])
        self.assertTrue(result["confidence"]["statisticallySupported"])
        self.assertLessEqual(
            result["confidence"]["allEvidence"]["twoSidedExactPValue"], 0.10
        )
        self.assertEqual(result["strata"]["minimumStratumSize"], 3)
        self.assertFalse(result["activationAuthorized"])
        self.assertNotIn("private-route", json.dumps(result))
        self.assertEqual(result["modelCalls"], 0)

    def test_shadow_comparison_separates_effect_from_confidence_and_suppresses_small_strata(self) -> None:
        baseline = RoutingPolicy()
        candidate = RoutingPolicy(
            policy_version="shadow-promising",
            intelligence_frontier_threshold=baseline.intelligence_frontier_threshold,
            balance_frontier_threshold=baseline.balance_frontier_threshold - 1,
            cost_balanced_threshold=baseline.cost_balanced_threshold,
        )
        boundary = baseline.balance_frontier_threshold - 1
        samples = []
        for index in range(8):
            changed = index == 0
            samples.append(
                {
                    "routeId": f"suppressed-route-{index}",
                    "featureSchemaVersion": FEATURE_SCHEMA_VERSION,
                    "strategy": "balance",
                    "effort": "medium",
                    "features": {
                        "complexity_score": boundary if changed else 0,
                        "high_risk": False,
                    },
                    "preferredModel": (
                        "codex:gpt-5.6-sol" if changed else "codex:gpt-5.6-terra"
                    ),
                    "preferredTier": "frontier" if changed else "balanced",
                    "labelSource": "verified-tier-escalation" if changed else "human",
                }
            )

        result = shadow_policy_comparison(samples, baseline, candidate)

        self.assertEqual(result["assessment"], "promising-unconfirmed")
        self.assertTrue(result["confidence"]["minimumEffectMet"])
        self.assertFalse(result["confidence"]["statisticallySupported"])
        self.assertEqual(result["confidence"]["allEvidence"]["discordantPairs"], 1)
        self.assertEqual(result["strata"]["suppressedStrata"]["labelSource"], 1)
        self.assertNotIn("suppressed-route", json.dumps(result))
        self.assertFalse(result["activationAuthorized"])

    def test_record_command_respects_off_and_observe_modes(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state_dir = pathlib.Path(temp)
            feedback = state_dir / "feedback.jsonl"
            command = [
                sys.executable,
                str(SCRIPTS / "policy_learning.py"),
                "record",
                "--state-dir",
                str(state_dir),
                "--feedback-file",
                str(feedback),
                "--stdin",
            ]
            configure(state_dir, mode="off")
            disabled = subprocess.run(
                command,
                input=json.dumps(route_payload("off-route")),
                text=True,
                capture_output=True,
                check=True,
            )
            self.assertFalse(json.loads(disabled.stdout)["recorded"])
            self.assertFalse(feedback.exists())

            private_payload = route_payload("private-off-route")
            private_payload["task"] = "must still be rejected"
            rejected = subprocess.run(
                command,
                input=json.dumps(private_payload),
                text=True,
                capture_output=True,
            )
            self.assertNotEqual(rejected.returncode, 0)
            self.assertIn("may not store field", rejected.stderr)

            configure(state_dir, mode="observe")
            observed = subprocess.run(
                command,
                input=json.dumps(route_payload("observe-route")),
                text=True,
                capture_output=True,
                check=True,
            )
            self.assertTrue(json.loads(observed.stdout)["recorded"])
            self.assertEqual(len(read_feedback(feedback)), 1)

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

    def test_legacy_feature_feedback_remains_readable_but_is_not_learned(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = pathlib.Path(temp) / "feedback.jsonl"
            payload = route_payload("legacy-route")
            payload.pop("feature_schema_version")
            append_route_event(payload, path)
            append_label_event(
                "legacy-route", "codex:gpt-5.6-terra", "pass", path
            )
            events = read_feedback(path)
            self.assertEqual(events[0]["featureSchemaVersion"], 1)
            self.assertEqual(labeled_samples(events), [])

    def test_feedback_appends_are_serialized(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = pathlib.Path(temp) / "feedback.jsonl"
            with ThreadPoolExecutor(max_workers=8) as executor:
                events = list(
                    executor.map(
                        lambda index: append_route_event(
                            route_payload(f"route-concurrent-{index}"), path
                        ),
                        range(24),
                    )
                )
            stored = read_feedback(path)
            self.assertEqual(len(events), 24)
            self.assertEqual(len(stored), 24)
            self.assertEqual(
                {event["routeId"] for event in stored},
                {f"route-concurrent-{index}" for index in range(24)},
            )

    def test_feedback_retention_is_dry_run_first_and_preserves_route_label_pairs(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = pathlib.Path(temp) / "feedback.jsonl"
            for route_id in ("old", "recent-a", "recent-b"):
                append_route_event(route_payload(route_id), path)
                append_label_event(
                    route_id, "codex:gpt-5.6-terra", "pass", path
                )
            append_route_event(route_payload("recent-c"), path)
            events = read_feedback(path)
            timestamps = {
                "old": "2025-01-01T00:00:00+00:00",
                "recent-a": "2026-01-08T00:00:00+00:00",
                "recent-b": "2026-01-09T00:00:00+00:00",
                "recent-c": "2026-01-10T00:00:00+00:00",
            }
            for event in events:
                event["recordedAt"] = timestamps[event["routeId"]]
            path.write_text(
                "".join(json.dumps(event) + "\n" for event in events),
                encoding="utf-8",
            )
            now = datetime(2026, 1, 10, tzinfo=timezone.utc)

            preview = maintain_feedback(
                path, maximum_routes=2, retention_days=7, apply=False, now=now
            )
            self.assertTrue(preview["wouldChange"])
            self.assertFalse(preview["applied"])
            self.assertEqual(len(read_feedback(path)), 7)

            applied = maintain_feedback(
                path, maximum_routes=2, retention_days=7, apply=True, now=now
            )
            retained = read_feedback(path)
            self.assertTrue(applied["applied"])
            self.assertEqual(applied["routesRemovedByAge"], 1)
            self.assertEqual(applied["routesRemovedByLimit"], 1)
            self.assertEqual(
                [event["routeId"] for event in retained],
                ["recent-b", "recent-b", "recent-c"],
            )
            self.assertFalse(applied["storesTaskText"])
            self.assertEqual(applied["modelCalls"], 0)

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
            payload["selected_model_observed_tokens"] = {
                "input": 40,
                "cached_input": 20,
                "output": 10,
                "reasoning_output": 2,
                "total": 50,
            }
            append_route_event(payload, path)
            stored = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(stored["schemaVersion"], 5)
            self.assertEqual(stored["featureSchemaVersion"], FEATURE_SCHEMA_VERSION)
            self.assertEqual(stored["observedTokens"]["cached_input"], 60)
            self.assertEqual(stored["observedTokens"]["reasoning_output"], 5)
            self.assertEqual(stored["selectedModelObservedTokens"]["input"], 40)
            self.assertEqual(stored["selectedModelObservedTokens"]["cached_input"], 20)

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
                "featureSchemaVersion": FEATURE_SCHEMA_VERSION,
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
                "featureSchemaVersion": FEATURE_SCHEMA_VERSION,
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
        self.assertEqual(candidate["featureSchemaVersion"], FEATURE_SCHEMA_VERSION)
        self.assertTrue(candidate["eligibleForApproval"])
        self.assertEqual(candidate["policy"]["thresholds"]["balanceFrontier"], 4)
        self.assertGreater(candidate["evaluation"]["validationAccuracyGain"], 0)
        self.assertTrue(candidate["safetyChecks"]["highRiskAlwaysFrontier"])

    def test_candidate_rejects_mixed_feature_schemas(self) -> None:
        samples = [labeled_sample(f"route-{index}") for index in range(24)]
        samples[0]["featureSchemaVersion"] = FEATURE_SCHEMA_VERSION - 1
        with self.assertRaisesRegex(ValueError, "current routing feature schema"):
            build_candidate(samples, RoutingPolicy(), min_labels=20)

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

    def test_feature_schema_change_invalidates_outstanding_candidate(self) -> None:
        samples = [labeled_sample(f"route-{index}") for index in range(24)]
        candidate = build_candidate(samples, RoutingPolicy(), min_labels=20)
        candidate["featureSchemaVersion"] = FEATURE_SCHEMA_VERSION - 1
        candidate.pop("candidateId")
        candidate["candidateId"] = _canonical_digest(candidate)
        with tempfile.TemporaryDirectory() as temp:
            candidate_path = pathlib.Path(temp) / "candidate.json"
            candidate_path.write_text(json.dumps(candidate), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "feature schema has changed"):
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
            self.assertRegex(approval["transactionId"], r"^[a-f0-9]{32}$")

            rollback = rollback_policy(state_dir, "unit-test")
            restored, _ = load_active_policy(state_dir)
            self.assertEqual(restored, RoutingPolicy())
            self.assertEqual(rollback["eventType"], "policy_rolled_back")
            self.assertRegex(rollback["transactionId"], r"^[a-f0-9]{32}$")
            audit_events = [
                json.loads(line)
                for line in (state_dir / "audit.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(len({event["transactionId"] for event in audit_events}), 2)

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
