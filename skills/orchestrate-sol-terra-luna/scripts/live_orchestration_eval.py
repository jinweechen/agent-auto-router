from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import json
import os
import pathlib
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any


API_URL = "https://api.openai.com/v1/responses"
ROOT = pathlib.Path(__file__).resolve().parent
DEFAULT_CASES = ROOT / "eval_cases.json"
RESULTS_DIR = ROOT / "eval-results"

# USD per 1M tokens. Update when the pricing page changes.
PRICING = {
    "gpt-5.6-sol": {"input": 5.0, "output": 30.0},
    "gpt-5.6-terra": {"input": 2.0, "output": 12.0},
    "gpt-5.6-luna": {"input": 1.0, "output": 6.0},
}


@dataclass
class CallRecord:
    role: str
    model: str
    effort: str
    latency_seconds: float
    input_tokens: int
    output_tokens: int
    estimated_cost_usd: float
    response_id: str


@dataclass
class RunContext:
    records: list[CallRecord] = field(default_factory=list)

    @property
    def total_cost(self) -> float:
        return sum(record.estimated_cost_usd for record in self.records)

    @property
    def total_latency(self) -> float:
        return sum(record.latency_seconds for record in self.records)


def extract_output_text(response: dict[str, Any]) -> str:
    pieces: list[str] = []
    for item in response.get("output", []):
        if item.get("type") != "message":
            continue
        for content in item.get("content", []):
            if content.get("type") == "output_text" and content.get("text"):
                pieces.append(content["text"])
    if not pieces and response.get("output_text"):
        pieces.append(response["output_text"])
    return "\n".join(pieces).strip()


def parse_json_object(text: str) -> dict[str, Any]:
    cleaned = text.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", cleaned, re.DOTALL)
    if fenced:
        cleaned = fenced.group(1)
    else:
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start >= 0 and end > start:
            cleaned = cleaned[start : end + 1]
    value = json.loads(cleaned)
    if not isinstance(value, dict):
        raise ValueError("Expected a JSON object")
    return value


class ResponsesClient:
    def __init__(self, api_key: str, timeout_seconds: int = 180) -> None:
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds

    def create(
        self,
        *,
        context: RunContext,
        role: str,
        model: str,
        effort: str,
        instructions: str,
        input_text: str,
        max_output_tokens: int = 4000,
    ) -> tuple[str, dict[str, Any]]:
        payload = {
            "model": model,
            "instructions": instructions,
            "input": input_text,
            "reasoning": {"effort": effort},
            "max_output_tokens": max_output_tokens,
            "store": False,
        }
        request = urllib.request.Request(
            API_URL,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )

        started = time.perf_counter()
        last_error: Exception | None = None
        for attempt_index in range(3):
            try:
                with urllib.request.urlopen(
                    request, timeout=self.timeout_seconds
                ) as raw_response:
                    response = json.loads(raw_response.read().decode("utf-8"))
                break
            except urllib.error.HTTPError as exc:
                details = exc.read().decode("utf-8", errors="replace")
                last_error = RuntimeError(f"HTTP {exc.code}: {details}")
                if exc.code not in {429, 500, 502, 503, 504} or attempt_index == 2:
                    raise last_error
                time.sleep(min(2 ** attempt_index, 4))
            except urllib.error.URLError as exc:
                last_error = exc
                if attempt_index == 2:
                    raise
                time.sleep(min(2 ** attempt_index, 4))
        else:
            raise RuntimeError("API request failed") from last_error

        latency = time.perf_counter() - started
        output_text = extract_output_text(response)
        if not output_text:
            raise RuntimeError(f"Response {response.get('id', '<unknown>')} has no output text")

        usage = response.get("usage", {})
        input_tokens = int(usage.get("input_tokens", 0))
        output_tokens = int(usage.get("output_tokens", 0))
        rates = PRICING.get(model, {"input": 0.0, "output": 0.0})
        estimated_cost = (
            input_tokens * rates["input"] + output_tokens * rates["output"]
        ) / 1_000_000
        context.records.append(
            CallRecord(
                role=role,
                model=model,
                effort=effort,
                latency_seconds=latency,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                estimated_cost_usd=estimated_cost,
                response_id=response.get("id", ""),
            )
        )
        return output_text, response


def make_plan(
    client: ResponsesClient,
    context: RunContext,
    case: dict[str, Any],
    model: str,
    effort: str,
) -> dict[str, Any]:
    prompt = json.dumps(
        {
            "request": case["prompt"],
            "acceptance_criteria": case["acceptance_criteria"],
        },
        ensure_ascii=False,
    )
    output, _ = client.create(
        context=context,
        role="planner",
        model=model,
        effort=effort,
        instructions=(
            "Decompose the request into independent, bounded tasks. Return only JSON with "
            "keys summary and tasks. tasks must be an array of objects with id, description, "
            "dependencies, and acceptance_criteria. Return no more than three tasks. Keep the "
            "plan small and executable."
        ),
        input_text=prompt,
    )
    plan = parse_json_object(output)
    if not isinstance(plan.get("tasks"), list) or not plan["tasks"]:
        raise ValueError("Planner returned no tasks")
    return plan


def terra_dispatch(
    client: ResponsesClient,
    context: RunContext,
    case: dict[str, Any],
    plan: dict[str, Any],
) -> dict[str, Any]:
    output, _ = client.create(
        context=context,
        role="dispatcher",
        model="gpt-5.6-terra",
        effort="medium",
        instructions=(
            "Validate and normalize a task plan for parallel workers. Preserve intent, remove "
            "overlap, make dependencies explicit, and include only context required by each task. "
            "Return only JSON with summary and tasks using the original schema."
        ),
        input_text=json.dumps(
            {
                "request": case["prompt"],
                "acceptance_criteria": case["acceptance_criteria"],
                "plan": plan,
            },
            ensure_ascii=False,
        ),
    )
    normalized = parse_json_object(output)
    if not isinstance(normalized.get("tasks"), list) or not normalized["tasks"]:
        raise ValueError("Dispatcher returned no tasks")
    return normalized


def run_workers(
    client: ResponsesClient,
    context: RunContext,
    case: dict[str, Any],
    plan: dict[str, Any],
    max_workers: int,
) -> list[dict[str, Any]]:
    tasks = plan["tasks"]

    def run_one(task: dict[str, Any]) -> dict[str, Any]:
        local_context = RunContext()
        output, _ = client.create(
            context=local_context,
            role=f"worker:{task.get('id', 'unknown')}",
            model="gpt-5.6-luna",
            effort="high",
            instructions=(
                "Complete exactly the assigned bounded task. Do not redesign the architecture or "
                "expand scope. State assumptions, provide the concrete deliverable, and show how "
                "the acceptance criteria are met. Keep the response under 900 words."
            ),
            input_text=json.dumps(
                {
                    "original_request": case["prompt"],
                    "assigned_task": task,
                },
                ensure_ascii=False,
            ),
        )
        return {"task": task, "output": output, "records": local_context.records}

    worker_results: list[dict[str, Any]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = [pool.submit(run_one, task) for task in tasks]
        for future in concurrent.futures.as_completed(futures):
            result = future.result()
            context.records.extend(result.pop("records"))
            worker_results.append(result)
    return worker_results


def synthesize(
    client: ResponsesClient,
    context: RunContext,
    case: dict[str, Any],
    plan: dict[str, Any],
    worker_results: list[dict[str, Any]],
    model: str,
    effort: str,
) -> str:
    output, _ = client.create(
        context=context,
        role="reviewer",
        model=model,
        effort=effort,
        instructions=(
            "Act as the final reviewer. Reconcile worker outputs, correct inconsistencies, reject "
            "unsupported claims, and return one complete final deliverable that satisfies every "
            "acceptance criterion. Do not mention the orchestration process. Keep the response "
            "under 1800 words."
        ),
        input_text=json.dumps(
            {
                "request": case["prompt"],
                "acceptance_criteria": case["acceptance_criteria"],
                "plan": plan,
                "worker_results": worker_results,
            },
            ensure_ascii=False,
        ),
        max_output_tokens=6000,
    )
    return output


def direct_sol(client: ResponsesClient, context: RunContext, case: dict[str, Any]) -> str:
    output, _ = client.create(
        context=context,
        role="direct",
        model="gpt-5.6-sol",
        effort="max",
        instructions=(
            "Produce a complete, implementation-ready answer. Check every acceptance criterion "
            "before returning the final deliverable."
        ),
        input_text=json.dumps(case, ensure_ascii=False),
        max_output_tokens=6000,
    )
    return output


def grade(
    client: ResponsesClient,
    context: RunContext,
    case: dict[str, Any],
    final_output: str,
) -> dict[str, Any]:
    output, _ = client.create(
        context=context,
        role="grader",
        model="gpt-5.6-sol",
        effort="high",
        instructions=(
            "Grade the candidate without knowing how it was produced. Return only JSON with: "
            "score (0-100 integer), passed (boolean), unmet_criteria (array), critical_errors "
            "(array), and rationale (short string). Passing requires every acceptance criterion "
            "and no critical error."
        ),
        input_text=json.dumps(
            {
                "request": case["prompt"],
                "acceptance_criteria": case["acceptance_criteria"],
                "candidate": final_output,
            },
            ensure_ascii=False,
        ),
    )
    return parse_json_object(output)


def run_variant(
    client: ResponsesClient,
    case: dict[str, Any],
    variant: str,
    max_workers: int,
) -> dict[str, Any]:
    context = RunContext()
    started = time.perf_counter()

    if variant == "A":
        final_output = direct_sol(client, context, case)
    elif variant == "B":
        plan = make_plan(client, context, case, "gpt-5.6-sol", "max")
        worker_results = run_workers(client, context, case, plan, max_workers)
        final_output = synthesize(
            client, context, case, plan, worker_results, "gpt-5.6-sol", "max"
        )
    elif variant == "C":
        plan = make_plan(client, context, case, "gpt-5.6-sol", "max")
        plan = terra_dispatch(client, context, case, plan)
        worker_results = run_workers(client, context, case, plan, max_workers)
        final_output = synthesize(
            client, context, case, plan, worker_results, "gpt-5.6-sol", "max"
        )
    elif variant == "D":
        plan = make_plan(client, context, case, "gpt-5.6-terra", "high")
        worker_results = run_workers(client, context, case, plan, max_workers)
        final_output = synthesize(
            client, context, case, plan, worker_results, "gpt-5.6-terra", "high"
        )
    else:
        raise ValueError(f"Unknown variant: {variant}")

    grade_result = grade(client, context, case, final_output)
    wall_seconds = time.perf_counter() - started
    return {
        "case_id": case["id"],
        "variant": variant,
        "grade": grade_result,
        "final_output": final_output,
        "wall_seconds": wall_seconds,
        "summed_call_latency_seconds": context.total_latency,
        "estimated_cost_usd": context.total_cost,
        "calls": [record.__dict__ for record in context.records],
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run live Sol/Terra/Luna orchestration evals")
    parser.add_argument("--cases", type=pathlib.Path, default=DEFAULT_CASES)
    parser.add_argument("--variants", default="A,B,C,D")
    parser.add_argument("--limit", type=int, default=1)
    parser.add_argument("--max-workers", type=int, default=4)
    parser.add_argument("--timeout", type=int, default=180)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        print(
            "OPENAI_API_KEY is not set. Set it in the current PowerShell session, then rerun.",
            file=sys.stderr,
        )
        return 2

    cases = json.loads(args.cases.read_text(encoding="utf-8"))
    if not isinstance(cases, list) or not cases:
        raise ValueError("Cases file must contain a non-empty JSON array")
    selected_cases = cases[: args.limit]
    variants = [item.strip().upper() for item in args.variants.split(",") if item.strip()]
    client = ResponsesClient(api_key, timeout_seconds=args.timeout)

    results: list[dict[str, Any]] = []
    for case in selected_cases:
        for variant in variants:
            print(f"Running case={case['id']} variant={variant}...", flush=True)
            result = run_variant(client, case, variant, args.max_workers)
            results.append(result)
            print(
                f"  score={result['grade'].get('score')} "
                f"passed={result['grade'].get('passed')} "
                f"wall={result['wall_seconds']:.1f}s "
                f"cost=${result['estimated_cost_usd']:.4f}",
                flush=True,
            )

    RESULTS_DIR.mkdir(exist_ok=True)
    timestamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output_path = RESULTS_DIR / f"live-eval-{timestamp}.json"
    output_path.write_text(
        json.dumps(
            {
                "created_at": timestamp,
                "pricing_assumptions": PRICING,
                "results": results,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"Results: {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
