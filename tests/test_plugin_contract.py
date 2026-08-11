from __future__ import annotations

import json
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from validate_plugin import validate  # noqa: E402


MANIFEST_PATH = ROOT / ".codex-plugin" / "plugin.json"


class PluginContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))

    def test_repository_is_a_valid_plugin_root(self) -> None:
        self.assertEqual(validate(ROOT), [])

    def test_plugin_and_project_versions_match(self) -> None:
        pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        match = re.search(r'^version\s*=\s*"([^"]+)"', pyproject, re.MULTILINE)
        self.assertIsNotNone(match)
        self.assertEqual(self.manifest["version"], match.group(1))

    def test_first_release_is_skill_only(self) -> None:
        self.assertEqual(self.manifest["skills"], "./skills/")
        self.assertNotIn("apps", self.manifest)
        self.assertNotIn("mcpServers", self.manifest)
        self.assertNotIn("hooks", self.manifest)
        self.assertFalse((ROOT / ".app.json").exists())
        self.assertFalse((ROOT / ".mcp.json").exists())

    def test_cross_host_entrypoints_remain_packaged(self) -> None:
        skill_scripts = ROOT / "skills" / "agent-auto-router" / "scripts"
        for name in (
            "aar.ps1", "quick_profiles.py", "quick_profiles.json",
            "host_execution_plan.py", "invoke_auto_task.ps1", "install.ps1",
        ):
            self.assertTrue((skill_scripts / name).is_file(), name)

    def make_plugin_fixture(self, temp_root: Path) -> Path:
        plugin_root = temp_root / "agent-auto-router"
        shutil.copytree(ROOT / ".codex-plugin", plugin_root / ".codex-plugin")
        shutil.copytree(
            ROOT / "skills" / "agent-auto-router",
            plugin_root / "skills" / "agent-auto-router",
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )
        return plugin_root

    def write_manifest(self, plugin_root: Path, manifest: dict[str, object]) -> None:
        (plugin_root / ".codex-plugin" / "plugin.json").write_text(
            json.dumps(manifest, indent=2) + "\n",
            encoding="utf-8",
        )

    def test_rejects_non_skill_directories_under_skills(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            plugin_root = self.make_plugin_fixture(Path(temp_dir))
            (plugin_root / "skills" / "stray-cache").mkdir()
            errors = validate(plugin_root)
        self.assertTrue(any("stray-cache" in error and "missing SKILL.md" in error for error in errors))

    def test_rejects_unknown_interface_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            plugin_root = self.make_plugin_fixture(Path(temp_dir))
            manifest = dict(self.manifest)
            manifest["interface"] = dict(self.manifest["interface"])
            manifest["interface"]["unexpectedField"] = True
            self.write_manifest(plugin_root, manifest)
            errors = validate(plugin_root)
        self.assertTrue(any("unexpectedField" in error for error in errors))

    def test_rejects_invalid_mcp_servers_type(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            plugin_root = self.make_plugin_fixture(Path(temp_dir))
            manifest = dict(self.manifest)
            manifest["mcpServers"] = 42
            self.write_manifest(plugin_root, manifest)
            errors = validate(plugin_root)
        self.assertIn("plugin mcpServers must be a string path or object", errors)

    def test_accepts_inline_mcp_server_objects(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            plugin_root = self.make_plugin_fixture(Path(temp_dir))
            manifest = dict(self.manifest)
            manifest["mcpServers"] = {
                "router-status": {"type": "http", "url": "https://example.test/mcp"}
            }
            self.write_manifest(plugin_root, manifest)
            errors = validate(plugin_root)
        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
