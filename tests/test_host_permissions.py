from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest


SCRIPTS = pathlib.Path(__file__).resolve().parents[1] / "skills" / "agent-auto-router" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from host_permissions import cli_permission_issue, parse_host_permissions, workdir_is_writable  # noqa: E402


def snapshot(sandbox: str, roots: list[str] | None = None) -> dict:
    default_roots = [str(SCRIPTS.parents[2].resolve())] if sandbox == "workspace-write" else []
    return {
        "schema": "agent-auto-router.host-permissions",
        "source": "test-host-turn",
        "sandbox": sandbox,
        "approvalPolicy": "never",
        "networkAccess": False,
        "writableRoots": default_roots if roots is None else roots,
        "canRequestPermissions": False,
    }


class HostPermissionsTests(unittest.TestCase):
    def test_inherit_preserves_all_supported_host_sandboxes(self) -> None:
        for sandbox in ("read-only", "workspace-write", "danger-full-access"):
            with self.subTest(sandbox=sandbox):
                permissions = parse_host_permissions(snapshot(sandbox))
                self.assertEqual(permissions.effective_sandbox(), sandbox)
                self.assertTrue(permissions.as_plan()["noPrivilegeEscalation"])

    def test_explicit_request_can_only_tighten(self) -> None:
        permissions = parse_host_permissions(snapshot("workspace-write"))
        self.assertEqual(permissions.effective_sandbox("danger-full-access"), "workspace-write")
        self.assertEqual(permissions.effective_sandbox("read-only"), "read-only")

    def test_legacy_on_failure_is_normalized_for_codex_cli(self) -> None:
        raw = snapshot("workspace-write")
        raw["approvalPolicy"] = "on-failure"
        permissions = parse_host_permissions(raw)
        self.assertEqual(permissions.approval_policy, "on-failure")
        self.assertEqual(permissions.codex_approval_policy, "on-request")

    def test_accepts_official_app_server_sandbox_shape(self) -> None:
        raw = snapshot("workspace-write")
        raw.pop("sandbox")
        raw["sandboxPolicy"] = {"type": "dangerFullAccess"}
        self.assertEqual(parse_host_permissions(raw).sandbox, "danger-full-access")

    def test_workspace_root_is_enforced(self) -> None:
        with tempfile.TemporaryDirectory() as allowed, tempfile.TemporaryDirectory() as denied:
            permissions = parse_host_permissions(snapshot("workspace-write", [allowed]))
            self.assertTrue(workdir_is_writable(pathlib.Path(allowed), permissions))
            self.assertFalse(workdir_is_writable(pathlib.Path(denied), permissions))

    def test_cli_blocks_unrepresentable_full_access_without_network(self) -> None:
        permissions = parse_host_permissions(snapshot("danger-full-access"))
        self.assertIsNotNone(cli_permission_issue(permissions))
        self.assertIsNone(cli_permission_issue(permissions, "read-only"))
        unknown = snapshot("danger-full-access")
        unknown["networkAccess"] = None
        self.assertIsNotNone(cli_permission_issue(parse_host_permissions(unknown)))

    def test_rejects_untrusted_or_inconsistent_shapes(self) -> None:
        with self.assertRaises(ValueError):
            parse_host_permissions({"sandbox": "danger-full-access"})
        with self.assertRaises(ValueError):
            parse_host_permissions(snapshot("read-only", [str(SCRIPTS.resolve())]))
        invalid = snapshot("workspace-write", ["relative"])
        with self.assertRaises(ValueError):
            parse_host_permissions(invalid)
        with self.assertRaises(ValueError):
            parse_host_permissions(snapshot("workspace-write", []))


if __name__ == "__main__":
    unittest.main()
