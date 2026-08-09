import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


SCRIPTS = pathlib.Path(__file__).resolve().parents[1] / "skills" / "agent-auto-router" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from codex_cli_adapter import (  # noqa: E402
    CodexCliAdapter,
    environment_for_codex_command,
    resolve_codex_command,
)


class CodexCommandResolutionTests(unittest.TestCase):
    def test_windows_prefers_cli_wrapper_with_companion_resources(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = pathlib.Path(temp_dir)
            wrapper = root / "codex.cmd"
            executable = root / "codex.exe"
            wrapper.write_text("test", encoding="utf-8")
            executable.write_text("test", encoding="utf-8")

            def which(name: str):
                return {
                    "codex.cmd": str(wrapper),
                    "codex.exe": str(executable),
                    "cmd.exe": r"C:\Windows\System32\cmd.exe",
                }.get(name)

            with patch.dict(os.environ, {}, clear=True):
                with patch("codex_cli_adapter.os.name", "nt"):
                    with patch("codex_cli_adapter.shutil.which", side_effect=which):
                        self.assertEqual(
                            resolve_codex_command(),
                            [
                                r"C:\Windows\System32\cmd.exe",
                                "/d",
                                "/c",
                                str(wrapper),
                            ],
                        )

    def test_explicit_codex_cli_path_is_used_before_path_lookup(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            executable = pathlib.Path(temp_dir) / "codex.exe"
            executable.write_text("test", encoding="utf-8")
            with patch.dict(os.environ, {"CODEX_CLI_PATH": str(executable)}, clear=False):
                with patch("codex_cli_adapter.shutil.which", return_value=None):
                    self.assertEqual(resolve_codex_command(), [str(executable)])

    def test_generic_host_detects_explicit_codex_cli_path(self) -> None:
        from host_execution_plan import detect_available_backends

        with tempfile.TemporaryDirectory() as temp_dir:
            executable = pathlib.Path(temp_dir) / "codex.exe"
            executable.write_text("test", encoding="utf-8")
            with patch.dict(os.environ, {"CODEX_CLI_PATH": str(executable)}, clear=False):
                with patch("codex_cli_adapter.shutil.which", return_value=None):
                    self.assertEqual(detect_available_backends({}, ["codex"]), ["codex"])

    def test_single_runner_uses_shared_codex_resolution(self) -> None:
        import single_task_runner

        with tempfile.TemporaryDirectory() as temp_dir:
            executable = pathlib.Path(temp_dir) / "codex.exe"
            executable.write_text("test", encoding="utf-8")
            with patch.dict(os.environ, {"CODEX_CLI_PATH": str(executable)}, clear=False):
                with patch("codex_cli_adapter.shutil.which", return_value=None):
                    self.assertEqual(single_task_runner.resolve_codex_command(), [str(executable)])

    def test_codex_install_directory_is_added_to_child_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            executable = pathlib.Path(temp_dir) / "codex.exe"
            command = [str(executable)]
            environment = environment_for_codex_command(
                command, {"PATH": r"C:\Windows\System32"}
            )
            self.assertEqual(environment["PATH"].split(os.pathsep)[0], str(executable.parent))
            self.assertIn(r"C:\Windows\System32", environment["PATH"])

    def test_empty_base_environment_stays_isolated(self) -> None:
        environment = environment_for_codex_command([], {})
        self.assertEqual(environment, {})

    def test_desktop_process_context_is_not_inherited_by_independent_cli(self) -> None:
        environment = environment_for_codex_command([], {
            "CODEX_PERMISSION_PROFILE": ":workspace",
            "CODEX_SANDBOX_NETWORK_DISABLED": "1",
            "CODEX_THREAD_ID": "desktop-thread",
            "CODEX_INTERNAL_ORIGINATOR_OVERRIDE": "Codex Desktop",
            "CODEX_CA_CERTIFICATE": r"C:\certs\local-ca.pem",
            "CODEX_HOME": r"C:\Users\tester\.codex",
        })
        for name in (
            "CODEX_PERMISSION_PROFILE",
            "CODEX_SANDBOX_NETWORK_DISABLED",
            "CODEX_THREAD_ID",
            "CODEX_INTERNAL_ORIGINATOR_OVERRIDE",
        ):
            self.assertNotIn(name, environment)
        self.assertEqual(environment["CODEX_CA_CERTIFICATE"], r"C:\certs\local-ca.pem")
        self.assertEqual(environment["CODEX_HOME"], r"C:\Users\tester\.codex")

    def test_single_runner_rejects_foreign_backend_before_launch(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            completed = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS / "single_task_runner.py"),
                    "--model",
                    "claude:sonnet",
                    "--effort",
                    "medium",
                    "--sandbox",
                    "read-only",
                    "--workdir",
                    str(SCRIPTS.parents[2]),
                    "--result-file",
                    str(pathlib.Path(temp_dir) / "result.json"),
                ],
                input="Reply with exactly OK",
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("does not belong to backend codex", completed.stderr)


if __name__ == "__main__":
    unittest.main()
