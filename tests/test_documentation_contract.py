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
from model_affinity import (  # noqa: E402
    AFFINITY_TTL_SECONDS,
    MINIMUM_STRONGER_TIER_CACHE_SIGNAL,
    PROFILE_PREFERRED_MAXIMUM_CACHE_SIGNAL,
    PROFILE_PREFERRED_MINIMUM_SAMPLES,
    ROLE_MODEL_POLICY_AFFINITY,
)
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

    def test_beginner_path_is_small_and_precedes_expert_entrypoints(self) -> None:
        quick = SCRIPT_DIR / "aar.ps1"
        parameter_block = quick.read_text(encoding="utf-8-sig").split(
            "$ErrorActionPreference", 1
        )[0]
        parameters = set(
            re.findall(
                r"(?m)^\s*\[(?:string|switch)\]\$([A-Za-z][A-Za-z0-9]*)",
                parameter_block,
            )
        )
        self.assertLessEqual(len(parameters), 8)
        self.assertEqual(
            parameters
            & {
                "ModelChoice", "Sandbox", "HostPermissionsJson", "ResultsDir",
                "StateDir", "FeedbackFile", "Variant", "MaxModelCalls",
                "OrchestrationPolicy", "ConfirmHighRiskOrchestration",
            },
            set(),
        )
        for readme in (self.readme, self.chinese_readme):
            self.assertIn("aar.ps1", readme)
            self.assertIn("-Profile safe", readme)
            self.assertIn("doctor.py --json", readme)
            self.assertLess(readme.index("aar.ps1"), readme.index("invoke_auto_task.ps1"))

    def test_learning_and_orchestration_defaults_are_explicit(self) -> None:
        for readme in (self.readme, self.chinese_readme):
            for mode in ("`off`", "`observe`", "`guarded`"):
                self.assertIn(mode, readme)
            for policy in ("`direct`", "`recommend`", "`auto`"):
                self.assertIn(policy, readme)
            self.assertIn("12", readme)
            self.assertIn("20%", readme)
        self.assertNotIn("configure --mode manual", self.readme)
        self.assertNotIn("configure --mode guarded-auto", self.readme)
        self.assertNotIn("configure --mode manual", self.chinese_readme)
        self.assertNotIn("configure --mode guarded-auto", self.chinese_readme)

    def test_standard_route_documents_zero_state_adaptive_defaults(self) -> None:
        skill = (ROOT / "skills" / "agent-auto-router" / "SKILL.md").read_text(
            encoding="utf-8"
        )
        contract = (
            ROOT / "skills" / "agent-auto-router" / "references" / "router-contract.md"
        ).read_text(encoding="utf-8")
        combined = "\n".join((skill, contract, self.readme, self.chinese_readme))
        for required in (
            "RepositoryContextMode=adaptive",
            "OrchestrationPolicy=recommend",
            "ModelAffinity=session",
            "-EnableLearningPolicy",
            "-EnableFeedback",
        ):
            self.assertIn(required, combined)

    def test_model_affinity_contract_is_documented(self) -> None:
        contract = (
            ROOT / "skills" / "agent-auto-router" / "references" / "router-contract.md"
        ).read_text(encoding="utf-8")
        for readme in (self.readme, self.chinese_readme):
            self.assertIn("-ModelAffinity session", readme)
            self.assertIn("-ModelAffinity auto", readme)
            self.assertIn("-ModelAffinity off", readme)
            self.assertIn(ROLE_MODEL_POLICY_AFFINITY, readme)
        self.assertIn(str(AFFINITY_TTL_SECONDS // 60), contract)
        self.assertIn(f"`{MINIMUM_STRONGER_TIER_CACHE_SIGNAL}`", contract)
        self.assertIn(f"`{PROFILE_PREFERRED_MAXIMUM_CACHE_SIGNAL}`", contract)
        self.assertIn(str(PROFILE_PREFERRED_MINIMUM_SAMPLES), contract)
        self.assertIn("never stores the path itself", contract)
        self.assertIn("not a quality label or billing estimate", contract)

    def test_model_consuming_benchmarks_are_separate_from_the_skill(self) -> None:
        for readme in (self.readme, self.chinese_readme):
            self.assertIn("[benchmarks/](benchmarks/README.md)", readme)
            self.assertIn("`--route-only`", readme)
            self.assertIn("`benchmarks/cases/`", readme)
            self.assertIn("`benchmarks/tools/`", readme)
            self.assertIn("`AGENT_AUTO_ROUTER_EVALUATIONS_DIR`", readme)
        self.assertFalse((SCRIPT_DIR / "codex_cli_orchestration_eval.py").exists())
        self.assertFalse((SCRIPT_DIR / "eval_cases.json").exists())

    def test_desktop_lifecycle_and_change_reconciliation_are_documented(self) -> None:
        skill = (ROOT / "skills" / "agent-auto-router" / "SKILL.md").read_text(
            encoding="utf-8"
        )
        entrypoints = (
            ROOT / "skills" / "agent-auto-router" / "references" / "entrypoints.md"
        ).read_text(encoding="utf-8")
        contract = (
            ROOT / "skills" / "agent-auto-router" / "references" / "router-contract.md"
        ).read_text(encoding="utf-8")
        combined = "\n".join((skill, entrypoints, contract))
        for required in (
            "timed_out",
            "orphaned",
            "try/finally",
            "authoritative terminal",
            "interruptGraceTimeoutMs",
            "total timeout",
            "one open Desktop run",
            "interrupted child without a final outcome",
            "stale host UI",
            "host runtime",
            "explicit content-aware diagnostic",
            "snapshot absence",
        ):
            self.assertIn(required, combined)

    def test_skill_is_explicit_use_only(self) -> None:
        skill = (ROOT / "skills" / "agent-auto-router" / "SKILL.md").read_text(
            encoding="utf-8"
        )
        for required in (
            "explicit-use only",
            "ordinary coding",
            "API queries",
            "do not invoke it",
            "zero snapshots",
        ):
            self.assertIn(required, skill)

    def test_desktop_permission_source_and_structured_block_are_documented(self) -> None:
        skill = (ROOT / "skills" / "agent-auto-router" / "SKILL.md").read_text(
            encoding="utf-8"
        )
        entrypoints = (
            ROOT / "skills" / "agent-auto-router" / "references" / "entrypoints.md"
        ).read_text(encoding="utf-8")
        contract = (
            ROOT / "skills" / "agent-auto-router" / "references" / "router-contract.md"
        ).read_text(encoding="utf-8")
        combined = "\n".join((skill, entrypoints, contract))
        for required in (
            "required non-empty `source`",
            "codex-desktop-current-turn",
            "task cannot supply or override `source`",
            "structured blocked",
            "plannedAgentCalls=0",
            "blocked.code",
            "argparse usage",
        ):
            self.assertIn(required, combined)

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
