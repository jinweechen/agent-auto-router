from __future__ import annotations

import json
import pathlib
import subprocess
import sys
import unittest


SCRIPTS = pathlib.Path(__file__).resolve().parents[1] / "skills" / "agent-auto-router" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from host_execution_plan import build_host_plan, detect_available_backends  # noqa: E402


def _route(
    *,
    model: str = "codex:gpt-5.6-luna",
    effort: str = "low",
    topology: str = "direct",
    variant: str = "F",
) -> dict:
    return {
        "routeId": "route-test",
        "selectedModel": model,
        "executionPlan": {
            "effort": effort,
            "topology": topology,
            "variant": variant,
            "context": {},
        },
        "decision": {"strategy": "balance", "reason": "test", "target_tier": "fast"},
        "policy": {"version": 0, "digest": "abc"},
        "registry": {"source": "test", "digest": "def"},
    }


def _permissions(sandbox: str = "workspace-write") -> dict:
    return {
        "schema": "agent-auto-router.host-permissions.v1",
        "source": "test-host-turn",
        "sandbox": sandbox,
        "approvalPolicy": "never",
        "networkAccess": False,
        "writableRoots": [str(SCRIPTS.parents[2].resolve())] if sandbox == "workspace-write" else [],
        "canRequestPermissions": False,
    }


class HostExecutionPlanTests(unittest.TestCase):
    def test_automatic_execution_without_permission_snapshot_is_blocked(self) -> None:
        plan = build_host_plan(_route(), workdir=".", available_backends=["codex"])
        self.assertEqual(plan["status"], "blocked")
        self.assertEqual(plan["blocked"]["code"], "host_permissions_required")

    def test_read_only_host_plan_has_no_writer(self) -> None:
        plan = build_host_plan(
            _route(),
            workdir=".",
            available_backends=["codex"],
            host_permissions=_permissions("read-only"),
        )
        self.assertEqual(plan["status"], "ready")
        self.assertFalse(plan["agent"]["writer"])
        self.assertIsNone(plan["hostContract"]["onlyWriter"])
        self.assertEqual(plan["action"]["permissions"]["effectiveSandbox"], "read-only")

    def test_cli_blocks_permission_shape_it_cannot_reproduce(self) -> None:
        plan = build_host_plan(
            _route(),
            workdir=".",
            available_backends=["codex"],
            host_permissions=_permissions("danger-full-access"),
        )
        self.assertEqual(plan["status"], "blocked")
        self.assertEqual(plan["blocked"]["code"], "host_permissions_unrepresentable_by_cli")

    def test_direct_cli_action_is_host_neutral(self) -> None:
        plan = build_host_plan(
            _route(), workdir=".", available_backends=["codex"], host_permissions=_permissions()
        )
        self.assertEqual(plan["schema"], "agent-auto-router.host-plan.v2")
        self.assertEqual(plan["executionBackend"], "host")
        self.assertEqual(plan["action"]["kind"], "cli")
        self.assertEqual(plan["action"]["backend"], "codex")
        self.assertEqual(plan["agent"]["role"], "direct")
        self.assertEqual(plan["agent"]["taskSource"], "host-current-user-task")
        self.assertNotIn("command", plan["action"])

    def test_direct_falls_back_to_explicit_approximate_host_execution(self) -> None:
        plan = build_host_plan(
            _route(), workdir=".", available_backends=["claude"], host_permissions=_permissions()
        )
        self.assertEqual(plan["status"], "ready")
        self.assertEqual(plan["action"]["kind"], "host_execute")
        self.assertEqual(plan["hostContract"]["modelAccuracy"], "approximate")
        self.assertFalse(plan["hostContract"]["silentModelOrProviderFallback"])

    def test_orchestration_action_has_executable_lowercase_argv(self) -> None:
        route = _route(
            model="claude:sonnet", effort="high", topology="orchestrated", variant="D"
        )
        plan = build_host_plan(route, workdir=".", available_backends=["claude"], host_permissions=_permissions())
        self.assertEqual(plan["action"]["kind"], "orchestrate")
        self.assertEqual(plan["action"]["backend"], "claude")
        self.assertEqual(plan["action"]["entrypoint"], "invoke_orchestrated_task.py")
        self.assertIn("--stdin", plan["action"]["argv"])
        self.assertIn("--variant", plan["action"]["argv"])
        self.assertNotIn("--Variant", plan["action"]["argv"])
        self.assertIn("--workdir", plan["action"]["argv"])
        permission_index = plan["action"]["argv"].index("--host-permissions-json")
        forwarded = json.loads(plan["action"]["argv"][permission_index + 1])
        self.assertEqual(forwarded["schema"], "agent-auto-router.host-permissions.v1")
        self.assertEqual(forwarded["sandbox"], "workspace-write")

    def test_orchestration_contract_has_no_direct_agent(self) -> None:
        route = _route(topology="orchestrated", variant="D")
        plan = build_host_plan(route, workdir=".", available_backends=["codex"], host_permissions=_permissions())
        self.assertIsNone(plan["agent"])
        self.assertEqual(plan["hostContract"]["maxAgents"], 0)
        self.assertIsNone(plan["hostContract"]["onlyRole"])
        self.assertEqual(plan["hostContract"]["onlyWriter"], "reviewer")
        self.assertEqual(plan["orchestration"]["onlyWriter"], "reviewer")
        self.assertIn("worker", plan["orchestration"]["readOnlyRoles"])

    def test_orchestration_never_switches_to_another_backend(self) -> None:
        route = _route(
            model="claude:sonnet", effort="high", topology="orchestrated", variant="D"
        )
        plan = build_host_plan(route, workdir=".", available_backends=["codex"], host_permissions=_permissions())
        self.assertEqual(plan["status"], "blocked")
        self.assertEqual(plan["blocked"]["code"], "host_selected_backend_unavailable")
        self.assertEqual(plan["plannedCalls"], 0)

    def test_unknown_programmatic_backend_is_blocked(self) -> None:
        plan = build_host_plan(
            _route(), workdir=".", available_backends=["unknown"], host_permissions=_permissions()
        )
        self.assertEqual(plan["status"], "blocked")
        self.assertEqual(plan["blocked"]["code"], "host_unknown_backend")

    def test_dry_run_has_no_writer_or_planned_call(self) -> None:
        plan = build_host_plan(
            _route(), workdir=".", available_backends=["codex"], host_permissions=_permissions(), dry_run=True
        )
        self.assertFalse(plan["executionRequested"])
        self.assertEqual(plan["plannedCalls"], 0)
        self.assertFalse(plan["agent"]["writer"])
        self.assertEqual(plan["action"]["kind"], "report_plan")

    def test_privacy_fields_omit_task_and_credentials(self) -> None:
        route = _route()
        route["task"] = "private task"
        plan = build_host_plan(route, workdir=".", available_backends=["codex"], host_permissions=_permissions())
        serialized = json.dumps(plan)
        self.assertNotIn("private task", serialized)
        self.assertFalse(plan["privacy"]["taskIncludedInPlan"])
        self.assertFalse(plan["privacy"]["credentialsForwarded"])
        self.assertFalse(plan["privacy"]["hostAppServerAttached"])

    def test_roundtrip_via_generic_main(self) -> None:
        repository = SCRIPTS.parents[2].resolve()
        generated = subprocess.run(
            [
                sys.executable,
                str(SCRIPTS / "select_auto_model.py"),
                "--stdin",
                "--strategy",
                "balance",
                "--available-backends",
                "codex",
            ],
            input="Implement a routine change",
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        self.assertEqual(generated.returncode, 0, generated.stderr)
        completed = subprocess.run(
            [
                sys.executable,
                str(SCRIPTS / "host_execution_plan.py"),
                "--workdir",
                str(repository),
                "--available-backends",
                "codex",
                "--host-permissions-json",
                json.dumps(_permissions()),
            ],
            input=generated.stdout,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        plan = json.loads(completed.stdout)
        self.assertEqual(plan["status"], "ready")
        self.assertEqual(plan["executionBackend"], "host")

    def test_cli_rejects_undeclared_backend(self) -> None:
        completed = subprocess.run(
            [
                sys.executable,
                str(SCRIPTS / "host_execution_plan.py"),
                "--workdir",
                ".",
                "--available-backends",
                "unknown",
            ],
            input=json.dumps(_route()),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("unknown backend", completed.stderr)

    def test_detect_available_backends_is_registry_bounded(self) -> None:
        backends = detect_available_backends({}, ["codex", "claude"])
        self.assertTrue(set(backends).issubset({"codex", "claude"}))

    def test_bogus_route_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            build_host_plan({}, workdir=".")
        with self.assertRaises(ValueError):
            build_host_plan(
                {"selectedModel": "", "executionPlan": {"effort": "low"}},
                workdir=".",
            )

    def test_unknown_topology_is_blocked(self) -> None:
        plan = build_host_plan(
            _route(topology="nested", variant="X"),
            workdir=".",
            available_backends=["codex"],
            host_permissions=_permissions(),
        )
        self.assertEqual(plan["status"], "blocked")
        self.assertEqual(plan["blocked"]["code"], "host_topology_unsupported")


if __name__ == "__main__":
    unittest.main()
