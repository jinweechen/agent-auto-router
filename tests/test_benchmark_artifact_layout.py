from __future__ import annotations

import json
import pathlib
import subprocess
import sys
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
TOOLS = ROOT / "benchmarks" / "tools"
sys.path.insert(0, str(TOOLS))

from artifact_layout import (  # noqa: E402
    ARTIFACTS_ENV,
    SCHEMA,
    create_run_directory,
    default_evaluations_root,
    prepare_explicit_run_directory,
    write_manifest,
)


class BenchmarkArtifactLayoutTests(unittest.TestCase):
    def test_explicit_environment_root_has_priority(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            expected = pathlib.Path(temp) / "custom"
            actual = default_evaluations_root({ARTIFACTS_ENV: str(expected)})
            self.assertEqual(actual, expected.resolve())

    def test_run_directory_uses_kind_timestamp_and_nonce(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            run_id, run_directory = create_run_directory(
                "route-only",
                root=pathlib.Path(temp),
                timestamp="20260811T030000Z",
                nonce="abc12345",
            )
            self.assertEqual(run_id, "20260811T030000Z-abc12345")
            self.assertEqual(
                run_directory,
                pathlib.Path(temp).resolve() / "route-only" / run_id,
            )
            self.assertTrue(run_directory.is_dir())

    def test_explicit_run_directory_must_be_empty(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            target = pathlib.Path(temp) / "run"
            run_id, resolved = prepare_explicit_run_directory(target)
            self.assertEqual(run_id, "run")
            (resolved / "existing.json").write_text("{}", encoding="utf-8")
            with self.assertRaisesRegex(FileExistsError, "must be empty"):
                prepare_explicit_run_directory(target)

    def test_manifest_uses_stable_schema_name(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            target = pathlib.Path(temp)
            manifest_path = write_manifest(target, {"runId": "test", "status": "completed"})
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["schema"], SCHEMA)
            self.assertEqual(manifest["runId"], "test")

    def test_route_only_writes_standard_run_files_without_model_calls(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            run_directory = pathlib.Path(temp) / "route-check"
            result = subprocess.run(
                [
                    sys.executable,
                    str(TOOLS / "codex_cli_orchestration_eval.py"),
                    "--route-only",
                    "--routing-mode",
                    "balance",
                    "--limit",
                    "1",
                    "--results-dir",
                    str(run_directory),
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((run_directory / "route-report.json").is_file())
            manifest = json.loads(
                (run_directory / "manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["status"], "completed")
            self.assertEqual(manifest["modelCalls"], 0)
            self.assertEqual(manifest["orchestrationPolicy"], "direct")
            self.assertEqual(manifest["modelAffinity"], "off")
            report = json.loads(
                (run_directory / "route-report.json").read_text(encoding="utf-8")
            )
            route = report["results"][0]["routing"]
            self.assertEqual(route["execution_plan"]["orchestrationPolicy"], "direct")
            self.assertEqual(route["model_affinity"]["mode"], "off")

    def test_route_only_requires_explicit_auto_orchestration(self) -> None:
        case = [{
            "id": "parallel",
            "prompt": "Implement API and tests for several independent components",
            "acceptance_criteria": ["API", "tests", "docs", "rollback"],
        }]
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            cases_path = root / "cases.json"
            cases_path.write_text(json.dumps(case), encoding="utf-8")
            run_directory = root / "route-check"
            result = subprocess.run(
                [
                    sys.executable,
                    str(TOOLS / "codex_cli_orchestration_eval.py"),
                    "--route-only",
                    "--routing-mode",
                    "balance",
                    "--orchestration-policy",
                    "auto",
                    "--cases",
                    str(cases_path),
                    "--results-dir",
                    str(run_directory),
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(
                (run_directory / "route-report.json").read_text(encoding="utf-8")
            )
            route = report["results"][0]["routing"]
            self.assertEqual(route["variant"], "D")
            self.assertEqual(route["execution_plan"]["orchestrationPolicy"], "auto")
            self.assertEqual(route["model_affinity"]["mode"], "off")


if __name__ == "__main__":
    unittest.main()
