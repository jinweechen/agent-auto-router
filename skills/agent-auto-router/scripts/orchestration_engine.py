from __future__ import annotations

import concurrent.futures
import json
import pathlib
import re
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

from model_registry import ModelRegistry, load_model_registry
from orchestration_profiles import OrchestrationProfiles, load_orchestration_profiles

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
    cached_input_tokens: int = 0
    reasoning_output_tokens: int = 0


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

    @property
    def total_input_tokens(self) -> int:
        return sum(record.input_tokens for record in self.records)

    @property
    def total_output_tokens(self) -> int:
        return sum(record.output_tokens for record in self.records)

    @property
    def total_tokens(self) -> int:
        return self.total_input_tokens + self.total_output_tokens

    @property
    def total_cached_input_tokens(self) -> int:
        return sum(record.cached_input_tokens for record in self.records)

    @property
    def total_uncached_input_tokens(self) -> int:
        return max(0, self.total_input_tokens - self.total_cached_input_tokens)

    @property
    def total_reasoning_output_tokens(self) -> int:
        return sum(record.reasoning_output_tokens for record in self.records)


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
    max_tasks: int = 3,
) -> dict[str, Any]:
    output, _ = client.create(
        context=context,
        role="planner",
        model=model,
        effort=effort,
        instructions=(
            "Decompose the request into independent bounded tasks. Return only JSON with summary "
            "and tasks. Each task requires id, description, dependencies, and acceptance_criteria. "
            f"Return no more than {max_tasks} tasks. Keep descriptions and criteria concise."
        ),
        input_text=json.dumps(
            {
                "request": case["prompt"],
                "acceptance_criteria": case["acceptance_criteria"],
                "repository_context": case.get("repository_context"),
            },
            ensure_ascii=False,
        ),
        max_output_tokens=1200,
    )
    plan = parse_json_object(output)
    if not isinstance(plan.get("tasks"), list) or not plan["tasks"]:
        raise ValueError("Planner returned no tasks")
    if len(plan["tasks"]) > max_tasks:
        raise ValueError(f"Planner returned more than {max_tasks} tasks")
    return plan


def dispatch_plan(
    client: OrchestrationClient,
    context: RunContext,
    case: dict[str, Any],
    plan: dict[str, Any],
    max_tasks: int,
    model: str,
    effort: str,
) -> dict[str, Any]:
    output, _ = client.create(
        context=context,
        role="dispatcher",
        model=model,
        effort=effort,
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
        max_output_tokens=1000,
    )
    normalized = parse_json_object(output)
    if not isinstance(normalized.get("tasks"), list) or not normalized["tasks"]:
        raise ValueError("Dispatcher returned no tasks")
    if len(normalized["tasks"]) > max_tasks:
        raise ValueError(f"Dispatcher returned more than {max_tasks} tasks")
    return normalized


def run_workers(
    client: OrchestrationClient,
    context: RunContext,
    case: dict[str, Any],
    plan: dict[str, Any],
    max_workers: int,
    model: str | None = None,
    effort: str = "high",
) -> list[dict[str, Any]]:
    if max_workers < 1:
        raise ValueError("max_workers must be at least 1")
    if model is None:
        model = load_model_registry().resolve_tier("fast", role="worker").model_id
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
            model=model,
            effort=effort,
            instructions=(
                "Complete exactly the assigned bounded task. Do not redesign the architecture or "
                "expand scope. Return concise implementation findings, file-level guidance, and "
                "acceptance evidence without repeating the original request."
            ),
            max_output_tokens=1800,
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
    execution_mode: bool = False,
) -> str:
    instructions = (
        "Inspect the workspace, reconcile worker findings, implement the requested changes, "
        "run appropriate validation, and report changed files and remaining risks. Do not only "
        "return a proposed patch."
        if execution_mode
        else "Reconcile worker outputs, correct inconsistencies, reject unsupported claims, and "
        "return one complete deliverable satisfying every acceptance criterion."
    )
    compact_results = [
        {
            "task_id": result["task"].get("id"),
            "description": result["task"].get("description"),
            "acceptance_criteria": result["task"].get("acceptance_criteria", []),
            "output": result["output"][:6000],
        }
        for result in worker_results
    ]
    output, _ = client.create(
        context=context,
        role="reviewer",
        model=model,
        effort=effort,
        instructions=instructions,
        input_text=json.dumps(
            {
                "request": case["prompt"],
                "acceptance_criteria": case["acceptance_criteria"],
                "plan": plan,
                "worker_results": compact_results,
            },
            ensure_ascii=False,
        ),
        max_output_tokens=4000,
    )
    return output


def direct(
    client: OrchestrationClient,
    context: RunContext,
    case: dict[str, Any],
    model: str,
    effort: str,
    execution_mode: bool = False,
) -> str:
    output, _ = client.create(
        context=context,
        role="direct",
        model=model,
        effort=effort,
        instructions=(
            "Inspect the workspace, implement the task, run appropriate validation, and report "
            "changed files and remaining risks."
            if execution_mode
            else "Produce a complete implementation-ready answer satisfying every criterion."
        ),
        input_text=json.dumps(case, ensure_ascii=False),
        max_output_tokens=5000,
    )
    return output


def grade(
    client: OrchestrationClient,
    context: RunContext,
    case: dict[str, Any],
    final_output: str,
    model: str,
    effort: str = "high",
    execution_mode: bool = False,
) -> dict[str, Any]:
    output, _ = client.create(
        context=context,
        role="grader",
        model=model,
        effort=effort,
        instructions=(
            "Inspect the resulting workspace and candidate report. Return only JSON with score, "
            "passed, unmet_criteria, critical_errors, and rationale. Passing requires every "
            "acceptance criterion and no critical error. Do not modify files."
            if execution_mode
            else "Return only JSON with score, passed, unmet_criteria, critical_errors, and "
            "rationale. Passing requires every acceptance criterion and no critical error."
        ),
        input_text=json.dumps(
            {
                "request": case["prompt"],
                "acceptance_criteria": case["acceptance_criteria"],
                "candidate": final_output[-4000:] if execution_mode else final_output,
            },
            ensure_ascii=False,
        ),
        max_output_tokens=800,
    )
    return parse_json_object(output)


def run_variant(
    client: OrchestrationClient,
    case: dict[str, Any],
    variant: str,
    max_workers: int,
    execution_mode: bool = False,
    grade_enabled: bool = True,
    worker_task_limit: int | None = None,
    registry: ModelRegistry | None = None,
    profiles: OrchestrationProfiles | None = None,
    required_capabilities: tuple[str, ...] = (),
    backends: tuple[str, ...] | None = None,
    allow_explicit_only: bool = False,
) -> dict[str, Any]:
    if max_workers < 1:
        raise ValueError("max_workers must be at least 1")
    context = RunContext()
    started = time.perf_counter()
    active_registry = registry or load_model_registry()
    active_profiles = profiles or load_orchestration_profiles()
    resolved_roles: dict[str, dict[str, str]] = {}

    final_role = "direct" if variant in {"A", "E", "F"} else "reviewer"

    def resolve(role: str) -> tuple[str, str]:
        assignment = active_profiles.assignment(variant, role)
        final_requirements = required_capabilities if role == final_role else ()
        model_spec = assignment.resolve(
            active_registry,
            role,
            required_capabilities=final_requirements,
            required_tier="frontier" if final_requirements else None,
            backends=backends,
            allow_explicit_only=allow_explicit_only,
        )
        resolved_roles[role] = {
            "model": model_spec.model_id,
            "tier": model_spec.tier,
            "effort": assignment.effort,
            "backend": model_spec.backend,
        }
        return model_spec.model_id, assignment.effort

    if variant == "A":
        direct_model, direct_effort = resolve("direct")
        final_output = direct(client, context, case, direct_model, direct_effort, execution_mode)
    elif variant in {"B", "C"}:
        planner_model, planner_effort = resolve("planner")
        plan = make_plan(
            client, context, case, planner_model, planner_effort, worker_task_limit or 3
        )
        if variant == "C":
            dispatcher_model, dispatcher_effort = resolve("dispatcher")
            plan = dispatch_plan(
                client, context, case, plan, worker_task_limit or 3,
                dispatcher_model, dispatcher_effort,
            )
        worker_model, worker_effort = resolve("worker")
        worker_results = run_workers(
            client, context, case, plan, max_workers, worker_model, worker_effort
        )
        reviewer_model, reviewer_effort = resolve("reviewer")
        final_output = synthesize(
            client, context, case, plan, worker_results,
            reviewer_model, reviewer_effort, execution_mode,
        )
    elif variant == "D":
        planner_model, planner_effort = resolve("planner")
        plan = make_plan(
            client, context, case, planner_model, planner_effort, worker_task_limit or 3
        )
        worker_model, worker_effort = resolve("worker")
        worker_results = run_workers(
            client, context, case, plan, max_workers, worker_model, worker_effort
        )
        reviewer_model, reviewer_effort = resolve("reviewer")
        final_output = synthesize(
            client, context, case, plan, worker_results,
            reviewer_model, reviewer_effort, execution_mode,
        )
    elif variant == "E":
        direct_model, direct_effort = resolve("direct")
        final_output = direct(client, context, case, direct_model, direct_effort, execution_mode)
    elif variant == "F":
        direct_model, direct_effort = resolve("direct")
        final_output = direct(client, context, case, direct_model, direct_effort, execution_mode)
    else:
        raise ValueError(f"Unknown variant: {variant}")

    if grade_enabled:
        grader_model, grader_effort = resolve("grader")
        grading_status = "completed"
        try:
            grade_result = grade(
                client, context, case, final_output, grader_model, grader_effort, execution_mode
            )
        except Exception as exc:
            grading_status = "failed"
            grade_result = {
                "score": None,
                "passed": False,
                "unmet_criteria": [],
                "critical_errors": [f"grader_failed: {type(exc).__name__}: {exc}"],
                "rationale": "Implementation completed, but the grader did not return a valid result.",
            }
    else:
        grading_status = "skipped"
        grade_result = {
            "score": None,
            "passed": None,
            "unmet_criteria": [],
            "critical_errors": [],
            "rationale": "Independent grading skipped by the token-saving execution policy.",
        }
    return {
        "case_id": case["id"],
        "variant": variant,
        "registry_source": active_registry.source,
        "profile_source": active_profiles.source,
        "resolved_roles": resolved_roles,
        "grade": grade_result,
        "implementation_status": "completed",
        "grading_status": grading_status,
        "final_output": final_output,
        "wall_seconds": time.perf_counter() - started,
        "summed_call_latency_seconds": context.total_latency,
        "estimated_cost_usd": context.total_cost,
        "tokens": {
            "input": context.total_input_tokens,
            "cached_input": context.total_cached_input_tokens,
            "uncached_input": context.total_uncached_input_tokens,
            "output": context.total_output_tokens,
            "reasoning_output": context.total_reasoning_output_tokens,
            "total": context.total_tokens,
        },
        "calls": [record.__dict__ for record in context.records],
    }
