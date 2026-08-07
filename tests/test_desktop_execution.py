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

SCRIPTS = pathlib.Path(__file__).resolve().parents[1] / "skills" / "codex-auto-router" / "scripts"
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


class DesktopExecutionTests(unittest.TestCase):
    def test_direct_plan_is_single_writer_and_privacy_safe(self) -> None:
        route = route_for("Implement a routine change")
        route["task"] = "private task"
        plan = build_desktop_plan(
            route, [route["selectedModel"]], workdir=SCRIPTS.parents[2]
        )
        self.assertEqual(plan["schema"], "codex-auto-router.desktop-plan.v1")
        self.assertEqual(plan["status"], "ready")
        self.assertEqual(plan["hostContract"]["action"], "spawn_agent")
        self.assertEqual(plan["hostContract"]["maxAgents"], 1)
        self.assertEqual(plan["hostContract"]["onlyWriter"], "direct")
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
            route, ["gpt-other"], workdir=SCRIPTS.parents[2]
        )
        self.assertEqual(plan["status"], "blocked")
        self.assertEqual(plan["blocked"]["code"], "desktop_model_unavailable")
        self.assertIsNone(plan["agent"])

    def test_multi_role_route_is_explicitly_blocked(self) -> None:
        route = route_for(
            "Implement API and tests for several independent components",
            criteria=["API", "tests", "docs", "rollback"],
        )
        self.assertEqual(route["executionPlan"]["topology"], "orchestrated")
        plan = build_desktop_plan(
            route, [route["selectedModel"]], workdir=SCRIPTS.parents[2]
        )
        self.assertEqual(plan["status"], "blocked")
        self.assertEqual(
            plan["blocked"]["code"], "desktop_multi_role_topology_unsupported"
        )

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
                f"-Workdir '{repository}' -NoFeedback"
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

    def test_cli_backend_remains_available(self) -> None:
        script = (SCRIPTS / "invoke_auto_task.ps1").read_text(encoding="utf-8")
        self.assertIn("single_task_runner.py", script)
        runner = (SCRIPTS / "single_task_runner.py").read_text(encoding="utf-8")
        self.assertIn('[*resolve_codex_command(), "exec", "--ephemeral"]', runner)


if __name__ == "__main__":
    unittest.main()
