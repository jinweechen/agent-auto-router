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
from dataclasses import asdict

SCRIPTS = pathlib.Path(__file__).resolve().parents[1] / "skills" / "agent-auto-router" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import desktop_execution  # noqa: E402
from desktop_execution import build_desktop_plan  # noqa: E402
from execution_plan import build_execution_plan  # noqa: E402
from model_registry import load_model_registry, registry_digest  # noqa: E402
from routing_policy import select_model  # noqa: E402


def route_for(task: str, *, criteria: list[str] | None = None) -> dict[str, object]:
    decision = select_model(task, "balance", acceptance_criteria=criteria or [])
    return {
        "routeId": "route-test",
        "decision": asdict(decision),
        "selectedModel": decision.model,
        "executionPlan": build_execution_plan(decision),
        "policy": {
            "version": decision.policy_version,
            "digest": decision.policy_digest,
        },
        "registry": {"digest": registry_digest(load_model_registry())},
    }


def _bare(model: str) -> str:
    return model.split(":", 1)[1] if ":" in model else model


def all_desktop_models() -> list[str]:
    return ["gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"]


def agent_for(plan: dict[str, object], role: str) -> dict[str, object]:
    return next(agent for agent in plan["agents"] if agent["role"] == role)


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
            route,
            [_bare(route["selectedModel"])],
            workdir=SCRIPTS.parents[2],
            host_permissions=None,
            max_parallel_children=3,
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
            max_parallel_children=3,
        )
        self.assertEqual(plan["status"], "ready")
        self.assertFalse(agent_for(plan, "direct")["writer"])
        self.assertIsNone(plan["hostContract"]["onlyWriter"])

    def test_direct_plan_is_single_writer_and_privacy_safe(self) -> None:
        route = route_for("Implement a routine change")
        route["task"] = "private task"
        plan = build_desktop_plan(
            route,
            [_bare(route["selectedModel"])],
            host_permissions=host_permissions(),
            workdir=SCRIPTS.parents[2],
            max_parallel_children=3,
        )
        self.assertEqual(plan["schema"], "agent-auto-router.desktop-plan.v3")
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
        direct = agent_for(plan, "direct")
        self.assertEqual(direct["model"], route["selectedModel"])
        self.assertEqual(direct["forkTurns"], "none")
        self.assertEqual(
            pathlib.Path(direct["workdir"]), SCRIPTS.parents[2].resolve()
        )
        self.assertEqual(direct["idempotencyKeyTemplate"], "route-test:direct:{instance}")
        self.assertEqual(plan["coordination"]["writerClaim"]["ownerRole"], "direct")
        self.assertFalse(plan["hostContract"]["fullHistoryForkAllowed"])
        self.assertFalse(plan["privacy"]["taskIncludedInPlan"])
        self.assertFalse(plan["privacy"]["credentialsRead"])
        self.assertEqual(
            plan["learning"]["reportSchema"],
            "agent-auto-router.execution-report.v1",
        )
        self.assertTrue(plan["learning"]["submitAfterExecution"])
        self.assertEqual(plan["learning"]["route"]["selectorModel"], route["decision"]["model"])
        self.assertEqual(plan["learning"]["route"]["selectedModel"], direct["model"])
        self.assertEqual(plan["learning"]["route"]["policyDigest"], route["policy"]["digest"])
        self.assertNotIn("private task", json.dumps(plan))

    def test_selected_model_unavailable_is_explicitly_blocked(self) -> None:
        route = route_for("Implement a routine change")
        plan = build_desktop_plan(
            route,
            ["gpt-other"],
            host_permissions=host_permissions(),
            workdir=SCRIPTS.parents[2],
            max_parallel_children=3,
        )
        self.assertEqual(plan["status"], "blocked")
        self.assertEqual(plan["blocked"]["code"], "desktop_model_unavailable")
        self.assertEqual(plan["plannedAgentCalls"], 0)
        self.assertEqual(plan["agents"], [])

    def test_missing_or_stale_registry_digest_blocks_before_spawn(self) -> None:
        for digest, expected in (
            (None, "desktop_registry_digest_required"),
            ("stale-digest", "desktop_registry_changed"),
        ):
            with self.subTest(digest=digest):
                route = route_for("Implement a routine change")
                route["registry"] = {} if digest is None else {"digest": digest}
                plan = build_desktop_plan(
                    route,
                    [_bare(route["selectedModel"])],
                    host_permissions=host_permissions(),
                    workdir=SCRIPTS.parents[2],
                    max_parallel_children=3,
                )
                self.assertEqual(plan["status"], "blocked")
                self.assertEqual(plan["blocked"]["code"], expected)
                self.assertEqual(plan["plannedAgentCalls"], 0)

    def test_foreign_backend_model_is_blocked_in_desktop(self) -> None:
        route = {
            "routeId": "route-test",
            "selectedModel": "claude:sonnet",
            "executionPlan": build_execution_plan(
                select_model("Implement a routine change", "balance")
            ),
            "registry": {"digest": registry_digest(load_model_registry())},
        }
        plan = build_desktop_plan(
            route,
            ["sonnet"],
            host_permissions=host_permissions(),
            workdir=SCRIPTS.parents[2],
            max_parallel_children=3,
        )
        self.assertEqual(plan["status"], "blocked")
        self.assertEqual(plan["blocked"]["code"], "desktop_backend_unsupported")
        self.assertEqual(plan["plannedAgentCalls"], 0)
        self.assertEqual(plan["agents"], [])

    def test_multi_role_route_emits_staged_single_writer_plan(self) -> None:
        route = route_for(
            "Implement API and tests for several independent components",
            criteria=["API", "tests", "docs", "rollback"],
        )
        self.assertEqual(route["executionPlan"]["topology"], "orchestrated")
        plan = build_desktop_plan(
            route,
            all_desktop_models(),
            host_permissions=host_permissions(),
            workdir=SCRIPTS.parents[2],
            max_parallel_children=3,
        )
        self.assertEqual(plan["status"], "ready")
        self.assertEqual(plan["schema"], "agent-auto-router.desktop-plan.v3")
        self.assertEqual(plan["hostContract"]["action"], "spawn_agents")
        self.assertEqual(plan["hostContract"]["onlyWriter"], "reviewer")
        self.assertEqual(plan["hostContract"]["maxParallelAgents"], 2)
        self.assertEqual([stage["id"] for stage in plan["stages"]], ["planner", "worker", "reviewer"])
        self.assertEqual(agent_for(plan, "worker")["maximumInstances"], 2)
        self.assertTrue(agent_for(plan, "reviewer")["writer"])
        self.assertEqual(
            sum(1 for agent in plan["agents"] if agent["writer"]),
            1,
        )
        self.assertLessEqual(plan["plannedAgentCalls"], plan["callBudget"]["maximum"])

    def test_multi_role_route_explicitly_upgrades_unavailable_worker_model(self) -> None:
        route = route_for(
            "Implement API and tests for several independent components",
            criteria=["API", "tests", "docs", "rollback"],
        )
        plan = build_desktop_plan(
            route,
            ["gpt-5.6-sol", "gpt-5.6-terra"],
            host_permissions=host_permissions(),
            workdir=SCRIPTS.parents[2],
            max_parallel_children=3,
        )
        self.assertEqual(plan["status"], "ready")
        worker = agent_for(plan, "worker")
        self.assertEqual(worker["preferredModel"], "codex:gpt-5.6-luna")
        self.assertEqual(worker["model"], "codex:gpt-5.6-terra")
        self.assertEqual(worker["modelResolution"], "runtime-tier-upgrade")

    def test_runtime_capacity_bounds_parallel_workers(self) -> None:
        route = route_for(
            "Implement API and tests for several independent components",
            criteria=["API", "tests", "docs", "rollback"],
        )
        plan = build_desktop_plan(
            route,
            all_desktop_models(),
            host_permissions=host_permissions(),
            workdir=SCRIPTS.parents[2],
            max_parallel_children=1,
        )
        self.assertEqual(plan["status"], "ready")
        self.assertEqual(agent_for(plan, "worker")["maximumInstances"], 1)
        self.assertEqual(plan["hostContract"]["maxParallelAgents"], 1)

    def test_variant_c_declares_dependencies_budget_and_exclusive_writer_claim(self) -> None:
        route = route_for(
            "Implement several independent authentication components",
            criteria=["API", "tests", "migration", "rollback"],
        )
        route["selectedModel"] = "codex:gpt-5.6-sol"
        route["executionPlan"].update({
            "topology": "orchestrated",
            "variant": "C",
            "maxModelCalls": 7,
        })
        route["decision"]["required_capabilities"] = ("high-risk-primary",)
        route["decision"]["high_risk"] = True
        plan = build_desktop_plan(
            route,
            all_desktop_models(),
            host_permissions=host_permissions(),
            workdir=SCRIPTS.parents[2],
            max_parallel_children=3,
        )
        self.assertEqual(plan["status"], "ready")
        self.assertEqual(
            [stage["id"] for stage in plan["stages"]],
            ["planner", "dispatcher", "worker", "reviewer", "grader"],
        )
        self.assertEqual(agent_for(plan, "worker")["dependsOn"], ["dispatcher"])
        self.assertEqual(agent_for(plan, "reviewer")["model"], "codex:gpt-5.6-sol")
        self.assertEqual(plan["wouldPlanAgentCalls"], {"minimum": 5, "maximum": 7})
        self.assertEqual(plan["plannedAgentCalls"], 7)
        self.assertEqual(plan["coordination"]["writerClaim"]["mode"], "exclusive")
        self.assertEqual(plan["coordination"]["writerClaim"]["ownerRole"], "reviewer")
        self.assertIn("blocked", plan["coordination"]["runStateMachine"])

    def test_agent_call_budget_blocks_an_impossible_staged_plan(self) -> None:
        route = route_for("Implement a routine change")
        route["selectedModel"] = "codex:gpt-5.6-sol"
        route["executionPlan"].update({
            "topology": "orchestrated",
            "variant": "C",
            "maxModelCalls": 4,
        })
        plan = build_desktop_plan(
            route,
            all_desktop_models(),
            host_permissions=host_permissions(),
            workdir=SCRIPTS.parents[2],
            max_parallel_children=3,
        )
        self.assertEqual(plan["status"], "blocked")
        self.assertEqual(plan["blocked"]["code"], "desktop_agent_call_budget_insufficient")

    def test_danger_full_access_is_inherited_without_router_elevation(self) -> None:
        route = route_for("Implement a routine change")
        plan = build_desktop_plan(
            route,
            [_bare(route["selectedModel"])],
            workdir=SCRIPTS.parents[2],
            host_permissions=host_permissions("danger-full-access"),
            max_parallel_children=3,
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
        self.assertIn("[int]$DesktopMaxParallelChildren", script)
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
                "-DesktopMaxParallelChildren 3 "
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
            direct = agent_for(plan, "direct")
            self.assertEqual(direct["forkTurns"], "none")
            self.assertEqual(pathlib.Path(direct["workdir"]), repository)
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
            "-DesktopMaxParallelChildren 3 "
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
        self.assertEqual(plan["schema"], "agent-auto-router.desktop-plan.v3")
        self.assertEqual(plan["status"], "ready")
        self.assertFalse(plan["executionRequested"])
        self.assertEqual(plan["plannedAgentCalls"], 0)
        self.assertFalse(agent_for(plan, "direct")["writer"])
        self.assertTrue(agent_for(plan, "direct")["wouldWrite"])
        self.assertEqual(plan["hostContract"]["action"], "report_plan")
        self.assertEqual(plan["hostContract"]["maxAgents"], 0)

    def test_desktop_entrypoint_routes_parallel_task_to_multi_agent_plan(self) -> None:
        powershell = shutil.which("pwsh") or shutil.which("powershell")
        self.assertIsNotNone(powershell)
        script = SCRIPTS / "invoke_auto_task.ps1"
        repository = SCRIPTS.parents[2].resolve()
        task = (
            "In parallel, refactor the architecture across multiple modules and "
            "independent workstreams, including API and tests."
        )
        command = (
            f"& '{script}' -Task '{task}' "
            "-ExecutionBackend desktop -DryRun -Json -NoFeedback "
            "-DesktopAvailableModels @('gpt-5.6-sol','gpt-5.6-terra','gpt-5.6-luna') "
            "-DesktopMaxParallelChildren 3 "
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
        self.assertEqual(plan["topology"], "orchestrated")
        self.assertEqual(plan["variant"], "B")
        self.assertEqual(
            [agent["role"] for agent in plan["agents"]],
            ["planner", "worker", "reviewer", "grader"],
        )
        self.assertEqual(plan["plannedAgentCalls"], 0)
        self.assertEqual(plan["wouldPlanAgentCalls"], {"minimum": 4, "maximum": 6})
        self.assertEqual(agent_for(plan, "worker")["maximumInstances"], 3)

    def test_desktop_entrypoint_routes_chinese_parallel_signals_to_multi_agent_plan(self) -> None:
        powershell = shutil.which("pwsh") or shutil.which("powershell")
        self.assertIsNotNone(powershell)
        script = SCRIPTS / "invoke_auto_task.ps1"
        repository = SCRIPTS.parents[2].resolve()
        task = "并行审查多个独立模块，覆盖调试、长上下文和多文件任务，最后统一审查"
        command = (
            f"& '{script}' -Task '{task}' "
            "-ExecutionBackend desktop -DryRun -Json -NoFeedback "
            "-DesktopAvailableModels @('gpt-5.6-sol','gpt-5.6-terra','gpt-5.6-luna') "
            "-DesktopMaxParallelChildren 3 "
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
        self.assertEqual(plan["status"], "ready")
        self.assertEqual(plan["topology"], "orchestrated")
        self.assertEqual(plan["variant"], "D")
        self.assertEqual(
            [agent["role"] for agent in plan["agents"]],
            ["planner", "worker", "reviewer"],
        )
        self.assertNotIn(task, completed.stdout)

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
                    "-DesktopMaxParallelChildren 3 "
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

    def test_desktop_requires_runtime_parallel_capacity(self) -> None:
        powershell = shutil.which("pwsh") or shutil.which("powershell")
        self.assertIsNotNone(powershell)
        script = SCRIPTS / "invoke_auto_task.ps1"
        repository = SCRIPTS.parents[2].resolve()
        command = (
            f"& '{script}' -Task 'Implement a routine change' "
            "-ExecutionBackend desktop "
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
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("DesktopMaxParallelChildren", completed.stderr)

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
