from __future__ import annotations

import json
import re
import sys
import unittest
from pathlib import Path
from urllib.parse import unquote


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = ROOT / "skills" / "agent-auto-router" / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

from desktop_execution import SCHEMA as DESKTOP_PLAN_SCHEMA  # noqa: E402
from host_execution_plan import SCHEMA as HOST_PLAN_SCHEMA  # noqa: E402
from host_permissions import SCHEMA as HOST_PERMISSIONS_SCHEMA  # noqa: E402
from routing_policy import FEATURE_SCHEMA_VERSION  # noqa: E402


README = ROOT / "README.md"
CHINESE_README = ROOT / "README.zh-CN.md"
MODEL_REGISTRY = SCRIPT_DIR / "model_registry.json"


def powershell_parameters(script: Path) -> set[str]:
    text = script.read_text(encoding="utf-8-sig")
    parameter_names = set(re.findall(r"\]\s*\$([A-Za-z][A-Za-z0-9]*)", text))
    aliases = set(re.findall(r"\[Alias\(['\"]([^'\"]+)['\"]\)\]", text))
    return parameter_names | aliases


class DocumentationContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.readme = README.read_text(encoding="utf-8")
        cls.chinese_readme = CHINESE_README.read_text(encoding="utf-8")

    def test_local_markdown_links_exist(self) -> None:
        missing: list[str] = []
        for readme_name, readme in (
            (README.name, self.readme),
            (CHINESE_README.name, self.chinese_readme),
        ):
            for raw_target in re.findall(r"\[[^\]]+\]\(([^)]+)\)", readme):
                target = raw_target.strip().split("#", 1)[0]
                if not target or target.startswith(("#", "http://", "https://", "mailto:")):
                    continue
                path = ROOT / unquote(target)
                if not path.exists():
                    missing.append(f"{readme_name}: {raw_target}")
        self.assertEqual(missing, [], f"README has missing local links: {missing}")

    def test_readmes_link_to_each_other(self) -> None:
        self.assertIn("[简体中文](README.zh-CN.md)", self.readme)
        self.assertIn("[English](README.md)", self.chinese_readme)

    def test_documented_schemas_match_source_constants(self) -> None:
        for readme in (self.readme, self.chinese_readme):
            self.assertIn(f"`{DESKTOP_PLAN_SCHEMA}`", readme)
            self.assertIn(f"`{HOST_PLAN_SCHEMA}`", readme)
            self.assertIn(f"`{HOST_PERMISSIONS_SCHEMA}`", readme)
        self.assertIn(f"Current v{FEATURE_SCHEMA_VERSION} records", self.readme)
        self.assertIn(f"当前 v{FEATURE_SCHEMA_VERSION} 数据", self.chinese_readme)

    def test_documented_project_version_matches_pyproject(self) -> None:
        pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        match = re.search(r'^version\s*=\s*"([^"]+)"', pyproject, re.MULTILINE)
        self.assertIsNotNone(match)
        self.assertIn(f"Current project version: `{match.group(1)}`", self.readme)
        self.assertIn(f"当前项目版本：`{match.group(1)}`", self.chinese_readme)
        plugin = json.loads((ROOT / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8"))
        self.assertEqual(plugin["version"], match.group(1))

    def test_plugin_installation_flow_is_actionable(self) -> None:
        self.assertIn('python "./scripts/install_personal_plugin.py"', self.readme)
        self.assertIn("~/.agents/plugins/marketplace.json", self.readme)
        self.assertIn("codex plugin remove agent-auto-router@personal", self.readme)

    def test_backend_qualified_model_ids_are_registered(self) -> None:
        registry = json.loads(MODEL_REGISTRY.read_text(encoding="utf-8"))
        registered = {model["id"] for model in registry["models"]}
        documented = set(re.findall(r"\b(?:codex|claude):[A-Za-z0-9._-]+", self.readme))
        self.assertTrue(documented, "README should document at least one qualified model ID")
        self.assertEqual(documented - registered, set())

    def test_documented_powershell_entrypoint_flags_exist(self) -> None:
        scripts = (
            SCRIPT_DIR / "invoke_auto_task.ps1",
            SCRIPT_DIR / "invoke_orchestrated_task.ps1",
        )
        supported = set().union(*(powershell_parameters(script) for script in scripts))
        blocks = re.findall(r"```powershell\s*(.*?)```", self.readme, re.DOTALL)
        entrypoint_blocks = [
            block
            for block in blocks
            if "invoke_auto_task.ps1" in block or "invoke_orchestrated_task.ps1" in block
        ]
        self.assertTrue(entrypoint_blocks)
        documented = set(
            re.findall(r"(?<!-)-([A-Z][A-Za-z0-9]*)", "\n".join(entrypoint_blocks))
        )
        self.assertEqual(documented - supported, set())


if __name__ == "__main__":
    unittest.main()
