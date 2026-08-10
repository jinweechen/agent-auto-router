from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "scripts" / "install_personal_plugin.py"
sys.path.insert(0, str(ROOT / "scripts"))

from install_personal_plugin import ensure_windows_codex_read_access  # noqa: E402


class PersonalPluginInstallerTests(unittest.TestCase):
    def test_windows_installer_grants_codex_sandbox_read_access(self) -> None:
        target = Path("C:/plugins/agent-auto-router")
        responses = [
            subprocess.CompletedProcess([], 0, "group exists", ""),
            subprocess.CompletedProcess([], 0, "processed", ""),
        ]
        with mock.patch("install_personal_plugin.os.name", "nt"), mock.patch(
            "install_personal_plugin.subprocess.run", side_effect=responses
        ) as run:
            updated = ensure_windows_codex_read_access(target)
        self.assertTrue(updated)
        self.assertEqual(run.call_count, 2)
        grant_args = run.call_args_list[1].args[0]
        self.assertEqual(grant_args[0], "icacls")
        self.assertIn("CodexSandboxUsers:(OI)(CI)(RX)", grant_args)
        self.assertIn("/T", grant_args)
        self.assertNotIn("/C", grant_args)

    def test_windows_installer_skips_missing_codex_sandbox_group(self) -> None:
        target = Path("C:/plugins/agent-auto-router")
        missing = subprocess.CompletedProcess([], 2, "", "missing")
        with mock.patch("install_personal_plugin.os.name", "nt"), mock.patch(
            "install_personal_plugin.subprocess.run", return_value=missing
        ) as run:
            updated = ensure_windows_codex_read_access(target)
        self.assertFalse(updated)
        run.assert_called_once()

    def test_windows_installer_fails_when_acl_update_is_denied(self) -> None:
        target = Path("C:/plugins/agent-auto-router")
        responses = [
            subprocess.CompletedProcess([], 0, "group exists", ""),
            subprocess.CompletedProcess([], 1, "Failed processing 1 files", "Access denied"),
        ]
        with mock.patch("install_personal_plugin.os.name", "nt"), mock.patch(
            "install_personal_plugin.subprocess.run", side_effect=responses
        ):
            with self.assertRaisesRegex(RuntimeError, "cannot grant Codex sandbox read access"):
                ensure_windows_codex_read_access(target)

    def run_installer(self, home: Path, *extra: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                str(INSTALLER),
                "--home",
                str(home),
                "--skip-codex-install",
                *extra,
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_installs_and_repeats_without_touching_real_home(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            home = Path(temp_dir)
            first = self.run_installer(home)
            self.assertEqual(first.returncode, 0, first.stderr)
            first_result = json.loads(first.stdout)
            self.assertTrue(first_result["packageChanged"])
            self.assertTrue(first_result["marketplaceChanged"])
            self.assertFalse(first_result["codexInstalled"])

            plugin_root = home / "plugins" / "agent-auto-router"
            self.assertEqual(
                {path.name for path in plugin_root.iterdir()},
                {".codex-plugin", "skills"},
            )
            self.assertTrue((plugin_root / "skills" / "agent-auto-router" / "SKILL.md").is_file())
            self.assertFalse(any(plugin_root.rglob("*.pyc")))
            self.assertFalse(any(path.name == "__pycache__" for path in plugin_root.rglob("*")))

            marketplace_path = home / ".agents" / "plugins" / "marketplace.json"
            marketplace = json.loads(marketplace_path.read_text(encoding="utf-8"))
            self.assertEqual(marketplace["name"], "personal")
            self.assertEqual(marketplace["interface"]["displayName"], "Personal")
            self.assertEqual(len(marketplace["plugins"]), 1)
            self.assertEqual(
                marketplace["plugins"][0]["source"]["path"],
                "./plugins/agent-auto-router",
            )

            second = self.run_installer(home)
            self.assertEqual(second.returncode, 0, second.stderr)
            second_result = json.loads(second.stdout)
            self.assertFalse(second_result["packageChanged"])
            self.assertFalse(second_result["marketplaceChanged"])
            self.assertIsNone(second_result["backupPath"])

    def test_preserves_marketplace_metadata_and_unrelated_entries(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            home = Path(temp_dir)
            marketplace_path = home / ".agents" / "plugins" / "marketplace.json"
            marketplace_path.parent.mkdir(parents=True)
            marketplace_path.write_text(
                json.dumps(
                    {
                        "name": "my-personal",
                        "interface": {"displayName": "My Plugins"},
                        "plugins": [
                            {
                                "name": "another-plugin",
                                "source": {"source": "local", "path": "./plugins/another-plugin"},
                                "policy": {
                                    "installation": "AVAILABLE",
                                    "authentication": "ON_USE",
                                },
                                "category": "Other",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            result = self.run_installer(home)
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(marketplace_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["name"], "my-personal")
            self.assertEqual(payload["interface"]["displayName"], "My Plugins")
            self.assertEqual([entry["name"] for entry in payload["plugins"]], [
                "another-plugin",
                "agent-auto-router",
            ])

    def test_changed_package_is_backed_up_before_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            home = Path(temp_dir)
            first = self.run_installer(home)
            self.assertEqual(first.returncode, 0, first.stderr)
            installed_skill = home / "plugins" / "agent-auto-router" / "skills" / "agent-auto-router" / "SKILL.md"
            installed_skill.write_text("locally changed\n", encoding="utf-8")

            second = self.run_installer(home)
            self.assertEqual(second.returncode, 0, second.stderr)
            result = json.loads(second.stdout)
            backup = Path(result["backupPath"])
            self.assertTrue(backup.is_dir())
            self.assertEqual(
                (backup / "skills" / "agent-auto-router" / "SKILL.md").read_text(encoding="utf-8"),
                "locally changed\n",
            )
            self.assertEqual(
                installed_skill.read_bytes(),
                (ROOT / "skills" / "agent-auto-router" / "SKILL.md").read_bytes(),
            )

    def test_blocks_legacy_standalone_skill(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            home = Path(temp_dir)
            legacy = home / ".codex" / "skills" / "agent-auto-router"
            legacy.mkdir(parents=True)
            result = self.run_installer(home)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("legacy standalone Skill exists", result.stderr)
            self.assertFalse((home / "plugins" / "agent-auto-router").exists())
            self.assertFalse((home / ".agents" / "plugins" / "marketplace.json").exists())

    def test_conflicting_entry_requires_explicit_force(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            home = Path(temp_dir)
            marketplace_path = home / ".agents" / "plugins" / "marketplace.json"
            marketplace_path.parent.mkdir(parents=True)
            marketplace_path.write_text(
                json.dumps(
                    {
                        "name": "personal",
                        "plugins": [
                            {
                                "name": "agent-auto-router",
                                "source": {"source": "local", "path": "../elsewhere"},
                                "policy": {
                                    "installation": "AVAILABLE",
                                    "authentication": "ON_INSTALL",
                                },
                                "category": "Productivity",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            blocked = self.run_installer(home)
            self.assertNotEqual(blocked.returncode, 0)
            self.assertIn("conflicting", blocked.stderr)
            self.assertFalse((home / "plugins" / "agent-auto-router").exists())

            forced = self.run_installer(home, "--force-marketplace-entry")
            self.assertEqual(forced.returncode, 0, forced.stderr)
            payload = json.loads(marketplace_path.read_text(encoding="utf-8"))
            self.assertEqual(
                payload["plugins"][0]["source"]["path"],
                "./plugins/agent-auto-router",
            )

    def test_alternate_home_requires_skip_codex_install(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            home = Path(temp_dir)
            result = subprocess.run(
                [sys.executable, str(INSTALLER), "--home", str(home)],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("only with --skip-codex-install", result.stderr)
            self.assertFalse((home / "plugins" / "agent-auto-router").exists())


if __name__ == "__main__":
    unittest.main()
