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
TASK = "Implement the requested task"

from auto_router import route_case  # noqa: E402
from host_execution_plan import build_host_plan, detect_available_backends  # noqa: E402
from model_affinity import resolve_model_affinity, workspace_identity  # noqa: E402
from model_registry import load_model_registry, registry_digest  # noqa: E402
from route_contract import build_route_decision  # noqa: E402


def _route(
    *,
    task: str = TASK,
    model: str = "codex:gpt-5.6-luna",
    effort: str = "low",
    topology: str = "direct",
    variant: str = "F",
) -> dict:
    registry = load_model_registry()
    spec = registry.get(model, role="direct")
    maximum_calls = {"A": 2, "B": 6, "C": 7, "D": 5, "E": 1, "F": 1}.get(
        variant, 1
    )
    workspace_key = workspace_identity(".")
    affinity = resolve_model_affinity(
        (),
        workspace_key=workspace_key,
        strategy="balance",
        selector_model=model,
        target_tier=spec.tier,
        registry=registry,
        available_backends=registry.backends,
        required_capabilities=(),
        mode="off",
    )
    execution_plan = {
            "model": model,
            "requiredTier": spec.tier,
            "selectedTier": spec.tier,
            "effort": effort,
            "effortSource": "explicit",
            "topology": topology,
            "variant": variant,
            "variantSource": "policy",
            "orchestrationPolicy": "auto",
            "context": {},
            "modelAffinity": affinity,
            "roleModelPolicy": affinity["roleModelPolicy"],
            "orchestrationRecommendation": {
                "eligible": False,
                "recommended": False,
                "recommendedTopology": "direct",
                "recommendedVariant": variant,
                "estimatedMaximumModelCalls": maximum_calls,
                "requiresExplicitOptIn": False,
                "utility": {
                    "score": 0,
                    "minimumScore": 1,
                    "passes": False,
                    "benefitPoints": 0,
                    "overheadPoints": 0,
                    "estimatedAdditionalModelCalls": 0,
                    "estimatedRoleTierSwitches": 0,
                    "estimatedProfileTierSwitches": 0,
                    "roleModelPolicy": affinity["roleModelPolicy"],
                    "cacheSignalRatio": None,
                    "sessionBoundaryOverheadPoints": 0,
                    "billingCostEstimated": False,
                    "components": {},
                },
                "blockedByUtilityGate": False,
                "blockedByRiskGate": False,
                "highRiskConfirmationProvided": False,
                "reason": "insufficient-independent-parallel-scale",
            },
            "graderPolicy": "auto",
            "maxModelCalls": maximum_calls,
            "escalation": {
                "eligible": False,
                "nextTier": None,
                "requiresExplicitOptIn": True,
            },
    }
    return build_route_decision(
        route_id="route-test",
        task_text=task,
        strategy="balance",
        effort=effort,
        selected_model=model,
        selected_tier=spec.tier,
        selector_model=model,
        target_tier=spec.tier,
        reason_code="balance_default",
        feature_schema_version=2,
        features={},
        matched_signals={},
        repository_mode="off",
        repository_metadata={},
        execution_plan=execution_plan,
        policy_version="test",
        policy_digest="a" * 64,
        registry_digest=registry_digest(registry),
        workspace_key=workspace_key,
        model_affinity=affinity,
    )


def _permissions(sandbox: str = "workspace-write") -> dict:
    return {
        "schema": "agent-auto-router.host-permissions",
        "source": "test-host-turn",
        "sandbox": sandbox,
        "approvalPolicy": "never",
        "networkAccess": False,
        "writableRoots": [str(SCRIPTS.parents[2].resolve())] if sandbox == "workspace-write" else [],
        "canRequestPermissions": False,
    }


class HostExecutionPlanTests(unittest.TestCase):
    def test_automatic_execution_without_permission_snapshot_is_blocked(self) -> None:
        plan = build_host_plan(
            _route(), task_text=TASK, workdir=".", available_backends=["codex"]
        )
        self.assertEqual(plan["status"], "blocked")
        self.assertEqual(plan["blocked"]["code"], "host_permissions_required")

    def test_read_only_host_plan_has_no_writer(self) -> None:
        plan = build_host_plan(
            _route(),
            task_text=TASK,
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
            task_text=TASK,
            workdir=".",
            available_backends=["codex"],
            host_permissions=_permissions("danger-full-access"),
        )
        self.assertEqual(plan["status"], "blocked")
        self.assertEqual(plan["blocked"]["code"], "host_permissions_unrepresentable_by_cli")

    def test_direct_cli_action_is_host_neutral(self) -> None:
        plan = build_host_plan(
            _route(), task_text=TASK, workdir=".", available_backends=["codex"], host_permissions=_permissions()
        )
        self.assertEqual(plan["schema"], "agent-auto-router.host-plan")
        self.assertEqual(plan["executionBackend"], "host")
        self.assertEqual(plan["action"]["kind"], "cli")
        self.assertEqual(plan["action"]["backend"], "codex")
        self.assertEqual(plan["agent"]["role"], "direct")
        self.assertEqual(plan["agent"]["taskSource"], "host-current-user-task")
        self.assertNotIn("command", plan["action"])

    def test_direct_falls_back_to_explicit_approximate_host_execution(self) -> None:
        plan = build_host_plan(
            _route(), task_text=TASK, workdir=".", available_backends=["claude"], host_permissions=_permissions()
        )
        self.assertEqual(plan["status"], "ready")
        self.assertEqual(plan["action"]["kind"], "host_execute")
        self.assertEqual(plan["hostContract"]["modelAccuracy"], "approximate")
        self.assertFalse(plan["hostContract"]["silentModelOrProviderFallback"])

    def test_orchestration_action_has_executable_lowercase_argv(self) -> None:
        route = _route(
            model="claude:sonnet", effort="high", topology="orchestrated", variant="D"
        )
        plan = build_host_plan(route, task_text=TASK, workdir=".", available_backends=["claude"], host_permissions=_permissions())
        self.assertEqual(plan["action"]["kind"], "orchestrate")
        self.assertEqual(plan["action"]["backend"], "claude")
        self.assertEqual(plan["action"]["entrypoint"], "invoke_orchestrated_task.py")
        self.assertIn("--execution-envelope-stdin", plan["action"]["argv"])
        self.assertIn("--variant", plan["action"]["argv"])
        self.assertNotIn("--route-decision-json", plan["action"]["argv"])
        self.assertNotIn("--host-permissions-json", plan["action"]["argv"])
        self.assertNotIn("--model-affinity", plan["action"]["argv"])
        self.assertLess(sum(len(item) + 3 for item in plan["action"]["argv"]), 4096)
        template = plan["action"]["stdinTemplate"]
        locked = template["routeDecision"]
        self.assertEqual(locked["selectedModel"], "claude:sonnet")
        self.assertEqual(locked["strategy"], "balance")
        self.assertEqual(locked["effort"], "high")
        self.assertEqual(template["task"], {"source": "host-current-user-task"})
        self.assertNotIn("--Variant", plan["action"]["argv"])
        self.assertIn("--workdir", plan["action"]["argv"])
        forwarded = template["hostPermissions"]
        self.assertEqual(forwarded["schema"], "agent-auto-router.host-permissions")
        self.assertEqual(forwarded["sandbox"], "workspace-write")
        self.assertNotIn(TASK, json.dumps(plan))

    def test_host_plan_rejects_route_bound_to_another_task(self) -> None:
        with self.assertRaisesRegex(ValueError, "task binding does not match"):
            build_host_plan(
                _route(),
                task_text="Delete production authentication data",
                workdir=".",
                available_backends=["codex"],
                host_permissions=_permissions("read-only"),
            )

    def test_orchestration_stdin_template_roundtrips_to_locked_dry_run(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            workdir = root / "workspace"
            state_dir = root / "state"
            workdir.mkdir()
            state_dir.mkdir()
            route = route_case(
                {
                    "id": "host-roundtrip",
                    "prompt": TASK,
                    "workspace_key": workspace_identity(workdir),
                },
                explicit_variant="D",
                model_affinity_mode="off",
            )["routeDecision"]
            plan = build_host_plan(
                route,
                task_text=TASK,
                workdir=workdir,
                available_backends=["codex"],
                host_permissions=_permissions("read-only"),
            )
            envelope = copy.deepcopy(plan["action"]["stdinTemplate"])
            envelope["task"] = TASK
            completed = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS / plan["action"]["entrypoint"]),
                    *plan["action"]["argv"],
                    "--dry-run",
                    "--state-dir", str(state_dir),
                    "--repository-context", "off",
                ],
                input=json.dumps(envelope),
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=True,
            )
        payload = json.loads(completed.stdout)
        self.assertEqual(payload["modelCalls"], 0)
        self.assertEqual(
            payload["routing"]["routeDecision"]["routeId"], "host-roundtrip"
        )

    def test_orchestration_contract_has_no_direct_agent(self) -> None:
        route = _route(topology="orchestrated", variant="D")
        plan = build_host_plan(route, task_text=TASK, workdir=".", available_backends=["codex"], host_permissions=_permissions())
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
        plan = build_host_plan(route, task_text=TASK, workdir=".", available_backends=["codex"], host_permissions=_permissions())
        self.assertEqual(plan["status"], "blocked")
        self.assertEqual(plan["blocked"]["code"], "host_selected_backend_unavailable")
        self.assertEqual(plan["plannedCalls"], 0)

    def test_unknown_programmatic_backend_is_blocked(self) -> None:
        plan = build_host_plan(
            _route(), task_text=TASK, workdir=".", available_backends=["unknown"], host_permissions=_permissions()
        )
        self.assertEqual(plan["status"], "blocked")
        self.assertEqual(plan["blocked"]["code"], "host_unknown_backend")

    def test_dry_run_has_no_writer_or_planned_call(self) -> None:
        plan = build_host_plan(
            _route(), task_text=TASK, workdir=".", available_backends=["codex"], host_permissions=_permissions(), dry_run=True
        )
        self.assertFalse(plan["executionRequested"])
        self.assertEqual(plan["plannedCalls"], 0)
        self.assertFalse(plan["agent"]["writer"])
        self.assertEqual(plan["action"]["kind"], "report_plan")

    def test_privacy_fields_reject_task_content(self) -> None:
        route = _route()
        route["task"] = "private task"
        with self.assertRaisesRegex(ValueError, "field: task"):
            build_host_plan(
                route,
                task_text=TASK,
                workdir=".",
                available_backends=["codex"],
                host_permissions=_permissions(),
            )

    def test_route_workspace_must_match_execution_workdir(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(ValueError, "workspaceKey does not match"):
                build_host_plan(
                    _route(),
                    task_text=TASK,
                    workdir=temporary,
                    available_backends=["codex"],
                    host_permissions=_permissions("read-only"),
                )

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
                "--workdir",
                str(repository),
                "--repository-context",
                "off",
            ],
            input="Implement a routine change",
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        self.assertEqual(generated.returncode, 0, generated.stderr)
        selected = json.loads(generated.stdout)
        request = {
            "schema": "agent-auto-router.host-request",
            "task": "Implement a routine change",
            "routeDecision": selected["routeDecision"],
        }
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
            input=json.dumps(request),
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
        self.assertNotIn("Implement a routine change", completed.stdout)

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
            build_host_plan({}, task_text=TASK, workdir=".")
        with self.assertRaises(ValueError):
            build_host_plan(
                {"selectedModel": "", "executionPlan": {"effort": "low"}},
                task_text=TASK,
                workdir=".",
            )

    def test_unknown_topology_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "variant is invalid"):
            build_host_plan(
                _route(topology="nested", variant="X"),
                task_text=TASK,
                workdir=".",
                available_backends=["codex"],
                host_permissions=_permissions(),
            )


if __name__ == "__main__":
    unittest.main()
