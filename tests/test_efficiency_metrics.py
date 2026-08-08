from __future__ import annotations

import json
import pathlib
import subprocess
import sys
import tempfile
import unittest

SCRIPTS = pathlib.Path(__file__).resolve().parents[1] / "skills" / "agent-auto-router" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from efficiency_metrics import summarize_benchmark, summarize_feedback  # noqa: E402


def route(route_id: str, model: str, total: int, outcome: str | None = None):
    events = [{
        "eventType": "route_outcome",
        "routeId": route_id,
        "selectedModel": model,
        "durationMs": 100,
        "observedTokens": {
            "input": total - 10,
            "cached_input": 5,
            "output": 10,
            "reasoning_output": 2,
            "total": total,
        },
    }]
    if outcome:
        events.append({"eventType": "human_label", "routeId": route_id, "outcome": outcome})
    return events


class EfficiencyMetricsTests(unittest.TestCase):
    def test_feedback_reports_tokens_per_pass_only_with_complete_coverage(self):
        events = route("one", "terra", 100, "pass") + route("two", "terra", 200, "fail")
        summary = summarize_feedback(events)
        self.assertTrue(summary["completeLabeledTokenCoverage"])
        self.assertEqual(summary["observedTokensPerPass"], 300)
        self.assertEqual(summary["byFinalModel"]["terra"]["labeledPassRate"], 0.5)

        events[2]["observedTokens"] = None
        incomplete = summarize_feedback(events)
        self.assertFalse(incomplete["completeLabeledTokenCoverage"])
        self.assertIsNone(incomplete["observedTokensPerPass"])

    def test_matched_benchmark_compares_quality_before_tokens(self):
        records = [
            {"caseId": "a", "configuration": "auto", "accepted": True, "durationMs": 10, "tokens": {"input": 80, "output": 20, "total": 100}},
            {"caseId": "a", "configuration": "sol", "accepted": True, "durationMs": 20, "tokens": {"input": 180, "output": 20, "total": 200}},
            {"caseId": "b", "configuration": "auto", "accepted": False, "durationMs": 10, "tokens": {"input": 40, "output": 10, "total": 50}},
            {"caseId": "b", "configuration": "sol", "accepted": True, "durationMs": 20, "tokens": {"input": 180, "output": 20, "total": 200}},
        ]
        summary = summarize_benchmark(records)
        pair = summary["pairwise"][0]
        self.assertEqual(pair["matchedCases"], 2)
        self.assertEqual(pair["rightOnlyAccepted"], 1)
        self.assertEqual(pair["meanObservedTokenDeltaOnBothAccepted"], -100)

    def test_benchmark_cli_rejects_task_text(self):
        with tempfile.TemporaryDirectory() as temp:
            path = pathlib.Path(temp) / "results.json"
            path.write_text(json.dumps([{
                "caseId": "a", "configuration": "auto", "accepted": True,
                "durationMs": 10, "prompt": "private task",
            }]), encoding="utf-8")
            completed = subprocess.run(
                [sys.executable, str(SCRIPTS / "evaluate_development_routes.py"), "--results", str(path)],
                capture_output=True, text=True, encoding="utf-8",
            )
            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("unsupported fields", completed.stderr)


if __name__ == "__main__":
    unittest.main()
