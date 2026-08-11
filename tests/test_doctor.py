from __future__ import annotations

import io
import json
import pathlib
import sys
import unittest
from unittest.mock import patch

SCRIPTS = pathlib.Path(__file__).resolve().parents[1] / "skills" / "agent-auto-router" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import doctor  # noqa: E402


class DoctorTests(unittest.TestCase):
    def test_doctor_is_privacy_safe_and_makes_no_model_calls(self) -> None:
        def command_lookup(name: str) -> str | None:
            return f"/bin/{name}" if name in {"git", "pwsh", "claude"} else None

        with patch("doctor.shutil.which", side_effect=command_lookup), patch(
            "doctor.codex_cli_available", return_value=False
        ) as codex_available:
            result = doctor.build_diagnostic()
        self.assertEqual(result["schema"], "agent-auto-router.doctor.v1")
        self.assertTrue(result["readyForLocalRouting"])
        self.assertTrue(result["readyForCliExecution"])
        self.assertEqual(result["modelCalls"], 0)
        self.assertEqual(result["defaults"]["learningMode"], "observe")
        self.assertEqual(result["defaults"]["orchestrationPolicy"], "auto")
        self.assertEqual(result["defaults"]["quickExecutionTopology"], "direct")
        self.assertNotIn("environment", result)
        self.assertFalse(result["privacy"]["pathsIncluded"])
        self.assertFalse(result["privacy"]["credentialInspection"])
        codex_available.assert_called_once_with(include_environment_locations=False)
        self.assertNotIn(str(SCRIPTS.parents[2].resolve()), json.dumps(result))
        self.assertEqual(
            result["registry"]["registrySource"],
            "packaged:model_registry.json",
        )
        self.assertEqual(
            result["registry"]["quickProfiles"]["available"],
            ["safe", "standard"],
        )

    def test_verbose_paths_are_explicit_opt_in(self) -> None:
        with patch("doctor.shutil.which", return_value=None), patch(
            "doctor.codex_cli_available", return_value=False
        ) as codex_available:
            result = doctor.build_diagnostic(verbose_paths=True)
        self.assertTrue(result["privacy"]["pathsIncluded"])
        self.assertTrue(pathlib.Path(result["registry"]["registrySource"]).is_absolute())
        self.assertIn("commandPaths", result)
        codex_available.assert_called_once_with(include_environment_locations=True)

    def test_doctor_reports_missing_execution_prerequisites(self) -> None:
        with patch("doctor.shutil.which", return_value=None), patch(
            "doctor.codex_cli_available", return_value=False
        ):
            result = doctor.build_diagnostic()
        self.assertIn("no_supported_cli_detected", result["issues"])
        self.assertFalse(result["readyForCliExecution"])

    def test_default_cli_is_a_short_human_summary(self) -> None:
        with patch.object(sys, "argv", ["doctor.py"]), patch(
            "doctor.build_diagnostic",
            return_value={
                "readyForLocalRouting": True,
                "readyForCliExecution": True,
                "commands": {"git": True, "powershell": True, "codex": True, "claude": False},
                "registry": {
                    "quickProfiles": {
                        "available": ["safe", "standard"],
                        "default": "standard",
                    }
                },
                "issues": [],
                "defaults": {
                    "learningMode": "observe",
                    "orchestrationPolicy": "auto",
                    "quickExecutionTopology": "direct",
                },
                "modelCalls": 0,
            },
        ), patch.object(sys, "stdout", io.StringIO()) as output:
            self.assertEqual(doctor.main(), 0)
        summary = output.getvalue()
        self.assertIn("Agent Auto Router doctor: READY", summary)
        self.assertIn("Quick profiles: safe, standard", summary)
        self.assertIn("Defaults: learning=observe, orchestration=auto, quick=direct", summary)
        self.assertNotIn('"schema"', summary)

    def test_json_cli_preserves_machine_readable_output(self) -> None:
        diagnostic = {
            "schema": "agent-auto-router.doctor.v1",
            "readyForLocalRouting": True,
            "readyForCliExecution": False,
            "commands": {},
            "registry": {},
            "issues": ["no_supported_cli_detected"],
            "modelCalls": 0,
        }
        with patch.object(sys, "argv", ["doctor.py", "--json"]), patch(
            "doctor.build_diagnostic", return_value=diagnostic
        ), patch.object(sys, "stdout", io.StringIO()) as output:
            self.assertEqual(doctor.main(), 0)
        self.assertEqual(json.loads(output.getvalue()), diagnostic)


if __name__ == "__main__":
    unittest.main()
