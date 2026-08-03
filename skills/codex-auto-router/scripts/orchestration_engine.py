from __future__ import annotations

import concurrent.futures
import json
import pathlib
import re
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

from routing_policy import LUNA_MODEL, SOL_MODEL, TERRA_MODEL

ROOT = pathlib.Path(__file__).resolve().parent
DEFAULT_CASES = ROOT / "eval_cases.json"


@dataclass
class CallRecord:
    role: str
    model: str
    effort: str
    latency_seconds: float
    input_tokens: int
    output_tokens: int
    estimated_cost_usd: float | None
    response_id: str


@dataclass
class RunContext:
    records: list[CallRecord] = field(default_factory=list)

    @property
    def total_cost(self) -> float | None:
        costs = [record.estimated_cost_usd for record in self.records]
        if not costs or any(cost is None for cost in costs):
            return None
        return sum(cost for cost in costs if cost is not None)

    @property
    def total_latency(self) -> float:
        return sum(record.latency_seconds for record in self.records)


class OrchestrationClient(Protocol):
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
    ) -> tuple[str, dict[str, Any]]: ...


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


def make_plan(
    client: OrchestrationClient,
    context: RunContext,
    case: dict[str, Any],
    model: str,
    effort: str,
) -> dict[str, Any]:
    output, _ = client.create(
        context=context,
        role="planner",
        model=model,
        effort=effort,
        instructions=(
            "Decompose the request into independent bounded tasks. Return only JSON with summary "
            "and tasks. Each task requires id, description, dependencies, and acceptance_criteria. "
            "Return no more than three tasks."
        ),
        input_text=json.dumps(
            {"request": case["prompt"], "acceptance_criteria": case["acceptance_criteria"]},
            ensure_ascii=False,
        ),
    )
    plan = parse_json_object(output)
    if not isinstance(plan.get("tasks"), list) or not plan["tasks"]:
        raise ValueError("Planner returned no tasks")
    return plan


def terra_dispatch(
    client: OrchestrationClient,
    context: RunContext,
    case: dict[str, Any],
    plan: dict[str, Any],
) -> dict[str, Any]:
    output, _ = client.create(
        context=context,
        role="dispatcher",
        model=TERRA_MODEL,
        effort="medium",
        instructions=(
            "Normalize the task plan for bounded workers. Preserve intent, remove overlap, make "
            "dependencies explicit, and return only JSON with summary and tasks."
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
    client: OrchestrationClient,
    context: RunContext,
    case: dict[str, Any],
    plan: dict[str, Any],
    max_workers: int,
) -> list[dict[str, Any]]:
    if max_workers < 1:
        raise ValueError("max_workers must be at least 1")
    tasks = plan["tasks"]
    task_by_id: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    dependencies: dict[str, list[str]] = {}
    for index, task in enumerate(tasks):
        task_id = str(task.get("id") or f"task-{index + 1}")
        if task_id in task_by_id:
            raise ValueError(f"Duplicate task id: {task_id}")
        normalized = dict(task)
        normalized["id"] = task_id
        raw_dependencies = normalized.get("dependencies", [])
        if not isinstance(raw_dependencies, list):
            raise ValueError(f"Task dependencies must be an array: {task_id}")
        task_by_id[task_id] = normalized
        order.append(task_id)
        dependencies[task_id] = [str(item) for item in raw_dependencies]

    known = set(order)
    for task_id, required in dependencies.items():
        unknown = [item for item in required if item not in known]
        if unknown:
            raise ValueError(f"Task {task_id} has unknown dependencies: {unknown}")

    def run_one(task: dict[str, Any], dependency_outputs: dict[str, str]) -> dict[str, Any]:
        local_context = RunContext()
        output, _ = client.create(
            context=local_context,
            role=f"worker:{task['id']}",
            model=LUNA_MODEL,
            effort="high",
            instructions=(
                "Complete exactly the assigned bounded task. Do not redesign the architecture or "
                "expand scope. Provide the concrete deliverable and satisfy its acceptance criteria."
            ),
            input_text=json.dumps(
                {
                    "original_request": case["prompt"],
                    "assigned_task": task,
                    "dependency_outputs": dependency_outputs,
                },
                ensure_ascii=False,
            ),
        )
        return {"task": task, "output": output, "records": local_context.records}

    completed: dict[str, dict[str, Any]] = {}
    pending = set(order)
    results: list[dict[str, Any]] = []
    wave = 0
    while pending:
        ready = [
            task_id for task_id in order
            if task_id in pending and all(item in completed for item in dependencies[task_id])
        ]
        if not ready:
            raise ValueError("Task dependency cycle detected")
        wave += 1
        wave_results: dict[str, dict[str, Any]] = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(max_workers, len(ready))) as pool:
            futures = {
                pool.submit(
                    run_one,
                    task_by_id[task_id],
                    {item: completed[item]["output"] for item in dependencies[task_id]},
                ): task_id
                for task_id in ready
            }
            for future in concurrent.futures.as_completed(futures):
                wave_results[futures[future]] = future.result()
        for task_id in ready:
            result = wave_results[task_id]
            context.records.extend(result.pop("records"))
            result["execution_wave"] = wave
            completed[task_id] = result
            results.append(result)
            pending.remove(task_id)
    return results


def synthesize(
    client: OrchestrationClient,
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
            "Reconcile worker outputs, correct inconsistencies, reject unsupported claims, and "
            "return one complete deliverable satisfying every acceptance criterion."
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


def direct(
    client: OrchestrationClient,
    context: RunContext,
    case: dict[str, Any],
    model: str,
    effort: str,
) -> str:
    output, _ = client.create(
        context=context,
        role="direct",
        model=model,
        effort=effort,
        instructions="Produce a complete implementation-ready answer satisfying every criterion.",
        input_text=json.dumps(case, ensure_ascii=False),
        max_output_tokens=6000,
    )
    return output


def grade(
    client: OrchestrationClient,
    context: RunContext,
    case: dict[str, Any],
    final_output: str,
    model: str,
) -> dict[str, Any]:
    output, _ = client.create(
        context=context,
        role="grader",
        model=model,
        effort="high",
        instructions=(
            "Return only JSON with score, passed, unmet_criteria, critical_errors, and rationale. "
            "Passing requires every acceptance criterion and no critical error."
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
    client: OrchestrationClient,
    case: dict[str, Any],
    variant: str,
    max_workers: int,
) -> dict[str, Any]:
    if max_workers < 1:
        raise ValueError("max_workers must be at least 1")
    context = RunContext()
    started = time.perf_counter()

    if variant == "A":
        final_output = direct(client, context, case, SOL_MODEL, "max")
    elif variant in {"B", "C"}:
        plan = make_plan(client, context, case, SOL_MODEL, "max")
        if variant == "C":
            plan = terra_dispatch(client, context, case, plan)
        worker_results = run_workers(client, context, case, plan, max_workers)
        final_output = synthesize(
            client, context, case, plan, worker_results, SOL_MODEL, "max"
        )
    elif variant == "D":
        plan = make_plan(client, context, case, TERRA_MODEL, "high")
        worker_results = run_workers(client, context, case, plan, max_workers)
        final_output = synthesize(
            client, context, case, plan, worker_results, TERRA_MODEL, "high"
        )
    elif variant == "E":
        final_output = direct(client, context, case, TERRA_MODEL, "high")
    elif variant == "F":
        final_output = direct(client, context, case, LUNA_MODEL, "medium")
    else:
        raise ValueError(f"Unknown variant: {variant}")

    grader_model = TERRA_MODEL if variant in {"A", "B", "C"} else SOL_MODEL
    grade_result = grade(client, context, case, final_output, grader_model)
    return {
        "case_id": case["id"],
        "variant": variant,
        "grade": grade_result,
        "final_output": final_output,
        "wall_seconds": time.perf_counter() - started,
        "summed_call_latency_seconds": context.total_latency,
        "estimated_cost_usd": context.total_cost,
        "calls": [record.__dict__ for record in context.records],
    }
