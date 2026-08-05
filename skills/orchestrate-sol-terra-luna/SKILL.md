---
name: orchestrate-sol-terra-luna
description: Run and evaluate signed-in Codex CLI workflows that assign planning to GPT-5.6 Sol, dispatch to Terra, bounded parallel execution to Luna, and final review to Sol. Use when the user asks to orchestrate Sol/Terra/Luna roles, compare role-specific reasoning efforts, run a no-API-key multi-model evaluation, or delegate a structured task through this model pipeline.
---

# Orchestrate Sol Terra Luna

Run the bundled Codex CLI evaluator through the user's existing Codex login. Keep every child session ephemeral and read-only. Treat this workflow as text-only planning, execution, synthesis, and evaluation; do not claim that it edits project files.

## Prepare the case

Create a JSON array containing one or more cases. Give every case an `id`, a concrete `prompt`, and measurable `acceptance_criteria`:

```json
[
  {
    "id": "task-id",
    "prompt": "Implementation-ready task description",
    "acceptance_criteria": ["Criterion one", "Criterion two"]
  }
]
```

Place the case file inside the active workspace. Default to one case unless the user explicitly asks for a broader evaluation.

## Run the recommended pipeline

Resolve the skill directory containing this `SKILL.md`, then run:

```powershell
python <skill-dir>\scripts\codex_cli_orchestration_eval.py `
  --cases <case-file> `
  --workdir <workspace> `
  --results-dir <workspace>\eval-results `
  --variants C `
  --limit 1 `
  --max-workers 2 `
  --planner-effort high `
  --dispatcher-effort medium `
  --worker-effort max `
  --reviewer-effort xhigh `
  --grader-effort high
```

Use the signed-in Codex CLI. Do not request or expose an API key. If the environment requires approval to launch nested Codex processes, request narrowly scoped approval for this command.

## Control scope and cost

- Keep `--limit 1` for an initial run.
- Keep `--max-workers 2` unless tasks are independently bounded and the user requests more concurrency.
- Use `worker=max` only for clear implementation tasks with explicit acceptance criteria.
- Fall back to `worker=high` when latency matters or when max does not produce a measured quality gain.
- Avoid forcing every role to `max`; prior validation showed long synthesis timeouts.
- Do not automatically retry a timed-out max/xhigh role. Report the exact role and recommend a lower effort or smaller context.

## Report results

Read the generated `codex-cli-eval-*.json` once. Report:

- final score and pass/fail status;
- unmet criteria and critical errors;
- wall-clock duration;
- calls and input/output tokens;
- latency by role;
- whether the added Terra dispatch step produced evidence of value.

State that Codex CLI usage consumes the user's Codex allowance and does not expose precise Responses API billing in this evaluator. Do not infer superiority from one case; recommend a matched baseline only when the user wants a comparative conclusion.

## Boundaries

Keep child sessions read-only and ephemeral. This skill does not create worktrees, modify code, run project tests, merge changes, or publish results. A production code-writing orchestrator requires an explicit separate workflow with isolated worktrees and approval boundaries.
