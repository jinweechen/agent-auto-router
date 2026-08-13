from __future__ import annotations

import json
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


SCRIPTS = pathlib.Path(__file__).resolve().parents[1] / "skills" / "agent-auto-router" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import control_plane_store  # noqa: E402
from control_plane_store import (  # noqa: E402
    ControlPlanePaths,
    ControlPlaneRecoveryRequired,
    commit_control_plane_transaction,
    control_plane_revision,
    recover_pending_transaction,
)
from routing_policy import RoutingPolicy, load_active_policy, policy_to_dict  # noqa: E402


class ControlPlaneStoreTests(unittest.TestCase):
    def test_transaction_commits_related_writes_audit_and_revision(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state_dir = pathlib.Path(temp)
            paths = ControlPlanePaths(state_dir)
            transaction_id = commit_control_plane_transaction(
                state_dir,
                operation="unit-transition",
                writes=(
                    (paths.active_policy, policy_to_dict(RoutingPolicy())),
                    (paths.lifecycle, {"schemaVersion": 1, "status": "idle"}),
                ),
                audit_events=({"eventType": "unit_transition"},),
            )

            self.assertTrue(paths.active_policy.is_file())
            self.assertTrue(paths.lifecycle.is_file())
            self.assertFalse(paths.pending_transaction.exists())
            self.assertEqual(control_plane_revision(state_dir), transaction_id)
            audit = json.loads(paths.audit.read_text(encoding="utf-8").strip())
            self.assertEqual(audit["transactionId"], transaction_id)

    def test_interrupted_transaction_replays_without_duplicate_audit(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state_dir = pathlib.Path(temp)
            paths = ControlPlanePaths(state_dir)
            original_write = control_plane_store.atomic_write_json
            failed = False

            def fail_first_revision(path: pathlib.Path, payload: dict[str, object]) -> None:
                nonlocal failed
                if path.name == paths.revision.name and not failed:
                    failed = True
                    raise OSError("simulated interruption before revision commit")
                original_write(path, payload)

            with mock.patch.object(
                control_plane_store, "atomic_write_json", side_effect=fail_first_revision
            ):
                with self.assertRaisesRegex(OSError, "simulated interruption"):
                    commit_control_plane_transaction(
                        state_dir,
                        operation="unit-recovery",
                        writes=((paths.lifecycle, {"schemaVersion": 1, "status": "idle"}),),
                        audit_events=({"eventType": "unit_recovery"},),
                    )

            self.assertTrue(paths.pending_transaction.is_file())
            self.assertEqual(len(paths.audit.read_text(encoding="utf-8").splitlines()), 1)
            recovered = recover_pending_transaction(state_dir)
            self.assertIsNotNone(recovered)
            self.assertFalse(paths.pending_transaction.exists())
            self.assertEqual(len(paths.audit.read_text(encoding="utf-8").splitlines()), 1)
            self.assertEqual(control_plane_revision(state_dir), recovered)

    def test_pending_transaction_makes_policy_reads_fail_closed_until_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state_dir = pathlib.Path(temp)
            paths = ControlPlanePaths(state_dir)
            original_write = control_plane_store.atomic_write_json
            target_attempted = False

            def fail_before_target(path: pathlib.Path, payload: dict[str, object]) -> None:
                nonlocal target_attempted
                if path == paths.active_policy and not target_attempted:
                    target_attempted = True
                    raise OSError("simulated interruption before policy write")
                original_write(path, payload)

            with mock.patch.object(
                control_plane_store, "atomic_write_json", side_effect=fail_before_target
            ):
                with self.assertRaises(OSError):
                    commit_control_plane_transaction(
                        state_dir,
                        operation="unit-policy-recovery",
                        writes=((paths.active_policy, policy_to_dict(RoutingPolicy())),),
                    )

            with self.assertRaises(ControlPlaneRecoveryRequired):
                load_active_policy(state_dir)
            recover_pending_transaction(state_dir)
            self.assertEqual(load_active_policy(state_dir)[0], RoutingPolicy())

    def test_boundary_cli_reports_pending_recovery_with_zero_model_calls(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state_dir = pathlib.Path(temp)
            paths = ControlPlanePaths(state_dir)
            original_write = control_plane_store.atomic_write_json

            def fail_revision(path: pathlib.Path, payload: dict[str, object]) -> None:
                if path.name == paths.revision.name:
                    raise OSError("simulated interruption")
                original_write(path, payload)

            with mock.patch.object(
                control_plane_store, "atomic_write_json", side_effect=fail_revision
            ):
                with self.assertRaises(OSError):
                    commit_control_plane_transaction(
                        state_dir,
                        operation="unit-boundary-recovery",
                        writes=((paths.lifecycle, {"schemaVersion": 1, "status": "idle"}),),
                    )

            permissions = json.dumps({
                "schema": "agent-auto-router.host-permissions.v1",
                "source": "unit-test",
                "sandbox": "read-only",
                "approvalPolicy": "never",
                "networkAccess": False,
                "writableRoots": [],
                "canRequestPermissions": False,
            })
            completed = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS / "guarded_auto.py"),
                    "check-boundary",
                    "--state-dir",
                    str(state_dir),
                    "--host-permissions-json",
                    permissions,
                    "--requested-sandbox",
                    "read-only",
                ],
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
            self.assertEqual(completed.returncode, 2)
            result = json.loads(completed.stdout)
            self.assertEqual(result["reason"], "guarded-auto-recovery-required")
            self.assertEqual(result["modelCalls"], 0)

    def test_boundary_cli_reports_invalid_permissions_without_usage_text(self) -> None:
        permissions = json.dumps({
            "schema": "agent-auto-router.host-permissions.v1",
            "sandbox": "read-only",
            "approvalPolicy": "never",
            "networkAccess": False,
            "writableRoots": [],
            "canRequestPermissions": False,
        })
        completed = subprocess.run(
            [
                sys.executable,
                str(SCRIPTS / "guarded_auto.py"),
                "check-boundary",
                "--host-permissions-json",
                permissions,
                "--requested-sandbox",
                "read-only",
            ],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        self.assertEqual(completed.returncode, 2)
        result = json.loads(completed.stdout)
        self.assertEqual(result["reason"], "invalid-host-permissions")
        self.assertIn("source", result["message"])
        self.assertEqual(result["modelCalls"], 0)
        self.assertNotIn("usage:", completed.stderr.lower())

    def test_transaction_rejects_targets_outside_state_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            state_dir = root / "state"
            outside = root / "outside.json"
            with self.assertRaisesRegex(ValueError, "outside the state directory"):
                commit_control_plane_transaction(
                    state_dir,
                    operation="unit-path-boundary",
                    writes=((outside, {"safe": True}),),
                )
            self.assertFalse(outside.exists())
            self.assertFalse(ControlPlanePaths(state_dir).pending_transaction.exists())

    def test_transaction_journal_rejects_private_task_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state_dir = pathlib.Path(temp)
            paths = ControlPlanePaths(state_dir)
            with self.assertRaisesRegex(ValueError, "may not store field: task"):
                commit_control_plane_transaction(
                    state_dir,
                    operation="unit-privacy-boundary",
                    writes=((paths.lifecycle, {"task": "private task text"}),),
                )
            self.assertFalse(paths.pending_transaction.exists())

    def test_corrupted_pending_transaction_is_not_silently_discarded(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state_dir = pathlib.Path(temp)
            paths = ControlPlanePaths(state_dir)
            paths.state_dir.mkdir(parents=True, exist_ok=True)
            paths.pending_transaction.write_text("{broken", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "journal is corrupted"):
                recover_pending_transaction(state_dir)
            self.assertTrue(paths.pending_transaction.is_file())


if __name__ == "__main__":
    unittest.main()
