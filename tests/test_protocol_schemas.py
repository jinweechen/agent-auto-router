from __future__ import annotations

import copy
import pathlib
import re
import sys
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "agent-auto-router" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from auto_router import route_case  # noqa: E402
from host_permissions import parse_host_permissions  # noqa: E402
from protocol_schemas import (  # noqa: E402
    HOST_PERMISSIONS_SCHEMA,
    ROUTE_DECISION_SCHEMA,
    RUNTIME_PROTOCOL_SCHEMAS,
)
from route_contract import validate_route_decision  # noqa: E402


class ProtocolSchemaTests(unittest.TestCase):
    def test_runtime_protocol_names_are_stable_and_unversioned(self) -> None:
        self.assertEqual(len(RUNTIME_PROTOCOL_SCHEMAS), 17)
        for schema in RUNTIME_PROTOCOL_SCHEMAS:
            with self.subTest(schema=schema):
                self.assertTrue(schema.startswith("agent-auto-router."))
                self.assertIsNone(re.search(r"\.v\d+$", schema))

    def test_version_suffixed_route_and_permissions_are_rejected(self) -> None:
        route = route_case({"id": "stable-schema", "prompt": "Reply OK"})[
            "routeDecision"
        ]
        old_route = copy.deepcopy(route)
        old_route["schema"] = f"{ROUTE_DECISION_SCHEMA}.v2"
        with self.assertRaisesRegex(ValueError, "route schema"):
            validate_route_decision(old_route)

        old_permissions = {
            "schema": f"{HOST_PERMISSIONS_SCHEMA}.v1",
            "source": "test-host-turn",
            "sandbox": "read-only",
            "approvalPolicy": "never",
            "networkAccess": False,
            "writableRoots": [],
            "canRequestPermissions": False,
        }
        with self.assertRaisesRegex(ValueError, "host permissions schema"):
            parse_host_permissions(old_permissions)

    def test_repository_has_no_version_suffixed_router_protocol_literals(self) -> None:
        protocol_names = (
            "route-decision|task-binding|execution-envelope|host-request|host-plan|"
            "host-permissions|desktop-spawn-capabilities|desktop-plan|execution-receipt|execution-report|runner-input|"
            "model-affinity|quick-profiles|doctor|policy-shadow|workspace-snapshot|"
            "workspace-comparison|evaluation-run"
        )
        forbidden = re.compile(
            rf"(?:agent-auto-router\.)?(?:{protocol_names})\.v\d+\b"
        )
        extensions = {".json", ".md", ".ps1", ".py", ".toml", ".yaml", ".yml"}
        violations: list[str] = []
        for path in ROOT.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in extensions:
                continue
            if any(part in {".git", ".claude", "__pycache__"} for part in path.parts):
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            if forbidden.search(text):
                violations.append(str(path.relative_to(ROOT)))
        self.assertEqual(violations, [])


if __name__ == "__main__":
    unittest.main()
