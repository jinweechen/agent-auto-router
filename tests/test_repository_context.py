from __future__ import annotations

import pathlib
import subprocess
import sys
import tempfile
import unittest

SCRIPTS = pathlib.Path(__file__).resolve().parents[1] / "skills" / "agent-auto-router" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from repository_context import build_repository_context, inspect_repository  # noqa: E402
from routing_policy import select_model  # noqa: E402


class RepositoryContextTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
