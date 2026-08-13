from __future__ import annotations

import json
import hashlib
import pathlib
import sys
import tempfile
import unittest
from datetime import datetime, timezone

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
    maintain_execution_reports,
    policy_shadow,
    recover_execution_report,
    run_cycle,
)
from policy_learning import append_route_event, default_feedback_path, read_feedback  # noqa: E402
from host_permissions import HostPermissions  # noqa: E402
from routing_policy import (  # noqa: E402
    FEATURE_SCHEMA_VERSION,
    RoutingPolicy,
    load_active_policy,
    load_policy_for_route,
    policy_digest,
)
from state_lock import control_plane_lock  # noqa: E402


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
        "feature_schema_version": FEATURE_SCHEMA_VERSION,
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
        mode="guarded",
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
    def test_default_mode_is_observe(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = load_config(pathlib.Path(temp))
            self.assertEqual(config["mode"], "observe")
            self.assertEqual(config["minimumSignals"], 12)
            self.assertEqual(config["canaryPercent"], 20)

    def test_legacy_learning_config_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state_dir = pathlib.Path(temp)
            config_path = state_dir / "guarded-auto-config.json"
            legacy = {
                "schemaVersion": 1,
                "mode": "manual",
                "minimumSignals": 20,
                "minimumValidationAccuracyGain": 0.05,
                "maximumThresholdStep": 1,
                "canaryPercent": 10,
                "minimumCanaryReports": 10,
                "minimumBaselineReports": 10,
                "minimumProbationReports": 20,
                "maximumFailureRateIncrease": 0.05,
            }
            config_path.write_text(json.dumps(legacy), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "schemaVersion"):
                load_config(state_dir)

    def test_stale_lock_file_does_not_block_cycle(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state_dir = pathlib.Path(temp)
            (state_dir / ".guarded-auto.lock").write_text("stale-pid", encoding="utf-8")
            result = run_cycle(state_dir)
            self.assertEqual(result["status"], "observe")

    def test_active_control_plane_lock_returns_busy(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state_dir = pathlib.Path(temp)
            with control_plane_lock(state_dir) as acquired:
                self.assertTrue(acquired)
                self.assertEqual(run_cycle(state_dir)["status"], "busy")

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
            configure(state_dir, mode="guarded")
            issue = learning_boundary_issue(
                state_dir, None, permissions, "workspace-write"
            )
            self.assertIn("outside child writable roots", str(issue))

    def test_guarded_state_rejects_full_access_but_off_mode_does_not(self) -> None:
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
            configure(state_dir, mode="guarded")
            self.assertIn(
                "requires protected state",
                str(learning_boundary_issue(state_dir, None, permissions)),
            )

    def test_session_affinity_needs_no_protected_persistent_state(self) -> None:
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
                learning_boundary_issue(
                    state_dir,
                    None,
                    permissions,
                    model_affinity_mode="session",
                )
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
            configure(state_dir, mode="guarded")
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

    def test_legacy_feature_feedback_is_not_an_inferred_signal(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            feedback = pathlib.Path(temp) / "feedback.jsonl"
            payload = route_payload("legacy")
            payload.pop("feature_schema_version")
            append_route_event(payload, feedback)
            events = read_feedback(feedback)
        self.assertEqual(events[0]["featureSchemaVersion"], 1)
        self.assertEqual(inferred_samples(events), [])

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
            audit = json.loads(
                (state_dir / "audit.jsonl").read_text(encoding="utf-8").splitlines()[-1]
            )
            self.assertRegex(audit["transactionId"], r"^[a-f0-9]{32}$")
            self.assertTrue((state_dir / ".control-plane-revision.json").is_file())

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
            configure(state_dir, mode="observe")
            route = {
                "routeId": "desktop-route-1",
                "strategy": "balance",
                "effort": "medium",
                "selectorModel": "codex:gpt-5.6-terra",
                "selectedModel": "codex:gpt-5.6-terra",
                "targetTier": "balanced",
                "reason": "balance_default",
                "featureSchemaVersion": FEATURE_SCHEMA_VERSION,
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
            stored = read_feedback(default_feedback_path(state_dir))
            self.assertEqual(stored[0]["featureSchemaVersion"], FEATURE_SCHEMA_VERSION)
            report["reportId"] = "report-with-task"
            report["route"] = dict(route, task="private")
            with self.assertRaisesRegex(ValueError, "unsupported fields"):
                ingest_execution_report(report, state_dir)

    def test_execution_report_is_not_persisted_when_learning_is_off(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state_dir = pathlib.Path(temp)
            configure(state_dir, mode="off")
            from model_registry import load_model_registry, registry_digest

            report = {
                "schema": EXECUTION_REPORT_SCHEMA,
                "reportId": "off-report",
                "host": "codex-desktop",
                "route": {
                    "routeId": "off-route",
                    "strategy": "balance",
                    "effort": "medium",
                    "selectorModel": "codex:gpt-5.6-terra",
                    "selectedModel": "codex:gpt-5.6-terra",
                    "targetTier": "balanced",
                    "reason": "balance_default",
                    "featureSchemaVersion": FEATURE_SCHEMA_VERSION,
                    "features": route_payload("template")["features"],
                    "policyVersion": "unit-test",
                    "policyDigest": policy_digest(RoutingPolicy()),
                    "modelRegistryDigest": registry_digest(load_model_registry()),
                    "explicitOverride": False,
                },
                "result": {
                    "status": "succeeded",
                    "durationMs": 100,
                    "verification": "not-run",
                    "validationConfigured": False,
                    "escalated": False,
                    "attemptCount": 1,
                },
            }
            result = ingest_execution_report(report, state_dir)
            self.assertEqual(result["status"], "ignored")
            self.assertEqual(result["reason"], "feedback-disabled")
            self.assertFalse(default_feedback_path(state_dir).exists())

    def test_execution_report_retention_preserves_nonterminal_markers(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state_dir = pathlib.Path(temp)
            reports = state_dir / "reports"
            reports.mkdir()
            markers = [
                ("old-recorded", "recorded", "2025-01-01T00:00:00+00:00"),
                ("recent-a", "recorded", "2026-01-09T00:00:00+00:00"),
                ("recent-b", "recorded", "2026-01-10T00:00:00+00:00"),
                ("pending", "pending", "2025-01-01T00:00:00+00:00"),
                ("incomplete", "incomplete", "2025-01-01T00:00:00+00:00"),
            ]
            for report_id, state, timestamp in markers:
                payload = {
                    "schema": EXECUTION_REPORT_SCHEMA,
                    "reportId": report_id,
                    "recordedAt": timestamp,
                    "host": "unit-test",
                    "routeId": f"route-{report_id}",
                    "state": state,
                    "storesTaskText": False,
                    "labelExpected": False,
                    "routeRecorded": state == "recorded",
                    "labelRecorded": False,
                    "cycleProcessed": state == "recorded",
                    "cycleDeferred": False,
                }
                if state == "recorded":
                    payload["completedAt"] = timestamp
                marker_name = hashlib.sha256(report_id.encode("utf-8")).hexdigest()
                (reports / f"{marker_name}.json").write_text(
                    json.dumps(payload), encoding="utf-8"
                )
            now = datetime(2026, 1, 10, tzinfo=timezone.utc)

            preview = maintain_execution_reports(
                state_dir,
                maximum_markers=1,
                retention_days=7,
                apply=False,
                now=now,
            )
            self.assertTrue(preview["wouldChange"])
            self.assertFalse(preview["applied"])
            self.assertEqual(len(list(reports.glob("*.json"))), 5)

            applied = maintain_execution_reports(
                state_dir,
                maximum_markers=1,
                retention_days=7,
                apply=True,
                now=now,
            )
            retained = {
                json.loads(path.read_text(encoding="utf-8"))["reportId"]
                for path in reports.glob("*.json")
            }
            self.assertEqual(retained, {"recent-b", "pending", "incomplete"})
            self.assertEqual(applied["markersRemovedByAge"], 1)
            self.assertEqual(applied["markersRemovedByLimit"], 1)
            self.assertTrue(applied["idempotencyWindowBounded"])
            self.assertFalse(applied["storesTaskText"])
            self.assertEqual(applied["modelCalls"], 0)

    def test_execution_report_recovery_releases_only_empty_marker_with_exact_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state_dir = pathlib.Path(temp)
            configure(state_dir, mode="observe")
            reports = state_dir / "reports"
            reports.mkdir()
            report_id = "recovery-empty"
            marker = reports / (hashlib.sha256(report_id.encode("utf-8")).hexdigest() + ".json")
            marker.write_text(
                json.dumps(
                    {
                        "schema": EXECUTION_REPORT_SCHEMA,
                        "reportId": report_id,
                        "recordedAt": "2026-01-10T00:00:00+00:00",
                        "host": "unit-test",
                        "routeId": "recovery-empty-route",
                        "state": "pending",
                        "storesTaskText": False,
                        "labelExpected": False,
                        "routeRecorded": False,
                        "labelRecorded": False,
                        "cycleProcessed": False,
                        "cycleDeferred": False,
                    }
                ),
                encoding="utf-8",
            )

            inspected = recover_execution_report(state_dir, report_id)
            self.assertTrue(inspected["allowedActions"]["releaseForRetry"])
            self.assertFalse(inspected["allowedActions"]["acknowledgeRecorded"])
            with self.assertRaisesRegex(ValueError, "exact --confirm-report-id"):
                recover_execution_report(
                    state_dir,
                    report_id,
                    action="release-for-retry",
                    confirm_report_id="wrong",
                    resolved_by="operator",
                )

            released = recover_execution_report(
                state_dir,
                report_id,
                action="release-for-retry",
                confirm_report_id=report_id,
                resolved_by="operator",
            )
            self.assertEqual(released["status"], "released-for-retry")
            self.assertTrue(released["retryAuthorized"])
            self.assertFalse(marker.exists())
            self.assertTrue((reports / "resolved" / marker.name).is_file())
            self.assertFalse(released["policyMutationAuthorized"])
            self.assertEqual(released["modelCalls"], 0)

    def test_execution_report_recovery_acknowledges_existing_evidence_without_mutating_it(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state_dir = pathlib.Path(temp)
            configure(state_dir, mode="observe")
            feedback = default_feedback_path(state_dir)
            append_route_event(route_payload("recovery-recorded-route"), feedback)
            feedback_before = feedback.read_bytes()
            reports = state_dir / "reports"
            reports.mkdir()
            report_id = "recovery-recorded"
            marker = reports / (hashlib.sha256(report_id.encode("utf-8")).hexdigest() + ".json")
            marker.write_text(
                json.dumps(
                    {
                        "schema": EXECUTION_REPORT_SCHEMA,
                        "reportId": report_id,
                        "recordedAt": "2026-01-10T00:00:00+00:00",
                        "host": "unit-test",
                        "routeId": "recovery-recorded-route",
                        "state": "incomplete",
                        "storesTaskText": False,
                        "labelExpected": False,
                        "routeRecorded": True,
                        "labelRecorded": False,
                        "cycleProcessed": False,
                        "cycleDeferred": False,
                        "errorType": "RuntimeError",
                    }
                ),
                encoding="utf-8",
            )

            inspected = recover_execution_report(state_dir, report_id, feedback)
            self.assertFalse(inspected["allowedActions"]["releaseForRetry"])
            self.assertTrue(inspected["allowedActions"]["acknowledgeRecorded"])
            acknowledged = recover_execution_report(
                state_dir,
                report_id,
                feedback,
                action="acknowledge-recorded",
                confirm_report_id=report_id,
                resolved_by="operator",
            )
            self.assertTrue(acknowledged["learningCycleRequired"])
            self.assertEqual(acknowledged["nextCommand"], "cycle")
            self.assertEqual(feedback.read_bytes(), feedback_before)
            stored = json.loads(marker.read_text(encoding="utf-8"))
            self.assertEqual(stored["state"], "recorded")
            self.assertTrue(stored["cycleDeferred"])
            self.assertNotIn("errorType", stored)
            self.assertFalse(acknowledged["policyMutationAuthorized"])

    def test_policy_shadow_is_read_only_and_omits_route_ids(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state_dir = pathlib.Path(temp)
            feedback = default_feedback_path(state_dir)
            enable_for_test(state_dir)
            seed_candidate(state_dir, feedback)
            state_before = (state_dir / "guarded-auto-state.json").read_bytes()
            feedback_before = feedback.read_bytes()

            result = policy_shadow(state_dir, feedback)

            self.assertEqual(result["schema"], "agent-auto-router.policy-shadow")
            self.assertFalse(result["activationAuthorized"])
            self.assertFalse(result["dataset"]["storesRouteIds"])
            self.assertFalse(result["dataset"]["storesTaskText"])
            self.assertEqual(result["modelCalls"], 0)
            self.assertNotIn("signal-", json.dumps(result))
            self.assertEqual(
                (state_dir / "guarded-auto-state.json").read_bytes(), state_before
            )
            self.assertEqual(feedback.read_bytes(), feedback_before)

    def test_execution_report_retention_fails_closed_on_corrupt_marker(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state_dir = pathlib.Path(temp)
            reports = state_dir / "reports"
            reports.mkdir()
            report_id = "corrupt-report"
            marker = reports / (
                hashlib.sha256(report_id.encode("utf-8")).hexdigest() + ".json"
            )
            marker.write_text(
                json.dumps(
                    {
                        "schema": EXECUTION_REPORT_SCHEMA,
                        "reportId": report_id,
                        "recordedAt": "not-a-time",
                        "host": "unit-test",
                        "routeId": "corrupt-route",
                        "state": "recorded",
                        "storesTaskText": False,
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "recordedAt is invalid"):
                maintain_execution_reports(state_dir, apply=True)
            self.assertTrue(marker.exists())

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
            configure(state_dir, mode="off")
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
            self.assertEqual(second["newSignals"], 0)
            self.assertEqual(second["minimumNewSignals"], 4)


if __name__ == "__main__":
    unittest.main()
