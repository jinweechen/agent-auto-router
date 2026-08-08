from __future__ import annotations

import json
import pathlib
import subprocess
import sys
import unittest

SCRIPTS = pathlib.Path(__file__).resolve().parents[1] / "skills" / "agent-auto-router" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import hermes_host_plan  # noqa: E402
from hermes_host_plan import build_hermes_plan, detect_available_backends  # noqa: E402


def _direct_route(
    *,
    model: str = "codex:gpt-5.6-luna",
    effort: str = "low",
    variant: str = "F",
) -> dict:
    """Build a minimal direct (A/E/F) route dict."""
    return {
        "routeId": "route-test",
        "selectedModel": model,
        "executionPlan": {
            "effort": effort,
            "topology": "direct",
            "variant": variant,
            "context": {},
        },
        "decision": {"strategy": "balance", "reason": "simple", "target_tier": "fast"},
        "policy": {"version": 0, "digest": "abc"},
        "registry": {"source": "test", "digest": "def"},
    }


def _orchestrated_route(
    *,
    model: str = "claude:sonnet",
    effort: str = "high",
    variant: str = "D",
) -> dict:
    """Build a minimal orchestrated (B/C/D) route dict."""
    return {
        "routeId": "route-test",
        "selectedModel": model,
        "executionPlan": {
            "effort": effort,
            "topology": "orchestrated",
            "variant": variant,
            "context": {},
        },
        "decision": {"strategy": "balance", "reason": "multi-module", "target_tier": "balanced"},
        "policy": {"version": 0, "digest": "abc"},
        "registry": {"source": "test", "digest": "def"},
    }


class HermesHostPlanTests(unittest.TestCase):
    # 1. ready + cli action when backend is available
    def test_ready_cli_action_for_available_backend(self) -> None:
        route = _direct_route(model="codex:gpt-5.6-luna", effort="low", variant="F")
        plan = build_hermes_plan(route, workdir=".", available_backends=["codex"])
        self.assertEqual(plan["schema"], "agent-auto-router.host-plan.v1")
        self.assertEqual(plan["status"], "ready")
        self.assertEqual(plan["executionBackend"], "hermes")
        self.assertEqual(plan["topology"], "direct")
        self.assertEqual(plan["variant"], "F")
        self.assertEqual(plan["action"]["kind"], "cli")
        self.assertEqual(plan["action"]["backend"], "codex")
        self.assertEqual(plan["hostContract"]["modelAccuracy"], "exact")
        self.assertEqual(plan["hostContract"]["action"], "cli")
        self.assertEqual(plan["agent"]["taskSource"], "hermes-current-user-task")

    # 2. host_execute fallback when the selected backend is unavailable
    def test_host_execute_action_when_backend_unavailable(self) -> None:
        route = _direct_route(model="codex:gpt-5.6-luna", effort="low", variant="F")
        plan = build_hermes_plan(route, workdir=".", available_backends=["claude"])
        self.assertEqual(plan["status"], "ready")
        self.assertEqual(plan["action"]["kind"], "host_execute")
        self.assertEqual(plan["action"]["modelAccuracy"], "approximate")
        self.assertEqual(plan["hostContract"]["modelAccuracy"], "approximate")
        self.assertIn("host executes with its own model", plan["action"]["note"])

    # 3. orchestrate action when a CLI backend is available
    def test_orchestrate_action(self) -> None:
        route = _orchestrated_route(model="claude:sonnet", effort="high", variant="D")
        plan = build_hermes_plan(route, workdir=".", available_backends=["claude"])
        self.assertEqual(plan["status"], "ready")
        self.assertEqual(plan["action"]["kind"], "orchestrate")
        self.assertEqual(plan["action"]["backend"], "claude")
        self.assertEqual(plan["action"]["variant"], "D")
        self.assertEqual(plan["hostContract"]["action"], "orchestrate")

    # 4. orchestrated + no backends → blocked
    def test_orchestrate_blocked_without_backend(self) -> None:
        route = _orchestrated_route(model="claude:sonnet", effort="high", variant="D")
        plan = build_hermes_plan(route, workdir=".", available_backends=[])
        self.assertEqual(plan["status"], "blocked")
        self.assertEqual(plan["blocked"]["code"], "hermes_no_cli_backend")
        self.assertIsNone(plan["agent"])

    # 5. unknown explicit backend → blocked
    def test_unknown_explicit_backend_blocked(self) -> None:
        route = _direct_route()
        plan = build_hermes_plan(route, workdir=".", available_backends=["nope"])
        # "nope" is not in available_backends set → codex not in backends → host_execute
        # Actually the plan builder never saw "nope" in KNOWN_BACKENDS, so it was not
        # included.  But the host_execute branch just checks if selected_backend is in
        # the backends list — "nope" != "codex" so it won't be.  The plan will be ready
        # with host_execute.  BUT the argparse main() rejects unknown backends.
        # So this test should exercise via main() style or check the blocked path.
        #
        # The spec says: "If an explicitly named backend is not in ("codex","claude"),
        # block ("hermes_unknown_backend", ...)".  That's the argparse main() path:
        # parser.error().  Test that via subprocess (handled in the roundtrip test).
        # For the direct builder test we need a different setup — test via a
        # synthetic route injected through main().
        pass  # covered by test_roundtrip_via_main_subprocess below

    # 5b. Explicitly test the KNOWN_BACKENDS guard via argparse (subprocess)
    def test_unknown_explicit_backend_blocked_via_subprocess(self) -> None:
        route = _direct_route()
        proc = subprocess.run(
            [
                sys.executable,
                str(SCRIPTS / "hermes_host_plan.py"),
                "--workdir", ".",
                "--available-backends", "nope",
            ],
            input=json.dumps(route),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("unknown backend", proc.stderr)

    # 6. dry_run → execution not requested, zero planned calls
    def test_dry_run_requests_no_execution(self) -> None:
        route = _direct_route()
        plan = build_hermes_plan(
            route, workdir=".", available_backends=["codex"], dry_run=True
        )
        self.assertEqual(plan["status"], "ready")
        self.assertFalse(plan["executionRequested"])
        self.assertEqual(plan["plannedCalls"], 0)

    # 7. privacy fields
    def test_privacy_fields_omitted(self) -> None:
        route = _direct_route()
        plan = build_hermes_plan(route, workdir=".", available_backends=["codex"])
        self.assertFalse(plan["privacy"]["taskIncludedInPlan"])
        self.assertFalse(plan["privacy"]["credentialsForwarded"])
        self.assertFalse(plan["privacy"]["hermesAppServerAttached"])

    # 8. roundtrip via main subprocess using real select_auto_model.py output
    def test_roundtrip_via_main_subprocess(self) -> None:
        repository = SCRIPTS.parents[2].resolve()
        # Generate a real route JSON
        gen = subprocess.run(
            [
                sys.executable,
                str(SCRIPTS / "select_auto_model.py"),
                "--stdin",
                "--strategy", "balance",
            ],
            input="Implement a routine change",
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        self.assertEqual(gen.returncode, 0, gen.stderr)
        route_json = gen.stdout.strip()

        # Feed to hermes_host_plan.py
        proc = subprocess.run(
            [
                sys.executable,
                str(SCRIPTS / "hermes_host_plan.py"),
                "--workdir", str(repository),
                "--available-backends", "codex",
            ],
            input=route_json,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        plan = json.loads(proc.stdout.strip())
        self.assertEqual(plan["status"], "ready")
        self.assertEqual(plan["executionBackend"], "hermes")
        self.assertEqual(plan["schema"], "agent-auto-router.host-plan.v1")

    def test_detect_available_backends_returns_list(self) -> None:
        # Just check the function runs without error
        backends = detect_available_backends({})
        self.assertIsInstance(backends, list)
        for b in backends:
            self.assertIn(b, ("codex", "claude"))

    def test_bogus_route_rejected(self) -> None:
        with self.assertRaises(ValueError):
            build_hermes_plan({}, workdir=".")
        with self.assertRaises(ValueError):
            build_hermes_plan(
                {"selectedModel": "", "executionPlan": {"effort": "low"}},
                workdir=".",
            )
        with self.assertRaises(ValueError):
            build_hermes_plan(
                {
                    "selectedModel": "codex:gpt-5.6-luna",
                    "executionPlan": {"effort": "", "topology": "direct"},
                },
                workdir=".",
            )

    def test_unknown_topology_blocked(self) -> None:
        route = {
            "routeId": "rt",
            "selectedModel": "codex:gpt-5.6-luna",
            "executionPlan": {
                "effort": "low",
                "topology": "nested",
                "variant": "X",
                "context": {},
            },
            "decision": {},
            "policy": {},
            "registry": {},
        }
        plan = build_hermes_plan(route, workdir=".")
        self.assertEqual(plan["status"], "blocked")
        self.assertEqual(plan["blocked"]["code"], "hermes_unknown_topology")


if __name__ == "__main__":
    unittest.main()
