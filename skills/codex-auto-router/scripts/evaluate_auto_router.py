#!/usr/bin/env python3
"""Zero-model-call evaluation for the shared Auto routing policy."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from auto_router import route_case  # noqa: E402
from model_registry import load_model_registry  # noqa: E402
from routing_policy import RoutingPolicy, load_policy_file, select_model  # noqa: E402

CASES = [
    ("constrained", "Reply with exactly OK", {"intelligence": "balanced", "balance": "fast", "cost": "fast"}),
    ("routine", "Implement a routine API endpoint", {"intelligence": "balanced", "balance": "balanced", "cost": "fast"}),
    ("complex", "Redesign the distributed architecture and resolve concurrency tradeoffs", {"intelligence": "frontier", "balance": "frontier", "cost": "balanced"}),
    ("high-risk", "Review a production authentication migration for security vulnerabilities", {"intelligence": "frontier", "balance": "frontier", "cost": "frontier"}),
]

def check(name: str, actual: object, expected: object) -> dict[str, object]:
    return {"name": name, "passed": actual == expected, "actual": actual, "expected": expected}

def evaluate(policy: RoutingPolicy | None = None) -> dict[str, object]:
    registry = load_model_registry()
    checks = []
    routes = []
    for strategy in ("intelligence", "balance", "cost"):
        for case_id, prompt, expected in CASES:
            decision = select_model(
                prompt, strategy, "medium", policy=policy, registry=registry
            )
            checks.append(check(f"route:{strategy}:{case_id}", decision.target_tier, expected[strategy]))
            routes.append({"strategy": strategy, "case": case_id, "tier": decision.target_tier, "model": decision.model, "reason": decision.reason})

    parallel_case = {
        "prompt": "Implement API and tests for several independent components",
        "acceptance_criteria": ["API", "tests", "docs", "rollback"],
    }
    checks.append(check("variant:d-reachable", route_case(parallel_case, "balance", policy=policy)["variant"], "D"))
    checks.append(check("incidental-security", select_model("Rename the security label", "balance", policy=policy).target_tier, "fast"))
    checks.append(check(
        "registry-route-models-enabled",
        all(route["model"] in registry.enabled_model_ids for route in routes),
        True,
    ))
    checks.append(check(
        "registry-high-risk-primary",
        registry.resolve_tier(
            "frontier", role="direct", required_capabilities=("high-risk-primary",)
        ).tier,
        "frontier",
    ))
    checks.append(check("xhigh-escalation", select_model("Implement this change", "balance", "xhigh", policy=policy).target_tier, "frontier"))
    checks.append(check("chinese-constrained", select_model("请格式化这段文本", "balance", policy=policy).target_tier, "fast"))

    passed = sum(1 for item in checks if item["passed"])
    return {
        "schemaVersion": 2,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "modelCalls": 0,
        "summary": {"passed": passed, "failed": len(checks) - passed, "total": len(checks)},
        "routes": routes,
        "checks": checks,
    }

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--policy-file", type=Path)
    args = parser.parse_args()
    policy = load_policy_file(args.policy_file) if args.policy_file else None
    report = evaluate(policy)
    payload = json.dumps(report, ensure_ascii=True, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload + "\n", encoding="utf-8")
    print(payload)
    return 0 if report["summary"]["failed"] == 0 else 1

if __name__ == "__main__":
    raise SystemExit(main())
