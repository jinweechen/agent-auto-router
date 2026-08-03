#!/usr/bin/env python3
"""Zero-model-call evaluation for the isolated Auto selector."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from select_auto_model import MODELS, select_model  # noqa: E402

CASES = [
    ("constrained", "Reply with exactly OK", {"intelligence": "gpt-5.6-terra", "balance": "gpt-5.6-luna", "cost": "gpt-5.6-luna"}),
    ("routine", "Implement a routine API endpoint and unit tests", {"intelligence": "gpt-5.6-terra", "balance": "gpt-5.6-terra", "cost": "gpt-5.6-luna"}),
    ("complex", "Redesign the distributed architecture and resolve concurrency tradeoffs", {"intelligence": "gpt-5.6-sol", "balance": "gpt-5.6-sol", "cost": "gpt-5.6-terra"}),
    ("high-risk", "Review a production authentication migration for security vulnerabilities", {"intelligence": "gpt-5.6-sol", "balance": "gpt-5.6-sol", "cost": "gpt-5.6-sol"}),
]

def check(name: str, actual: object, expected: object) -> dict[str, object]:
    return {"name": name, "passed": actual == expected, "actual": actual, "expected": expected}

def evaluate() -> dict[str, object]:
    checks = []
    routes = []
    for strategy in ("intelligence", "balance", "cost"):
        for case_id, prompt, expected in CASES:
            decision = select_model(prompt, strategy, "medium")
            checks.append(check(f"route:{strategy}:{case_id}", decision.model, expected[strategy]))
            routes.append({"strategy": strategy, "case": case_id, "model": decision.model, "reason": decision.reason})
    checks.append(check("closed-allowlist", sorted(MODELS), ["gpt-5.6-luna", "gpt-5.6-sol", "gpt-5.6-terra"]))
    checks.append(check("xhigh-escalation", select_model("Implement this change", "balance", "xhigh").model, "gpt-5.6-sol"))
    checks.append(check("chinese-constrained", select_model("请格式化这段文本", "balance", "medium").model, "gpt-5.6-luna"))
    passed = sum(1 for item in checks if item["passed"])
    return {
        "schemaVersion": 1,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "modelCalls": 0,
        "summary": {"passed": passed, "failed": len(checks) - passed, "total": len(checks)},
        "routes": routes,
        "checks": checks,
    }

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = evaluate()
    payload = json.dumps(report, ensure_ascii=True, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload + "\n", encoding="utf-8")
    print(payload)
    return 0 if report["summary"]["failed"] == 0 else 1

if __name__ == "__main__":
    raise SystemExit(main())
