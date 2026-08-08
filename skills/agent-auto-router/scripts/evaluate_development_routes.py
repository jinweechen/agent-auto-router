#!/usr/bin/env python3
"""Summarize matched, acceptance-labeled development route results."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from efficiency_metrics import summarize_benchmark


ALLOWED_FIELDS = {
    "caseId", "configuration", "model", "effort", "accepted", "tokens",
    "durationMs", "retries",
}


def load_records(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list) or not payload:
        raise ValueError("benchmark file must contain a non-empty JSON array")
    seen: set[tuple[str, str]] = set()
    records: list[dict[str, Any]] = []
    for index, record in enumerate(payload):
        if not isinstance(record, dict):
            raise ValueError(f"benchmark record {index} must be an object")
        unknown = sorted(set(record) - ALLOWED_FIELDS)
        if unknown:
            raise ValueError(
                f"benchmark record {index} contains unsupported fields: {', '.join(unknown)}"
            )
        for required in ("caseId", "configuration", "accepted", "durationMs"):
            if required not in record:
                raise ValueError(f"benchmark record {index} is missing {required}")
        if not isinstance(record["accepted"], bool):
            raise ValueError(f"benchmark record {index} accepted must be boolean")
        if not isinstance(record["durationMs"], int) or record["durationMs"] < 0:
            raise ValueError(f"benchmark record {index} durationMs must be non-negative")
        identity = (str(record["caseId"]), str(record["configuration"]))
        if identity in seen:
            raise ValueError(f"duplicate benchmark case/configuration: {identity}")
        seen.add(identity)
        tokens = record.get("tokens")
        if tokens is not None:
            if not isinstance(tokens, dict) or set(tokens) - {
                "input", "cached_input", "output", "reasoning_output", "total"
            }:
                raise ValueError(f"benchmark record {index} tokens are invalid")
            if any(
                isinstance(value, bool) or not isinstance(value, int) or value < 0
                for value in tokens.values()
            ):
                raise ValueError(f"benchmark record {index} tokens must be non-negative integers")
            if int(tokens.get("total", 0)) != int(tokens.get("input", 0)) + int(tokens.get("output", 0)):
                raise ValueError(f"benchmark record {index} total tokens are inconsistent")
        records.append(record)
    return records


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = {
        "schemaVersion": 1,
        "source": str(args.results),
        "storesTaskText": False,
        **summarize_benchmark(load_records(args.results)),
    }
    serialized = json.dumps(result, ensure_ascii=True, indent=2) + "\n"
    if args.output:
        args.output.write_text(serialized, encoding="utf-8")
    print(serialized, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
