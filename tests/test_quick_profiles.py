from __future__ import annotations

import json
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "agent-auto-router" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from quick_profiles import SCHEMA, load_quick_profiles, profile_payload  # noqa: E402


class QuickProfileTests(unittest.TestCase):
    def test_packaged_profiles_keep_the_small_surface_and_safe_boundaries(self) -> None:
        profiles = load_quick_profiles()
        self.assertEqual(profiles.default_profile, "standard")
        self.assertEqual(set(profiles.profiles), {"safe", "standard"})
        self.assertEqual(profiles.profiles["safe"].sandbox, "read-only")
        self.assertFalse(profiles.profiles["safe"].enableLearningPolicy)
        self.assertFalse(profiles.profiles["safe"].enableFeedback)
        self.assertEqual(profiles.profiles["safe"].repositoryContextMode, "off")
        self.assertEqual(profiles.profiles["safe"].modelAffinity, "off")
        self.assertEqual(profiles.profiles["standard"].sandbox, "workspace-write")
        self.assertTrue(profiles.profiles["standard"].enableLearningPolicy)
        self.assertTrue(profiles.profiles["standard"].enableFeedback)
        self.assertEqual(profiles.profiles["standard"].repositoryContextMode, "adaptive")
        self.assertEqual(profiles.profiles["standard"].modelAffinity, "session")
        payload = profile_payload(profiles, "standard")
        self.assertEqual(payload["schema"], SCHEMA)
        self.assertEqual(payload["modelCalls"], 0)

    def test_profile_loader_rejects_a_write_capable_safe_profile(self) -> None:
        source = json.loads(
            (SCRIPTS / "quick_profiles.json").read_text(encoding="utf-8")
        )
        source["profiles"]["safe"]["sandbox"] = "workspace-write"
        with tempfile.TemporaryDirectory() as temporary:
            path = pathlib.Path(temporary) / "profiles.json"
            path.write_text(json.dumps(source), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "safe profile"):
                load_quick_profiles(path)

    def test_profile_loader_rejects_affinity_for_safe_profile(self) -> None:
        source = json.loads(
            (SCRIPTS / "quick_profiles.json").read_text(encoding="utf-8")
        )
        source["profiles"]["safe"]["modelAffinity"] = "auto"
        with tempfile.TemporaryDirectory() as temporary:
            path = pathlib.Path(temporary) / "profiles.json"
            path.write_text(json.dumps(source), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "safe profile"):
                load_quick_profiles(path)

    def test_profile_loader_rejects_learning_or_feedback_for_safe_profile(self) -> None:
        for field in ("enableLearningPolicy", "enableFeedback"):
            with self.subTest(field=field):
                source = json.loads(
                    (SCRIPTS / "quick_profiles.json").read_text(encoding="utf-8")
                )
                source["profiles"]["safe"][field] = True
                with tempfile.TemporaryDirectory() as temporary:
                    path = pathlib.Path(temporary) / "profiles.json"
                    path.write_text(json.dumps(source), encoding="utf-8")
                    with self.assertRaisesRegex(ValueError, "safe profile"):
                        load_quick_profiles(path)

    def test_profile_loader_rejects_repository_scan_for_standard_profile(self) -> None:
        source = json.loads(
            (SCRIPTS / "quick_profiles.json").read_text(encoding="utf-8")
        )
        source["profiles"]["standard"]["repositoryContextMode"] = "off"
        with tempfile.TemporaryDirectory() as temporary:
            path = pathlib.Path(temporary) / "profiles.json"
            path.write_text(json.dumps(source), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "standard profile"):
                load_quick_profiles(path)

    @unittest.skipUnless(
        shutil.which("pwsh") or shutil.which("powershell"),
        "PowerShell is required for the quick wrapper DryRun",
    )
    def test_quick_wrapper_profiles_are_zero_call_and_bounded(self) -> None:
        powershell = shutil.which("pwsh") or shutil.which("powershell")
        expected = {
            "safe": ("read-only", "off", False, False),
            "standard": ("workspace-write", "session", True, True),
        }
        for profile, (sandbox, affinity, learning, feedback) in expected.items():
            with self.subTest(profile=profile):
                completed = subprocess.run(
                    [
                        str(powershell), "-NoProfile", "-NonInteractive", "-File",
                        str(SCRIPTS / "aar.ps1"),
                        "run", "Reply with exactly OK",
                        "-Profile", profile,
                        "-DryRun", "-Json",
                        "-Workdir", str(ROOT),
                    ],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    check=False,
                )
                self.assertEqual(completed.returncode, 0, completed.stderr)
                route = json.loads(completed.stdout)
                self.assertEqual(route["quickProfile"], profile)
                self.assertEqual(route["effectiveSandbox"], sandbox)
                self.assertEqual(route["effectiveModelAffinity"], affinity)
                self.assertEqual(route["learningPolicyOnExecution"], learning)
                self.assertEqual(route["feedbackOnExecution"], feedback)
                self.assertEqual(route["routeModelCalls"], 0)

    @unittest.skipUnless(
        shutil.which("pwsh") or shutil.which("powershell"),
        "PowerShell is required for the quick wrapper DryRun",
    )
    def test_no_feedback_overrides_standard_persistence_only(self) -> None:
        powershell = shutil.which("pwsh") or shutil.which("powershell")
        completed = subprocess.run(
            [
                str(powershell), "-NoProfile", "-NonInteractive", "-File",
                str(SCRIPTS / "aar.ps1"),
                "run", "Reply with exactly OK",
                "-Profile", "standard",
                "-NoFeedback", "-DryRun", "-Json",
                "-Workdir", str(ROOT),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        route = json.loads(completed.stdout)
        self.assertTrue(route["learningPolicyOnExecution"])
        self.assertFalse(route["feedbackOnExecution"])
        self.assertEqual(route["routeModelCalls"], 0)

    @unittest.skipUnless(
        sys.platform == "win32" and (shutil.which("pwsh") or shutil.which("powershell")),
        "Windows PowerShell is required for the single-root boundary regression",
    )
    def test_protected_standard_boundary_serializes_one_writable_root_as_array(self) -> None:
        powershell = shutil.which("pwsh") or shutil.which("powershell")
        with tempfile.TemporaryDirectory() as temporary:
            temporary_root = pathlib.Path(temporary)
            workdir = temporary_root / "workdir"
            state_dir = temporary_root / "state"
            workdir.mkdir()
            state_dir.mkdir()
            completed = subprocess.run(
                [
                    str(powershell), "-NoProfile", "-NonInteractive", "-File",
                    str(SCRIPTS / "invoke_auto_task.ps1"),
                    "-Task", "Boundary serialization regression",
                    "-ModelChoice", "definitely-not-a-model",
                    "-Sandbox", "workspace-write",
                    "-Workdir", str(workdir),
                    "-StateDir", str(state_dir),
                    "-FeedbackFile", str(state_dir / "feedback.jsonl"),
                    "-EnableLearningPolicy", "-EnableFeedback",
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
            )
        output = completed.stdout + completed.stderr
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("Auto model selection failed", output)
        self.assertNotIn("writableRoots must be an array", output)
        self.assertNotIn("Guarded automatic learning boundary is unsafe", output)


if __name__ == "__main__":
    unittest.main()
