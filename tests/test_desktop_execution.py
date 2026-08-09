from __future__ import annotations

import inspect
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

SCRIPTS = pathlib.Path(__file__).resolve().parents[1] / "skills" / "agent-auto-router" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import desktop_execution  # noqa: E402
from desktop_execution import build_desktop_plan  # noqa: E402
from execution_plan import build_execution_plan  # noqa: E402
from routing_policy import select_model  # noqa: E402


def route_for(task: str, *, criteria: list[str] | None = None) -> dict[str, object]:
    decision = select_model(task, "balance", acceptance_criteria=criteria or [])
    return {
        "routeId": "route-test",
        "selectedModel": decision.model,
        "executionPlan": build_execution_plan(decision),
    }


def _bare(model: str) -> str:
    return model.split(":", 1)[1] if ":" in model else model


def host_permissions(sandbox: str = "workspace-write") -> dict[str, object]:
    return {
        "schema": "agent-auto-router.host-permissions.v1",
        "source": "test-desktop-turn",
        "sandbox": sandbox,
        "approvalPolicy": "never",
        "networkAccess": False,
        "writableRoots": [str(SCRIPTS.parents[2].resolve())] if sandbox == "workspace-write" else [],
        "canRequestPermissions": False,
    }


class DesktopExecutionTests(unittest.TestCase):
    def test_automatic_execution_requires_trusted_host_permissions(self) -> None:
        route = route_for("Implement a routine change")
        plan = build_desktop_plan(
            route, [_bare(route["selectedModel"])], workdir=SCRIPTS.parents[2]
        )
        self.assertEqual(plan["status"], "blocked")
        self.assertEqual(plan["blocked"]["code"], "desktop_host_permissions_required")

    def test_read_only_host_cannot_produce_a_writer(self) -> None:
        route = route_for("Review a routine change")
        plan = build_desktop_plan(
            route,
            [_bare(route["selectedModel"])],
            host_permissions=host_permissions("read-only"),
            workdir=SCRIPTS.parents[2],
        )
        self.assertEqual(plan["status"], "ready")
        self.assertFalse(plan["agent"]["writer"])
        self.assertIsNone(plan["hostContract"]["onlyWriter"])

    def test_direct_plan_is_single_writer_and_privacy_safe(self) -> None:
        route = route_for("Implement a routine change")
        route["task"] = "private task"
        plan = build_desktop_plan(
            route, [_bare(route["selectedModel"])], host_permissions=host_permissions(), workdir=SCRIPTS.parents[2]
        )
        self.assertEqual(plan["schema"], "agent-auto-router.desktop-plan.v2")
        self.assertEqual(plan["status"], "ready")
        self.assertEqual(plan["hostContract"]["action"], "spawn_agent")
        self.assertEqual(plan["hostContract"]["maxAgents"], 1)
        self.assertEqual(plan["hostContract"]["onlyWriter"], "direct")
        self.assertEqual(plan["modelCalls"], 0)
        self.assertEqual(plan["modelCallsScope"], "routing")
        self.assertEqual(plan["routingModelCalls"], 0)
        self.assertTrue(plan["executionRequested"])
        self.assertEqual(plan["plannedAgentCalls"], 1)
        self.assertEqual(plan["privacy"]["semantics"], "planner-guarantees")
        self.assertEqual(plan["context"]["profile"], "standard")
        self.assertEqual(plan["agent"]["model"], route["selectedModel"])
        self.assertEqual(plan["agent"]["forkTurns"], "none")
        self.assertEqual(
            pathlib.Path(plan["agent"]["workdir"]), SCRIPTS.parents[2].resolve()
        )
        self.assertFalse(plan["hostContract"]["fullHistoryForkAllowed"])
        self.assertFalse(plan["privacy"]["taskIncludedInPlan"])
        self.assertFalse(plan["privacy"]["credentialsRead"])
        self.assertNotIn("private task", json.dumps(plan))

    def test_selected_model_unavailable_is_explicitly_blocked(self) -> None:
        route = route_for("Implement a routine change")
        plan = build_desktop_plan(
            route, ["gpt-other"], host_permissions=host_permissions(), workdir=SCRIPTS.parents[2]
        )
        self.assertEqual(plan["status"], "blocked")
        self.assertEqual(plan["blocked"]["code"], "desktop_model_unavailable")
        self.assertEqual(plan["plannedAgentCalls"], 0)
        self.assertIsNone(plan["agent"])

    def test_foreign_backend_model_is_blocked_in_desktop(self) -> None:
        route = {
            "routeId": "route-test",
            "selectedModel": "claude:sonnet",
            "executionPlan": build_execution_plan(
                select_model("Implement a routine change", "balance")
            ),
        }
        plan = build_desktop_plan(
            route, ["sonnet"], host_permissions=host_permissions(), workdir=SCRIPTS.parents[2]
        )
        self.assertEqual(plan["status"], "blocked")
        self.assertEqual(plan["blocked"]["code"], "desktop_backend_unsupported")
        self.assertEqual(plan["plannedAgentCalls"], 0)
        self.assertIsNone(plan["agent"])

    def test_multi_role_route_is_explicitly_blocked(self) -> None:
        route = route_for(
            "Implement API and tests for several independent components",
            criteria=["API", "tests", "docs", "rollback"],
        )
        self.assertEqual(route["executionPlan"]["topology"], "orchestrated")
        plan = build_desktop_plan(
            route, [_bare(route["selectedModel"])], host_permissions=host_permissions(), workdir=SCRIPTS.parents[2]
        )
        self.assertEqual(plan["status"], "blocked")
        self.assertEqual(
            plan["blocked"]["code"], "desktop_multi_role_topology_unsupported"
        )
        self.assertEqual(plan["plannedAgentCalls"], 0)

    def test_danger_full_access_is_inherited_without_router_elevation(self) -> None:
        route = route_for("Implement a routine change")
        plan = build_desktop_plan(
            route,
            [_bare(route["selectedModel"])],
            workdir=SCRIPTS.parents[2],
            host_permissions=host_permissions("danger-full-access"),
        )
        self.assertEqual(plan["status"], "ready")
        self.assertEqual(plan["hostContract"]["permissions"]["effectiveSandbox"], "danger-full-access")
        self.assertTrue(plan["hostContract"]["permissions"]["noPrivilegeEscalation"])

    def test_desktop_planner_has_no_cli_or_process_execution_path(self) -> None:
        source = inspect.getsource(desktop_execution).lower()
        self.assertNotIn("subprocess", source)
        self.assertNotIn("codex exec", source)
        self.assertNotIn("app-server", source)
        self.assertNotIn("codex_ca_certificate", source)

    def test_powershell_backend_branch_does_not_require_cli_for_desktop(self) -> None:
        script = (SCRIPTS / "invoke_auto_task.ps1").read_text(encoding="utf-8")
        self.assertIn("[ValidateSet('cli', 'desktop')]", script)
        self.assertIn("[string[]]$DesktopAvailableModels", script)
        self.assertIn("desktop_execution.py", script)
        self.assertIn("$ExecutionBackend -eq 'cli'", script)
        desktop_branch = script.split("if ($ExecutionBackend -eq 'desktop')", 1)[1]
        desktop_branch = desktop_branch.split("$runnerResultPath", 1)[0]
        self.assertNotIn("single_task_runner.py", desktop_branch)
        self.assertNotIn("codex exec", desktop_branch.lower())

    def test_desktop_backend_executes_with_a_failing_codex_shim_untouched(self) -> None:
        powershell = shutil.which("pwsh") or shutil.which("powershell")
        self.assertIsNotNone(powershell)
        script = SCRIPTS / "invoke_auto_task.ps1"
        repository = SCRIPTS.parents[2].resolve()
        with tempfile.TemporaryDirectory() as temp:
            temp_path = pathlib.Path(temp)
            marker = temp_path / "codex-invoked.txt"
            shim = temp_path / "codex.cmd"
            shim.write_text(
                '@echo off\r\necho invoked>"%CODEX_SHIM_MARKER%"\r\nexit /b 99\r\n',
                encoding="utf-8",
            )
            environment = os.environ.copy()
            environment["CODEX_SHIM_MARKER"] = str(marker)
            environment["PATH"] = f"{temp_path}{os.pathsep}{environment['PATH']}"
            command = (
                f"& '{script}' -Task 'Implement a routine change' "
                "-ExecutionBackend desktop "
                "-DesktopAvailableModels @('gpt-5.6-sol','gpt-5.6-terra') "
                f"-Workdir '{repository}' -HostPermissionsJson '{json.dumps(host_permissions())}' -NoFeedback"
            )
            completed = subprocess.run(
                [
                    powershell,
                    "-NoProfile",
                    "-NonInteractive",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-Command",
                    command,
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=environment,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            plan = json.loads(completed.stdout.strip())
            self.assertEqual(plan["status"], "ready")
            self.assertEqual(plan["agent"]["forkTurns"], "none")
            self.assertEqual(pathlib.Path(plan["agent"]["workdir"]), repository)
            self.assertFalse(marker.exists(), "Desktop backend invoked the Codex CLI shim")

    def test_desktop_dry_run_explain_and_json_emit_one_plan(self) -> None:
        powershell = shutil.which("pwsh") or shutil.which("powershell")
        self.assertIsNotNone(powershell)
        script = SCRIPTS / "invoke_auto_task.ps1"
        repository = SCRIPTS.parents[2].resolve()
        command = (
            f"& '{script}' -Task 'Implement a routine change' "
            "-ExecutionBackend desktop -DryRun -Explain -Json -NoFeedback "
            "-DesktopAvailableModels @('gpt-5.6-sol','gpt-5.6-terra') "
            f"-Workdir '{repository}' -HostPermissionsJson '{json.dumps(host_permissions())}'"
        )
        completed = subprocess.run(
            [powershell, "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        plan = json.loads(completed.stdout.strip())
        self.assertEqual(plan["schema"], "agent-auto-router.desktop-plan.v2")
        self.assertEqual(plan["status"], "ready")
        self.assertFalse(plan["executionRequested"])
        self.assertEqual(plan["plannedAgentCalls"], 0)
        self.assertFalse(plan["agent"]["writer"])
        self.assertTrue(plan["agent"]["wouldWrite"])
        self.assertEqual(plan["hostContract"]["action"], "report_plan")
        self.assertEqual(plan["hostContract"]["maxAgents"], 0)

    def test_desktop_rejects_cli_only_parameter_values(self) -> None:
        powershell = shutil.which("pwsh") or shutil.which("powershell")
        self.assertIsNotNone(powershell)
        script = SCRIPTS / "invoke_auto_task.ps1"
        repository = SCRIPTS.parents[2].resolve()
        for extra, expected in (
            ("-ContextMode full", "does not consume CLI ContextMode"),
            ("-FeedbackFile feedback.jsonl", "FeedbackFile is CLI-only"),
        ):
            with self.subTest(extra=extra):
                command = (
                    f"& '{script}' -Task 'Implement a routine change' "
                    "-ExecutionBackend desktop "
                    "-DesktopAvailableModels @('gpt-5.6-sol','gpt-5.6-terra') "
                    f"-Workdir '{repository}' {extra}"
                )
                completed = subprocess.run(
                    [powershell, "-NoProfile", "-NonInteractive", "-Command", command],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    check=False,
                )
                self.assertNotEqual(completed.returncode, 0)
                self.assertIn(expected, completed.stderr)

    def test_cli_backend_remains_available(self) -> None:
        script = (SCRIPTS / "invoke_auto_task.ps1").read_text(encoding="utf-8")
        self.assertIn("single_task_runner.py", script)
        self.assertIn("--available-backends', 'codex", script)
        runner = (SCRIPTS / "single_task_runner.py").read_text(encoding="utf-8")
        self.assertIn('[*codex_command, "exec", "--ephemeral"]', runner)
        self.assertIn('strip_backend_prefix(args.model, "codex")', runner)

    def test_cli_backend_rejects_foreign_explicit_model_before_launch(self) -> None:
        powershell = shutil.which("pwsh") or shutil.which("powershell")
        self.assertIsNotNone(powershell)
        script = SCRIPTS / "invoke_auto_task.ps1"
        repository = SCRIPTS.parents[2].resolve()
        command = (
            f"& '{script}' -Task 'Reply with exactly OK' "
            "-ExecutionBackend cli -Model claude:sonnet -Sandbox read-only -DryRun "
            f"-Workdir '{repository}' -HostPermissionsJson '{json.dumps(host_permissions())}'"
        )
        completed = subprocess.run(
            [powershell, "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("not available on the requested backends", completed.stderr)


if __name__ == "__main__":
    unittest.main()
