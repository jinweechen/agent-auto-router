from __future__ import annotations

import copy
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest


SCRIPTS = pathlib.Path(__file__).resolve().parents[1] / "skills" / "agent-auto-router" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from auto_router import route_case  # noqa: E402
from route_contract import ROUTE_DECISION_SCHEMA, validate_route_decision  # noqa: E402


class RouteDecisionContractTests(unittest.TestCase):
    @staticmethod
    def execution_envelope(route: dict, task: str) -> str:
        return json.dumps({
            "schema": "agent-auto-router.execution-envelope.v1",
            "task": task,
            "routeDecision": route,
            "hostPermissions": {
                "schema": "agent-auto-router.host-permissions.v1",
                "source": "test-host-turn",
                "sandbox": "read-only",
                "approvalPolicy": "never",
                "networkAccess": False,
                "writableRoots": [],
                "canRequestPermissions": False,
            },
        })

    def select(self, *arguments: str) -> dict:
        completed = subprocess.run(
            [
                sys.executable,
                str(SCRIPTS / "select_auto_model.py"),
                "--ignore-active-policy",
                *arguments,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
        )
        return json.loads(completed.stdout)

    def test_all_python_entrypoints_share_route_decision_v1_keys(self) -> None:
        library_route = route_case({"id": "case-1", "prompt": "Reply with OK"})
        cli_route = self.select("--text", "Reply with OK")
        self.assertEqual(library_route["schema"], ROUTE_DECISION_SCHEMA)
        self.assertEqual(cli_route["schema"], ROUTE_DECISION_SCHEMA)
        self.assertEqual(
            set(library_route["routeDecision"]),
            set(cli_route["routeDecision"]),
        )
        self.assertEqual(
            library_route["routeDecision"]["schema"], ROUTE_DECISION_SCHEMA
        )
        self.assertEqual(cli_route["routeDecision"]["modelCalls"], 0)
        self.assertEqual(
            cli_route["routeDecision"]["matchedSignals"],
            cli_route["matchedSignals"],
        )
        self.assertNotIn("Reply with OK", json.dumps(cli_route["routeDecision"]))

    def test_selector_defaults_to_stateless_direct_routing_without_repo_scan(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            feedback = root / "feedback.jsonl"
            feedback.write_text("not-json\n", encoding="utf-8")
            completed = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS / "select_auto_model.py"),
                    "--text", "Implement a routine change",
                    "--workdir", str(root),
                    "--feedback-file", str(feedback),
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=True,
            )
            route = json.loads(completed.stdout)["routeDecision"]
        self.assertEqual(route["policy"]["source"], "builtin")
        self.assertEqual(route["repository"]["mode"], "off")
        self.assertTrue(route["repository"]["metadata"]["inspection_disabled"])
        self.assertEqual(route["repository"]["metadata"]["scan_duration_ms"], 0)
        self.assertEqual(route["modelAffinity"]["mode"], "off")
        self.assertEqual(route["executionPlan"]["orchestrationPolicy"], "direct")
        self.assertEqual(route["executionPlan"]["topology"], "direct")

    def test_explicit_override_preserves_selector_and_selected_identity(self) -> None:
        route = self.select(
            "--text", "Reply with exactly OK", "--model-choice", "sol"
        )["routeDecision"]
        self.assertTrue(route["explicitOverride"])
        self.assertEqual(route["selectorModel"], "codex:gpt-5.6-luna")
        self.assertEqual(route["targetTier"], "fast")
        self.assertEqual(route["selectedModel"], "codex:gpt-5.6-sol")
        self.assertEqual(route["selectedTier"], "frontier")
        self.assertEqual(route["executionPlan"]["requiredTier"], "fast")
        self.assertEqual(route["executionPlan"]["selectedTier"], "frontier")
        self.assertEqual(route["reasonCode"], "explicit_model")

    def test_route_contract_rejects_raw_workspace_and_repository_paths(self) -> None:
        with self.assertRaisesRegex(ValueError, "SHA-256"):
            route_case({"prompt": "Reply OK", "workspace_key": r"C:\\private\\repo"})
        with self.assertRaisesRegex(ValueError, "unsupported fields: files"):
            route_case({
                "prompt": "Reply OK",
                "workspace_key": "a" * 64,
                "repository_features": {
                    "repo_files": 1,
                    "files": [r"C:\\private\\repo\\secret.py"],
                },
            })
        route = route_case({"prompt": "Reply OK"})["routeDecision"]
        route["modelAffinity"]["secret"] = r"C:\\private\\affinity.json"
        route["executionPlan"]["modelAffinity"] = route["modelAffinity"]
        with self.assertRaisesRegex(ValueError, "field: secret"):
            validate_route_decision(route)

    def test_route_contract_rejects_unknown_content_bearing_extensions(self) -> None:
        route = route_case({"id": "privacy", "prompt": "Reply OK"})["routeDecision"]
        for owner, field in (
            ("root", None),
            ("features", "features"),
            ("affinity", "modelAffinity"),
            ("plan", "executionPlan"),
        ):
            candidate = copy.deepcopy(route)
            target = candidate if field is None else candidate[field]
            target["note"] = "customer-secret-project-orion"
            with self.subTest(owner=owner):
                with self.assertRaisesRegex(ValueError, "unsupported fields: note"):
                    validate_route_decision(candidate)

    def test_route_contract_accepts_only_packaged_matched_terms(self) -> None:
        route = route_case({"id": "signals", "prompt": "Reply OK"})["routeDecision"]
        route["matchedSignals"]["complexity"] = ["customer-secret-project-orion"]
        with self.assertRaisesRegex(ValueError, "non-packaged term"):
            validate_route_decision(route)

    def test_locked_orchestration_reuses_route_without_identity_loss(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workdir = pathlib.Path(temporary) / "workspace"
            workdir.mkdir()
            state_dir = pathlib.Path(temporary) / "state"
            selected = self.select(
                "--text", "Reply with exactly OK",
                "--model-choice", "sol",
                "--workdir", str(workdir),
                "--repository-context", "off",
            )["routeDecision"]
            completed = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS / "invoke_orchestrated_task.py"),
                    "--execution-envelope-stdin",
                    "--dry-run",
                    "--workdir", str(workdir),
                    "--repository-context", "off",
                    "--state-dir", str(state_dir),
                ],
                input=self.execution_envelope(selected, "Reply with exactly OK"),
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=True,
            )
        locked = json.loads(completed.stdout)["routing"]["routeDecision"]
        for key in (
            "routeId", "strategy", "effort", "selectedModel", "selectedTier",
            "selectorModel", "targetTier", "workspaceKey", "modelAffinity",
        ):
            self.assertEqual(locked[key], selected[key])
        self.assertEqual(locked["registry"]["source"], "file:model_registry.json")
        self.assertNotIn(str(workdir), json.dumps(locked))

    def test_locked_orchestration_rejects_a_different_task(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workdir = pathlib.Path(temporary) / "workspace"
            workdir.mkdir()
            state_dir = pathlib.Path(temporary) / "state"
            selected = self.select(
                "--text", "Reply with exactly OK",
                "--workdir", str(workdir),
                "--repository-context", "off",
            )["routeDecision"]
            completed = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS / "invoke_orchestrated_task.py"),
                    "--execution-envelope-stdin",
                    "--dry-run",
                    "--workdir", str(workdir),
                    "--repository-context", "off",
                    "--state-dir", str(state_dir),
                ],
                input=self.execution_envelope(
                    selected, "Delete production authentication data"
                ),
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("task binding does not match", completed.stderr)

    def test_locked_orchestration_rejects_a_different_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            selected_workdir = root / "selected"
            execution_workdir = root / "execution"
            state_dir = root / "state"
            selected_workdir.mkdir()
            execution_workdir.mkdir()
            selected = self.select(
                "--text", "Reply with exactly OK",
                "--workdir", str(selected_workdir),
                "--repository-context", "off",
            )["routeDecision"]
            completed = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS / "invoke_orchestrated_task.py"),
                    "--execution-envelope-stdin",
                    "--dry-run",
                    "--workdir", str(execution_workdir),
                    "--repository-context", "off",
                    "--state-dir", str(state_dir),
                ],
                input=self.execution_envelope(selected, "Reply with exactly OK"),
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("workspaceKey does not match", completed.stderr)


if __name__ == "__main__":
    unittest.main()
