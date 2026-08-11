from __future__ import annotations

import json
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest


SCRIPTS = pathlib.Path(__file__).resolve().parents[1] / "skills" / "agent-auto-router" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from desktop_workspace_snapshot import capture_snapshot, compare_snapshots, main  # noqa: E402


class DesktopWorkspaceSnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        if shutil.which("git") is None:
            self.skipTest("git is required")

    def _git(self, repository: pathlib.Path, *arguments: str) -> None:
        completed = subprocess.run(
            ["git", "-C", str(repository), *arguments],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def _repository(self, root: pathlib.Path) -> pathlib.Path:
        repository = root / "repo"
        repository.mkdir()
        self._git(repository, "init", "--quiet")
        self._git(repository, "config", "user.email", "router-tests@example.invalid")
        self._git(repository, "config", "user.name", "Router Tests")
        (repository / "changed.txt").write_text("committed\n", encoding="utf-8")
        (repository / "unchanged-dirty.txt").write_text("committed\n", encoding="utf-8")
        self._git(repository, "add", "changed.txt", "unchanged-dirty.txt")
        self._git(repository, "commit", "--quiet", "-m", "baseline")
        return repository

    def test_content_manifest_detects_second_edit_to_already_dirty_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = self._repository(pathlib.Path(temporary))
            changed = repository / "changed.txt"
            unchanged_dirty = repository / "unchanged-dirty.txt"
            changed.write_text("dirty before run\n", encoding="utf-8")
            unchanged_dirty.write_text("dirty but untouched by run\n", encoding="utf-8")
            before = capture_snapshot(repository)

            changed.write_text("changed by child\n", encoding="utf-8")
            comparison = compare_snapshots(before, capture_snapshot(repository))

            self.assertEqual(comparison["runChangedPaths"], ["changed.txt"])
            self.assertEqual(comparison["runChangedFileCount"], 1)
            self.assertEqual(
                comparison["preexistingDirtyPaths"],
                ["changed.txt", "unchanged-dirty.txt"],
            )
            self.assertEqual(
                comparison["finalDirtyPaths"],
                ["changed.txt", "unchanged-dirty.txt"],
            )

    def test_capture_cli_rejects_baseline_inside_child_workdir(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = self._repository(pathlib.Path(temporary))
            with self.assertRaisesRegex(ValueError, "outside every child-writable root"):
                main(
                    [
                        "capture",
                        "--workdir",
                        str(repository),
                        "--output",
                        str(repository / "baseline.json"),
                    ]
                )

    def test_capture_cli_rejects_a_sibling_child_writable_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            repository = self._repository(root)
            sibling_writable = root / "sibling-writable"
            sibling_writable.mkdir()
            with self.assertRaisesRegex(ValueError, "outside every child-writable root"):
                main(
                    [
                        "capture",
                        "--workdir",
                        str(repository),
                        "--output",
                        str(sibling_writable / "baseline.json"),
                        "--forbidden-root",
                        str(sibling_writable),
                    ]
                )

    def test_non_git_manifest_counts_files_without_counting_parent_directories(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            before = capture_snapshot(root)
            nested = root / "nested"
            nested.mkdir()
            (nested / "child.txt").write_text("child\n", encoding="utf-8")

            comparison = compare_snapshots(before, capture_snapshot(root))

            self.assertEqual(comparison["runChangedPaths"], ["nested/child.txt"])
            self.assertEqual(comparison["runChangedFileCount"], 1)

    def test_cli_round_trip_reports_separate_run_and_final_dirty_counts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            repository = self._repository(root)
            baseline_path = root / "baseline.json"
            result_path = root / "comparison.json"
            (repository / "unchanged-dirty.txt").write_text("preexisting\n", encoding="utf-8")
            self.assertEqual(
                main(["capture", "--workdir", str(repository), "--output", str(baseline_path)]),
                0,
            )
            (repository / "new.txt").write_text("child output\n", encoding="utf-8")
            self.assertEqual(
                main(
                    [
                        "compare",
                        "--workdir",
                        str(repository),
                        "--baseline",
                        str(baseline_path),
                        "--output",
                        str(result_path),
                    ]
                ),
                0,
            )
            result = json.loads(result_path.read_text(encoding="utf-8"))
            self.assertEqual(result["runChangedPaths"], ["new.txt"])
            self.assertEqual(result["runChangedFileCount"], 1)
            self.assertEqual(result["finalDirtyFileCount"], 2)


if __name__ == "__main__":
    unittest.main()
