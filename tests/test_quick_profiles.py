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
        self.assertTrue(profiles.profiles["safe"].noFeedback)
        self.assertEqual(profiles.profiles["safe"].modelAffinity, "off")
        self.assertEqual(profiles.profiles["standard"].sandbox, "workspace-write")
        self.assertEqual(profiles.profiles["standard"].modelAffinity, "auto")
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

    @unittest.skipUnless(
        shutil.which("pwsh") or shutil.which("powershell"),
        "PowerShell is required for the quick wrapper DryRun",
    )
    def test_quick_wrapper_profiles_are_zero_call_and_bounded(self) -> None:
        powershell = shutil.which("pwsh") or shutil.which("powershell")
        expected = {
            "safe": ("read-only", "off", False),
            "standard": ("workspace-write", "auto", True),
        }
        for profile, (sandbox, affinity, feedback) in expected.items():
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
                self.assertEqual(route["feedbackOnExecution"], feedback)
                self.assertEqual(route["routeModelCalls"], 0)


if __name__ == "__main__":
    unittest.main()
