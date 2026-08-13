from __future__ import annotations

import copy
import json
import pathlib
import subprocess
import sys
import unittest


SCRIPTS = pathlib.Path(__file__).resolve().parents[1] / "skills" / "agent-auto-router" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from execution_receipt import (  # noqa: E402
    build_execution_receipt,
    build_execution_receipt_contract,
    build_receipt_plan_binding,
    validate_execution_receipt,
)


def desktop_plan(*, would_write: bool = False) -> dict[str, object]:
    agent: dict[str, object] = {
        "id": "direct",
        "role": "direct",
        "preferredModel": "codex:gpt-5.6-sol",
        "model": "codex:gpt-5.6-sol",
        "modelResolution": "selected-exact",
        "reasoningEffort": "high",
        "receiptIdentity": {
            "requested": {"model": "codex:gpt-5.6-sol", "effort": "high"},
            "resolved": {
                "model": "codex:gpt-5.6-sol",
                "effort": "high",
                "modelResolution": "selected-exact",
            },
            "actualPolicy": "trusted-host-observed-or-unresolved",
        },
        "idempotencyKeyTemplate": "route-1:direct:{instance}",
        "writer": would_write,
        "wouldWrite": would_write,
        "minimumInstances": 1,
        "maximumInstances": 1,
    }
    agent["receiptBinding"] = build_receipt_plan_binding("route-1", agent)
    return {
        "schema": "agent-auto-router.desktop-plan",
        "status": "ready",
        "executionRequested": True,
        "routeId": "route-1",
        "agents": [agent],
        "stages": [
            {
                "id": "direct",
                "agent": "direct",
                "minimumInstances": 1,
                "maximumInstances": 1,
            }
        ],
        "coordination": {
            "executionReceipt": build_execution_receipt_contract(
                required_after_attempt=True
            )
        },
    }


def actual(
    *,
    model: str | None = "codex:gpt-5.6-sol",
    effort: str | None = "high",
) -> dict[str, object]:
    return {
        "state": "observed" if model is not None else "unresolved",
        "model": model,
        "effort": effort,
        "source": "codex-desktop-host-telemetry",
    }


def terminal(outcome: str | None = "succeeded") -> dict[str, object]:
    observed = outcome is not None
    return {
        "finalStatusNotification": observed,
        "childThreadCompleted": False,
        "terminalToolResult": False,
        "finalAnswerReceived": observed,
        "taskCompleteReceived": observed,
        "terminalOutcome": outcome,
    }


def lifecycle(
    *,
    deadline: str = "none",
    deadline_sequence: int | None = None,
    terminal_sequence: int | None = 1,
    interrupted: bool = False,
    grace_expired: bool = False,
    advisory: str = "terminal",
) -> dict[str, object]:
    return {
        "deadlineOutcome": deadline,
        "deadlineSequence": deadline_sequence,
        "terminalSequence": terminal_sequence,
        "interruptAttempted": interrupted,
        "interruptGraceExpired": grace_expired,
        "advisoryHostStatus": advisory,
    }


def validation_check(
    *,
    status: str = "passed",
    owner: str = "deterministic-validator",
) -> dict[str, object]:
    return {
        "name": "unit-tests",
        "status": status,
        "required": True,
        "evidenceOwner": owner,
        "artifactRef": None,
    }


def changed_workspace() -> dict[str, object]:
    return {
        "state": "changed",
        "changedFileCount": 2,
        "digest": "a" * 64,
        "source": "host-runtime",
    }


class ExecutionReceiptTests(unittest.TestCase):
    def build(self, **overrides: object) -> dict[str, object]:
        values: dict[str, object] = {
            "plan": desktop_plan(),
            "stage_id": "direct",
            "instance": 1,
            "attempt_id": "attempt-1",
            "actual": actual(),
            "terminal_evidence": terminal(),
            "lifecycle": lifecycle(),
            "checks": [validation_check()],
        }
        values.update(overrides)
        return build_execution_receipt(**values)

    def test_matched_identity_and_trusted_evidence_are_accepted(self) -> None:
        receipt = self.build()
        self.assertEqual(receipt["schema"], "agent-auto-router.execution-receipt")
        self.assertEqual(receipt["identity"]["decision"], "matched")
        self.assertEqual(receipt["completionDecision"]["outcome"], "succeeded")
        self.assertEqual(receipt["completionDecision"]["uiReconciliation"], "consistent")
        self.assertEqual(receipt["acceptanceDecision"]["status"], "accepted")
        self.assertEqual(receipt["idempotencyKey"], "route-1:direct:1")
        self.assertTrue(all(value is False for value in receipt["privacy"].values()))

    def test_unobserved_actual_identity_is_not_inferred(self) -> None:
        receipt = self.build(actual=actual(model=None, effort=None))
        self.assertEqual(receipt["identity"]["actual"]["state"], "unresolved")
        self.assertIsNone(receipt["identity"]["actual"]["model"])
        self.assertEqual(receipt["identity"]["decision"], "unresolved")
        self.assertEqual(
            receipt["acceptanceDecision"],
            {"status": "pending_evidence", "reason": "actual-identity-unresolved"},
        )

    def test_actual_identity_mismatch_rejects_acceptance(self) -> None:
        receipt = self.build(actual=actual(model="codex:gpt-5.6-terra", effort="medium"))
        self.assertEqual(receipt["identity"]["decision"], "mismatch")
        self.assertEqual(receipt["completionDecision"]["outcome"], "succeeded")
        self.assertEqual(
            receipt["acceptanceDecision"],
            {"status": "rejected", "reason": "actual-identity-mismatch"},
        )

    def test_authoritative_terminal_wins_over_processing_ui(self) -> None:
        receipt = self.build(lifecycle=lifecycle(advisory="processing"))
        self.assertTrue(receipt["completionDecision"]["authoritativeTerminal"])
        self.assertEqual(receipt["completionDecision"]["outcome"], "succeeded")
        self.assertEqual(
            receipt["completionDecision"]["uiReconciliation"], "stale_host_ui"
        )

    def test_late_terminal_does_not_erase_timeout(self) -> None:
        receipt = self.build(
            lifecycle=lifecycle(
                deadline="timed_out",
                deadline_sequence=1,
                terminal_sequence=2,
                interrupted=True,
            ),
        )
        self.assertEqual(receipt["completionDecision"]["outcome"], "timed_out")
        self.assertTrue(receipt["completionDecision"]["lateTerminalAfterTimeout"])
        self.assertEqual(receipt["acceptanceDecision"]["status"], "rejected")

    def test_terminal_before_deadline_wins_by_sequence(self) -> None:
        receipt = self.build(
            lifecycle=lifecycle(
                deadline="timed_out",
                deadline_sequence=2,
                terminal_sequence=1,
                interrupted=True,
            ),
        )
        self.assertEqual(receipt["completionDecision"]["outcome"], "succeeded")
        self.assertFalse(receipt["completionDecision"]["lateTerminalAfterTimeout"])
        self.assertEqual(receipt["acceptanceDecision"]["status"], "accepted")

    def test_grace_expiry_without_terminal_is_orphaned(self) -> None:
        receipt = self.build(
            terminal_evidence=terminal(None),
            lifecycle=lifecycle(
                terminal_sequence=None,
                interrupted=True,
                grace_expired=True,
                advisory="processing",
            ),
        )
        self.assertEqual(receipt["completionDecision"]["outcome"], "orphaned")
        self.assertEqual(receipt["acceptanceDecision"]["status"], "rejected")

    def test_agent_claim_cannot_accept_work(self) -> None:
        receipt = self.build(checks=[validation_check(owner="agent-claim")])
        self.assertEqual(
            receipt["acceptanceDecision"],
            {"status": "pending_evidence", "reason": "required-check-unverified"},
        )

    def test_receipt_rejects_plan_and_idempotency_binding_mismatch(self) -> None:
        route_mismatch = desktop_plan()
        route_mismatch["routeId"] = "another-route"
        with self.assertRaisesRegex(ValueError, "idempotency key template"):
            self.build(plan=route_mismatch)

        binding_mismatch = desktop_plan()
        binding_mismatch["agents"][0]["model"] = "codex:gpt-5.6-terra"
        with self.assertRaisesRegex(ValueError, "resolved identity"):
            self.build(plan=binding_mismatch)

        contract_mismatch = desktop_plan()
        contract_mismatch["coordination"]["executionReceipt"][
            "receiptIdPolicy"
        ] = "attempt-only"
        with self.assertRaisesRegex(ValueError, "contract is incomplete or changed"):
            self.build(plan=contract_mismatch)

    def test_writer_requires_workspace_change_evidence(self) -> None:
        writer_plan = desktop_plan(would_write=True)
        pending = self.build(plan=writer_plan)
        self.assertEqual(
            pending["acceptanceDecision"],
            {"status": "pending_evidence", "reason": "workspace-evidence-unresolved"},
        )
        unchanged = self.build(
            plan=writer_plan,
            workspace_evidence={
                "state": "unchanged",
                "changedFileCount": 0,
                "digest": "b" * 64,
                "source": "host-runtime",
            },
        )
        self.assertEqual(
            unchanged["acceptanceDecision"],
            {"status": "rejected", "reason": "required-workspace-change-missing"},
        )
        accepted = self.build(plan=writer_plan, workspace_evidence=changed_workspace())
        self.assertEqual(accepted["acceptanceDecision"]["status"], "accepted")

    def test_write_intent_cannot_bypass_workspace_evidence_binding(self) -> None:
        inconsistent = desktop_plan(would_write=True)
        inconsistent["agents"][0]["wouldWrite"] = False
        with self.assertRaisesRegex(ValueError, "receipt binding changed"):
            self.build(plan=inconsistent)

        untrusted = changed_workspace()
        untrusted["source"] = "agent-claim"
        with self.assertRaisesRegex(ValueError, "trusted host evidence"):
            self.build(
                plan=desktop_plan(would_write=True),
                workspace_evidence=untrusted,
            )

        digestless = changed_workspace()
        digestless["digest"] = None
        with self.assertRaisesRegex(ValueError, "content digest"):
            self.build(
                plan=desktop_plan(would_write=True),
                workspace_evidence=digestless,
            )

    def test_receipt_id_binds_content_while_attempt_binding_stays_stable(self) -> None:
        matched = self.build()
        unresolved = self.build(actual=actual(model=None, effort=None))
        self.assertEqual(matched["attemptBindingId"], unresolved["attemptBindingId"])
        self.assertNotEqual(matched["receiptId"], unresolved["receiptId"])

    def test_receipt_rejects_unknown_fields_and_tampered_decisions(self) -> None:
        receipt = self.build()
        unknown = copy.deepcopy(receipt)
        unknown["task"] = "private task"
        with self.assertRaisesRegex(ValueError, "unknown=.*task"):
            validate_execution_receipt(unknown)

        tampered = copy.deepcopy(receipt)
        tampered["completionDecision"]["outcome"] = "failed"
        with self.assertRaisesRegex(ValueError, "completionDecision"):
            validate_execution_receipt(tampered)

        rebound = copy.deepcopy(receipt)
        rebound["checks"][0]["status"] = "failed"
        rebound["acceptanceDecision"] = {
            "status": "rejected",
            "reason": "required-check-failed",
        }
        with self.assertRaisesRegex(ValueError, "receiptId"):
            validate_execution_receipt(rebound)

    def test_cli_builds_content_bound_receipt(self) -> None:
        payload = {
            "plan": desktop_plan(),
            "stageId": "direct",
            "instance": 1,
            "attemptId": "attempt-cli",
            "actual": actual(),
            "terminalEvidence": terminal(),
            "lifecycle": lifecycle(),
            "checks": [validation_check()],
        }
        completed = subprocess.run(
            [sys.executable, str(SCRIPTS / "execution_receipt.py")],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        receipt = json.loads(completed.stdout)
        self.assertEqual(receipt["acceptanceDecision"]["status"], "accepted")
        self.assertNotIn("task", receipt)


if __name__ == "__main__":
    unittest.main()
