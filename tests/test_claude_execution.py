from __future__ import annotations

import inspect
import pathlib
import subprocess
import sys
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

SCRIPTS = pathlib.Path(__file__).resolve().parents[1] / "skills" / "agent-auto-router" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from claude_cli_adapter import (  # noqa: E402
    ClaudeCliAdapter,
    extract_claude_usage,
    extract_result_text,
    parse_claude_json_output,
)
from execution_policy import ExecutionPolicy  # noqa: E402
from orchestration_engine import RunContext  # noqa: E402


class ClaudeExecutionTests(unittest.TestCase):
    def test_adapter_implements_execution_adapter(self) -> None:
        from execution_adapter import ExecutionAdapter  # noqa: E402
        self.assertTrue(issubclass(ClaudeCliAdapter, ExecutionAdapter))
        sig = inspect.signature(ClaudeCliAdapter.create)
        params = list(sig.parameters.keys())
        self.assertIn("context", params)
        self.assertIn("role", params)
        self.assertIn("model", params)
        self.assertIn("effort", params)
        self.assertIn("instructions", params)
        self.assertIn("input_text", params)
        self.assertIn("max_output_tokens", params)

    def test_model_map_resolution(self) -> None:
        adapter = ClaudeCliAdapter()
        # Default empty map: strip claude: prefix, passthrough
        self.assertEqual(adapter._resolve_model("claude:sonnet"), "sonnet")
        self.assertEqual(adapter._resolve_model("claude:opus"), "opus")
        self.assertEqual(adapter._resolve_model("unknown"), "unknown")

        # Custom map overrides after prefix strip
        custom = ClaudeCliAdapter(model_map={"opus": "sonnet"})
        self.assertEqual(custom._resolve_model("claude:opus"), "sonnet")

        # Foreign-backend prefix raises ValueError
        with self.assertRaises(ValueError):
            adapter._resolve_model("codex:gpt-5.6-sol")

    def test_create_with_foreign_backend_model_raises_valueerror(self) -> None:
        instance = object.__new__(ClaudeCliAdapter)
        instance.policy = ExecutionPolicy(False, "workspace-write")
        instance.model_map = {}
        instance.effort_override = None
        instance.role_efforts = {}
        instance.max_model_calls = None
        instance.max_total_tokens = None
        instance.calls_started = 0
        instance._call_lock = threading.Lock()
        instance._call_reservations: dict[int, int] = {}
        instance._token_lock = threading.Lock()
        instance.usage_events_observed = 0
        instance.observed_input_tokens = 0
        instance.observed_cached_input_tokens = 0
        instance.observed_output_tokens = 0
        instance.observed_reasoning_output_tokens = 0
        instance.workdir = pathlib.Path.cwd()
        instance.claude_command = ["claude"]
        instance.max_turns = 30
        instance.allowed_tools = ("Read", "Edit", "Write", "Bash")
        instance.progress_callback = None
        instance.timeout_seconds = 600
        instance.total_timeout_seconds = None
        instance.started_at = time.monotonic()
        instance.execution_mode = False

        with self.assertRaises(ValueError):
            instance.create(
                context=RunContext(),
                role="planner",
                model="codex:gpt-5.6-sol",
                effort="low",
                instructions="test",
                input_text="test",
            )

    def test_effort_normalization(self) -> None:
        adapter = ClaudeCliAdapter()
        self.assertEqual(adapter._normalize_effort("none"), "low")
        self.assertEqual(adapter._normalize_effort("high"), "high")

        self.assertEqual(adapter.effective_effort("planner", "max"), "high")

        adapter.effort_override = "xhigh"
        self.assertEqual(adapter.effective_effort("planner", "high"), "xhigh")
        adapter.effort_override = None

        adapter.role_efforts = {"worker": "low", "reviewer": "xhigh"}
        self.assertEqual(adapter.effective_effort("worker:one", "high"), "low")
        self.assertEqual(adapter.effective_effort("reviewer", "high"), "xhigh")

        # worker: prefix strips to worker for role_efforts lookup
        self.assertEqual(adapter.effective_effort("worker:task-2", "high"), "low")

    def test_allowed_tools_by_role(self) -> None:
        adapter = ClaudeCliAdapter(execution_mode=True)
        full_tools = ["Read", "Edit", "Write", "Bash"]
        self.assertEqual(adapter._allowed_tools_for_role("direct"), full_tools)
        self.assertEqual(adapter._allowed_tools_for_role("reviewer"), full_tools)
        self.assertEqual(adapter._allowed_tools_for_role("planner"), ["Read"])
        self.assertEqual(adapter._allowed_tools_for_role("worker:one"), ["Read"])
        self.assertEqual(adapter._allowed_tools_for_role("grader"), ["Read"])

        # non-execution mode: all roles are read-only
        eval_adapter = ClaudeCliAdapter(execution_mode=False)
        self.assertEqual(eval_adapter._allowed_tools_for_role("direct"), ["Read"])
        self.assertEqual(eval_adapter._allowed_tools_for_role("reviewer"), ["Read"])

    def test_parse_json_output_tolerates_noise(self) -> None:
        result = parse_claude_json_output(
            'Warning: x\n{"type":"result","result":"hello"}\n'
        )
        self.assertEqual(result["result"], "hello")

    def test_extract_usage_details(self) -> None:
        payload: dict[str, object] = {
            "usage": {
                "input_tokens": 100,
                "output_tokens": 50,
                "cache_read_input_tokens": 30,
                "reasoning_tokens": 20,
            }
        }
        usage = extract_claude_usage(payload)  # type: ignore[arg-type]
        self.assertEqual(usage["input_tokens"], 100)
        self.assertEqual(usage["cached_input_tokens"], 30)
        self.assertEqual(usage["output_tokens"], 50)
        self.assertEqual(usage["reasoning_output_tokens"], 20)

        # missing usage dict defaults all keys to 0
        empty_usage = extract_claude_usage({})
        self.assertEqual(empty_usage["input_tokens"], 0)
        self.assertEqual(empty_usage["cached_input_tokens"], 0)
        self.assertEqual(empty_usage["output_tokens"], 0)
        self.assertEqual(empty_usage["reasoning_output_tokens"], 0)

        # null values default to 0
        nulled: dict[str, object] = {"usage": {
            "input_tokens": None,
            "output_tokens": None,
            "cache_read_input_tokens": None,
            "reasoning_tokens": None,
        }}
        nulled_usage = extract_claude_usage(nulled)  # type: ignore[arg-type]
        self.assertEqual(nulled_usage["input_tokens"], 0)
        self.assertEqual(nulled_usage["cached_input_tokens"], 0)
        self.assertEqual(nulled_usage["output_tokens"], 0)
        self.assertEqual(nulled_usage["reasoning_output_tokens"], 0)

    def test_extract_result_text_returns_none_when_absent(self) -> None:
        self.assertIsNone(extract_result_text({"type": "result"}))
        self.assertIsNone(extract_result_text({"type": "result", "result": None}))
        self.assertIsNone(extract_result_text({"type": "result", "result": ""}))
        self.assertEqual(
            extract_result_text({"type": "result", "result": "final answer"}),
            "final answer",
        )

    def test_create_failure_missing_output(self) -> None:
        instance = object.__new__(ClaudeCliAdapter)
        instance.policy = ExecutionPolicy(False, "workspace-write")
        instance.model_map = {}
        instance.effort_override = None
        instance.role_efforts = {}
        instance.max_model_calls = None
        instance.max_total_tokens = None
        instance.calls_started = 0
        instance._call_lock = threading.Lock()
        instance._call_reservations: dict[int, int] = {}
        instance._token_lock = threading.Lock()
        instance.usage_events_observed = 0
        instance.observed_input_tokens = 0
        instance.observed_cached_input_tokens = 0
        instance.observed_output_tokens = 0
        instance.observed_reasoning_output_tokens = 0
        instance.workdir = pathlib.Path.cwd()
        instance.claude_command = ["claude"]
        instance.max_turns = 30
        instance.allowed_tools = ("Read", "Edit", "Write", "Bash")
        instance.progress_callback = None
        instance.timeout_seconds = 600
        instance.total_timeout_seconds = None
        instance.started_at = time.monotonic()
        instance.execution_mode = False

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = '{"type":"result","result":null}'
        mock_result.stderr = ""

        with patch("claude_cli_adapter.subprocess.run", return_value=mock_result):
            with self.assertRaisesRegex(RuntimeError, "no result"):
                instance.create(
                    context=RunContext(),
                    role="planner",
                    model="claude:sonnet",
                    effort="low",
                    instructions="test",
                    input_text="test",
                )

    def test_budget_guard_blocks_readonly_roles(self) -> None:
        instance = object.__new__(ClaudeCliAdapter)
        instance.max_total_tokens = 1
        instance.max_model_calls = None
        instance.calls_started = 0
        instance._call_lock = threading.Lock()
        instance._call_reservations: dict[int, int] = {}
        instance._token_lock = threading.Lock()
        instance.observed_input_tokens = 1
        instance.observed_output_tokens = 0

        with self.assertRaisesRegex(RuntimeError, "token budget exhausted"):
            instance.reserve_call("worker", 0)

        self.assertEqual(instance.reserve_call("direct", 0), 1)

    def test_finalize_argv_keeps_prompt_whole_under_cmd_wrapper(self) -> None:
        instance = object.__new__(ClaudeCliAdapter)
        prompt = "Do the thing now please with spaces and newlines\nsecond line"
        argv = ["cmd.exe", "/d", "/c", "claude.cmd", "-p", prompt, "--model", "haiku"]
        finalized = instance._finalize_argv(argv)
        self.assertEqual(finalized[:3], ["cmd.exe", "/d", "/c"])
        self.assertEqual(len(finalized), 4)
        # The wrapper + arguments must be one quoted command line so cmd.exe
        # cannot re-split the whitespace/newline inside the prompt value.
        self.assertEqual(finalized[3], subprocess.list2cmdline(argv[3:]))
        self.assertIn("claude.cmd", finalized[3])
        self.assertIn(prompt, finalized[3])

    def test_finalize_argv_passthrough_without_cmd_wrapper(self) -> None:
        instance = object.__new__(ClaudeCliAdapter)
        argv = ["claude", "-p", "hello world", "--model", "haiku"]
        self.assertEqual(instance._finalize_argv(argv), argv)


if __name__ == "__main__":
    unittest.main()
