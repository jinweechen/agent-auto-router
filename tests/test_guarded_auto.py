from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import unittest

SCRIPTS = pathlib.Path(__file__).resolve().parents[1] / "skills" / "agent-auto-router" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from guarded_auto import (  # noqa: E402
    EXECUTION_REPORT_SCHEMA,
    configure,
    inferred_samples,
    ingest_execution_report,
    learning_boundary_issue,
    load_config,
    load_state,
    run_cycle,
)
from policy_learning import append_route_event, default_feedback_path, read_feedback  # noqa: E402
from host_permissions import HostPermissions  # noqa: E402
from routing_policy import (  # noqa: E402
    RoutingPolicy,
    load_active_policy,
    load_policy_for_route,
    policy_digest,
)


def route_payload(
    route_id: str,
    *,
    selector_model: str = "codex:gpt-5.6-terra",
    selected_model: str = "codex:gpt-5.6-sol",
    digest: str | None = None,
    validation_passed: bool = True,
    escalated: bool = True,
    attempt_count: int = 2,
    high_risk: bool = False,
) -> dict[str, object]:
    selector_tier = {
        "codex:gpt-5.6-luna": "fast",
        "codex:gpt-5.6-terra": "balanced",
        "codex:gpt-5.6-sol": "frontier",
    }[selector_model]
    return {
        "route_id": route_id,
        "strategy": "balance",
        "effort": "medium",
        "selector_model": selector_model,
        "selected_model": selected_model,
        "target_tier": selector_tier,
        "reason": "complexity" if selector_tier == "frontier" else "balance_default",
        "features": {
            "prompt_chars": 120,
            "criteria_count": 0,
            "complexity_score": 2,
            "risk_score": 4 if high_risk else 0,
            "clarity_score": 0,
            "high_risk": high_risk,
            "constrained": False,
            "parallelizable": False,
            "dependency_ambiguity": False,
            "orchestration_eligible": False,
        },
        "policy_version": "unit-test",
        "policy_digest": digest or policy_digest(RoutingPolicy()),
        "explicit_override": False,
        "exit_code": 0 if validation_passed else 1,
        "duration_ms": 10,
        "validation_configured": True,
        "validation_passed": validation_passed,
        "escalated": escalated,
        "attempt_count": attempt_count,
    }


def enable_for_test(state_dir: pathlib.Path) -> None:
    configure(
        state_dir,
        mode="guarded-auto",
        minimum_signals=4,
        minimum_validation_accuracy_gain=0,
        canary_percent=50,
        minimum_canary_reports=2,
        minimum_baseline_reports=2,
        minimum_probation_reports=2,
        maximum_failure_rate_increase=0,
    )


def seed_candidate(state_dir: pathlib.Path, feedback: pathlib.Path) -> dict[str, object]:
    for index in range(8):
        append_route_event(route_payload(f"signal-{index}"), feedback)
    result = run_cycle(state_dir, feedback)
    if result.get("status") != "canary":
        raise AssertionError(result)
    return result["state"]


class GuardedAutoTests(unittest.TestCase):
    def test_default_mode_is_manual(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            self.assertEqual(load_config(pathlib.Path(temp))["mode"], "manual")

    def test_guarded_state_must_be_outside_child_write_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            state_dir = root / "workspace" / "state"
            permissions = HostPermissions(
                source="unit-test",
                sandbox="workspace-write",
                approval_policy="never",
                network_access=False,
                writable_roots=(str(root / "workspace"),),
                can_request_permissions=False,
            )
            configure(state_dir, mode="guarded-auto")
            issue = learning_boundary_issue(
                state_dir, None, permissions, "workspace-write"
            )
            self.assertIn("outside child writable roots", str(issue))

    def test_guarded_state_rejects_full_access_but_manual_mode_does_not(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state_dir = pathlib.Path(temp) / "state"
            permissions = HostPermissions(
                source="unit-test",
                sandbox="danger-full-access",
                approval_policy="never",
                network_access=True,
                writable_roots=(),
                can_request_permissions=False,
            )
            self.assertIsNone(
                learning_boundary_issue(state_dir, None, permissions)
            )
            configure(state_dir, mode="guarded-auto")
            self.assertIn(
                "requires protected state",
                str(learning_boundary_issue(state_dir, None, permissions)),
            )

    def test_guarded_state_outside_workspace_write_root_is_protected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            state_dir = root / "protected-state"
            permissions = HostPermissions(
                source="unit-test",
                sandbox="workspace-write",
                approval_policy="never",
                network_access=False,
                writable_roots=(str(root / "workspace"),),
                can_request_permissions=False,
            )
            configure(state_dir, mode="guarded-auto")
            self.assertIsNone(
                learning_boundary_issue(
                    state_dir, None, permissions, "workspace-write"
                )
            )

    def test_only_verified_adjacent_tier_escalation_becomes_inferred_signal(self) -> None:
        events = []
        with tempfile.TemporaryDirectory() as temp:
            feedback = pathlib.Path(temp) / "feedback.jsonl"
            append_route_event(route_payload("valid"), feedback)
            append_route_event(
                route_payload("ordinary", escalated=False, attempt_count=1), feedback
            )
            append_route_event(
                route_payload(
                    "skipped-tier",
                    selector_model="codex:gpt-5.6-luna",
                    selected_model="codex:gpt-5.6-sol",
                ),
                feedback,
            )
            append_route_event(route_payload("high-risk", high_risk=True), feedback)
            events = read_feedback(feedback)
        samples = inferred_samples(events)
        self.assertEqual([sample["routeId"] for sample in samples], ["valid"])
        self.assertEqual(samples[0]["labelSource"], "verified-tier-escalation")

    def test_dry_run_does_not_write_candidate_or_lifecycle_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state_dir = pathlib.Path(temp)
            feedback = default_feedback_path(state_dir)
            enable_for_test(state_dir)
            for index in range(8):
                append_route_event(route_payload(f"signal-{index}"), feedback)
            result = run_cycle(state_dir, feedback, dry_run=True)
            self.assertEqual(result["action"], "would-start-canary")
            self.assertFalse((state_dir / "candidates").exists())
            self.assertFalse((state_dir / "guarded-auto-state.json").exists())

    def test_candidate_only_moves_one_threshold_toward_stronger_model(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state_dir = pathlib.Path(temp)
            feedback = default_feedback_path(state_dir)
            enable_for_test(state_dir)
            state = seed_candidate(state_dir, feedback)
            candidate = json.loads(pathlib.Path(state["candidatePath"]).read_text(encoding="utf-8"))
            thresholds = candidate["policy"]["thresholds"]
            self.assertEqual(thresholds["balanceFrontier"], 2)
            self.assertEqual(thresholds["intelligenceFrontier"], 3)
            self.assertEqual(thresholds["costBalanced"], 3)
            self.assertFalse(candidate["requiresHumanApproval"])
            self.assertTrue(candidate["optimizerSettings"]["conservativeOnly"])
            self.assertEqual(candidate["optimizerSettings"]["maximumThresholdStep"], 1)

    def test_canary_selection_is_deterministic_and_integrity_checked(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state_dir = pathlib.Path(temp)
            feedback = default_feedback_path(state_dir)
            enable_for_test(state_dir)
            state = seed_candidate(state_dir, feedback)
            candidate_route = next(
                route_id
                for route_id in (f"route-{index}" for index in range(1000))
                if load_policy_for_route(state_dir, route_id)[1].startswith("guarded-auto-canary:")
            )
            first = load_policy_for_route(state_dir, candidate_route)
            second = load_policy_for_route(state_dir, candidate_route)
            self.assertEqual(first, second)
            self.assertEqual(first[0].balance_frontier_threshold, 2)
            candidate_path = pathlib.Path(state["candidatePath"])
            candidate = json.loads(candidate_path.read_text(encoding="utf-8"))
            candidate["policy"]["thresholds"]["balanceFrontier"] = 1
            candidate_path.write_text(json.dumps(candidate), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "integrity"):
                load_policy_for_route(state_dir, candidate_route)

    def test_canary_promotes_then_probation_rolls_back_on_verified_regression(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state_dir = pathlib.Path(temp)
            feedback = default_feedback_path(state_dir)
            enable_for_test(state_dir)
            state = seed_candidate(state_dir, feedback)
            base_digest = str(state["basePolicyDigest"])
            candidate_digest = str(state["candidatePolicyDigest"])
            for index in range(2):
                append_route_event(
                    route_payload(
                        f"base-pass-{index}",
                        selector_model="codex:gpt-5.6-terra",
                        selected_model="codex:gpt-5.6-terra",
                        digest=base_digest,
                        escalated=False,
                        attempt_count=1,
                    ),
                    feedback,
                )
                append_route_event(
                    route_payload(
                        f"canary-pass-{index}",
                        selector_model="codex:gpt-5.6-sol",
                        selected_model="codex:gpt-5.6-sol",
                        digest=candidate_digest,
                        escalated=False,
                        attempt_count=1,
                    ),
                    feedback,
                )
            promoted = run_cycle(state_dir, feedback)
            self.assertEqual(promoted["action"], "promoted")
            self.assertEqual(policy_digest(load_active_policy(state_dir)[0]), candidate_digest)
            for index in range(2):
                append_route_event(
                    route_payload(
                        f"probation-fail-{index}",
                        selector_model="codex:gpt-5.6-sol",
                        selected_model="codex:gpt-5.6-sol",
                        digest=candidate_digest,
                        validation_passed=False,
                        escalated=False,
                        attempt_count=1,
                    ),
                    feedback,
                )
            rolled_back = run_cycle(state_dir, feedback)
            self.assertEqual(rolled_back["action"], "rolled-back")
            self.assertEqual(policy_digest(load_active_policy(state_dir)[0]), base_digest)
            self.assertEqual(load_state(state_dir)["status"], "idle")

    def test_execution_report_is_idempotent_and_rejects_task_text(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state_dir = pathlib.Path(temp)
            route = {
                "routeId": "desktop-route-1",
                "strategy": "balance",
                "effort": "medium",
                "selectorModel": "codex:gpt-5.6-terra",
                "selectedModel": "codex:gpt-5.6-terra",
                "targetTier": "balanced",
                "reason": "balance_default",
                "features": route_payload("template")["features"],
                "policyVersion": "unit-test",
                "policyDigest": policy_digest(RoutingPolicy()),
                "modelRegistryDigest": None,
                "explicitOverride": False,
            }
            from model_registry import registry_digest, load_model_registry

            route["modelRegistryDigest"] = registry_digest(load_model_registry())
            report = {
                "schema": EXECUTION_REPORT_SCHEMA,
                "reportId": "report-1",
                "host": "codex-desktop",
                "route": route,
                "result": {
                    "status": "succeeded",
                    "durationMs": 100,
                    "verification": "passed",
                    "validationConfigured": True,
                    "escalated": False,
                    "attemptCount": 1,
                },
            }
            self.assertEqual(ingest_execution_report(report, state_dir)["status"], "recorded")
            self.assertEqual(ingest_execution_report(report, state_dir)["status"], "duplicate")
            report["reportId"] = "report-with-task"
            report["route"] = dict(route, task="private")
            with self.assertRaisesRegex(ValueError, "unsupported fields"):
                ingest_execution_report(report, state_dir)

    def test_disabling_during_probation_restores_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state_dir = pathlib.Path(temp)
            feedback = default_feedback_path(state_dir)
            enable_for_test(state_dir)
            state = seed_candidate(state_dir, feedback)
            base_digest = str(state["basePolicyDigest"])
            candidate_digest = str(state["candidatePolicyDigest"])
            for index in range(2):
                append_route_event(
                    route_payload(
                        f"disable-base-{index}",
                        selected_model="codex:gpt-5.6-terra",
                        digest=base_digest,
                        escalated=False,
                        attempt_count=1,
                    ),
                    feedback,
                )
                append_route_event(
                    route_payload(
                        f"disable-canary-{index}",
                        selector_model="codex:gpt-5.6-sol",
                        selected_model="codex:gpt-5.6-sol",
                        digest=candidate_digest,
                        escalated=False,
                        attempt_count=1,
                    ),
                    feedback,
                )
            self.assertEqual(run_cycle(state_dir, feedback)["action"], "promoted")
            configure(state_dir, mode="manual")
            self.assertEqual(policy_digest(load_active_policy(state_dir)[0]), base_digest)
            self.assertEqual(load_state(state_dir)["status"], "idle")

    def test_rejected_candidate_is_not_recomputed_without_new_strong_signals(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state_dir = pathlib.Path(temp)
            feedback = default_feedback_path(state_dir)
            enable_for_test(state_dir)
            for index in range(8):
                payload = route_payload(f"already-frontier-{index}")
                payload["features"] = dict(payload["features"], complexity_score=3)
                append_route_event(payload, feedback)
            first = run_cycle(state_dir, feedback)
            second = run_cycle(state_dir, feedback)
            self.assertEqual(first["action"], "rejected")
            self.assertEqual(second["status"], "waiting-for-new-signals")
            self.assertEqual(second["nextEvaluationAt"], 12)


if __name__ == "__main__":
    unittest.main()
