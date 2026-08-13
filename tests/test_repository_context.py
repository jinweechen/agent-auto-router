from __future__ import annotations

import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = pathlib.Path(__file__).resolve().parents[1] / "skills" / "agent-auto-router" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from repository_context import (  # noqa: E402
    build_repository_context,
    disabled_repository_inspection,
    inspect_repository,
    should_inspect_repository,
)
from routing_policy import select_model  # noqa: E402


class RepositoryContextTests(unittest.TestCase):
    def test_adaptive_mode_scans_code_tasks_but_skips_plain_answers(self) -> None:
        self.assertTrue(
            should_inspect_repository("Fix the failing test in payment_service.py")
        )
        self.assertTrue(
            should_inspect_repository("Inspect the repository code and diagnose the defaults")
        )
        self.assertTrue(should_inspect_repository("重构订单服务和测试模块"))
        self.assertTrue(should_inspect_repository("检查仓库代码并分析路由配置"))
        self.assertFalse(should_inspect_repository("Reply with exactly OK"))
        self.assertFalse(should_inspect_repository("Analyze this business proposal"))
        self.assertTrue(should_inspect_repository("Reply with exactly OK", "auto"))
        self.assertFalse(should_inspect_repository("Fix app.py", "off"))

    def test_context_ranks_task_specific_files_without_storing_task(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            (root / "src").mkdir()
            (root / "tests").mkdir()
            (root / "src" / "payment_service.py").write_text("pass\n", encoding="utf-8")
            (root / "src" / "other.py").write_text("pass\n", encoding="utf-8")
            (root / "tests" / "test_payment.py").write_text("pass\n", encoding="utf-8")
            context, metadata = build_repository_context(
                root,
                "Fix payment_service.py and its tests",
                max_candidate_files=4,
                repo_map_tokens=200,
            )
            self.assertIn("src/payment_service.py", context)
            self.assertGreaterEqual(metadata["candidate_files"], 1)
            self.assertTrue(metadata["context_useful"])
            self.assertNotIn("task", metadata)

    def test_explicit_hidden_relative_path_is_never_crowded_out(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            hidden = root / ".codex-plugin"
            hidden.mkdir()
            (hidden / "plugin.json").write_text(
                '{"version":"private-value-must-not-enter-context"}\n',
                encoding="utf-8",
            )
            ignored = root / ".venv"
            ignored.mkdir()
            (ignored / "noise.json").write_text("{}\n", encoding="utf-8")
            for index in range(12):
                (root / f"plugin_context_version_{index}.json").write_text(
                    "{}\n", encoding="utf-8"
                )

            context, metadata = build_repository_context(
                root,
                "Read `.codex-plugin/plugin.json` and report its version.",
                max_candidate_files=1,
                repo_map_tokens=200,
            )

            self.assertTrue(metadata["task_has_path_hint"])
            self.assertEqual(metadata["repo_files"], 13)
            self.assertIn("- .codex-plugin/plugin.json", context)
            self.assertNotIn("private-value-must-not-enter-context", context)

    def test_windows_relative_path_hint_is_normalized(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            workflow = root / ".github" / "workflows"
            workflow.mkdir(parents=True)
            (workflow / "test.yml").write_text("name: test\n", encoding="utf-8")
            context, metadata = build_repository_context(
                root,
                r"Inspect .github\workflows\test.yml.",
                max_candidate_files=1,
                repo_map_tokens=200,
            )
            self.assertTrue(metadata["task_has_path_hint"])
            self.assertIn("- .github/workflows/test.yml", context)

    def test_tiny_repository_without_candidates_skips_context_injection(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            (root / "app.py").write_text("print('ok')\n", encoding="utf-8")
            _, metadata = build_repository_context(
                root,
                "Explain the concept",
                max_candidate_files=4,
                repo_map_tokens=200,
            )
            self.assertFalse(metadata["context_useful"])

    def test_large_monorepo_features_can_raise_scope_complexity(self) -> None:
        repository = {"large_repo": True, "monorepo": True}
        decision = select_model(
            "Update public API across packages",
            "balance",
            repository_features=repository,
        )
        self.assertEqual(decision.target_tier, "frontier")

    def test_git_repository_inspection_is_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            (root / "app.py").write_text("print('ok')\n", encoding="utf-8")
            inspected = inspect_repository(root)
            self.assertTrue(inspected["is_git_repo"])
            self.assertEqual(inspected["source_files"], 1)
            self.assertIn("scan_duration_ms", inspected)
            self.assertFalse(inspected["scan_truncated"])

    def test_precomputed_inspection_avoids_second_scan(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            (root / "app.py").write_text("print('ok')\n", encoding="utf-8")
            inspection = inspect_repository(root, "Update app.py")
            with patch("repository_context.inspect_repository") as repeated_scan:
                context, metadata = build_repository_context(
                    root,
                    "Update app.py",
                    max_candidate_files=4,
                    repo_map_tokens=200,
                    repository_inspection=inspection,
                )
            repeated_scan.assert_not_called()
            self.assertIn("app.py", context)
            self.assertGreaterEqual(metadata["candidate_files"], 1)

    def test_disabled_inspection_is_explicit_and_empty(self) -> None:
        inspection = disabled_repository_inspection()
        self.assertTrue(inspection["inspection_disabled"])
        self.assertEqual(inspection["files"], [])
        self.assertEqual(inspection["scan_duration_ms"], 0)


if __name__ == "__main__":
    unittest.main()
