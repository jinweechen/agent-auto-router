#!/usr/bin/env python3
"""Select a GPT-5.6 model without changing Codex configuration."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict

from routing_policy import EFFORTS, MODELS, STRATEGIES, ModelDecision, select_model

Decision = ModelDecision

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--strategy", choices=STRATEGIES, default="balance")
    parser.add_argument("--effort", choices=EFFORTS, default="medium")
    route_input = parser.add_mutually_exclusive_group(required=True)
    route_input.add_argument("--text")
    route_input.add_argument("--stdin", action="store_true")
    args = parser.parse_args()
    prompt = sys.stdin.read() if args.stdin else args.text
    decision = select_model(prompt or "", args.strategy, args.effort)
    print(json.dumps({"decision": asdict(decision), "modelCalls": 0}, ensure_ascii=True))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
