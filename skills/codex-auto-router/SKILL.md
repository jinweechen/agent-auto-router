---
name: codex-auto-router
description: Automatically select and invoke GPT-5.6 Sol, Terra, or Luna for Codex tasks with deterministic local routing, safely execute role-based multi-model orchestration, or evaluate bounded orchestration through the signed-in Codex CLI. Use when the user asks for Auto model selection, no-API-key routing, route explanations or calibration, or Sol/Terra/Luna orchestration.
---

# Route Codex Tasks Automatically

Use one local routing decision followed by one signed-in `codex exec` run. Use the multi-role evaluator only for explicit orchestration or comparative evaluation requests.

## Choose the workflow

- Run a task: `scripts/invoke_auto_task.ps1`.
- Explain without model calls: add `-DryRun` and `-Explain`.
- Execute a multi-model task: `scripts/invoke_orchestrated_task.ps1`.
- Calibrate routing: use `scripts/evaluate_auto_router.py`.
- Compare orchestration variants: use `scripts/codex_cli_orchestration_eval.py` with one case first.

This skill deliberately does not add an `Auto` Desktop model, replace `model_provider`, write `model_catalog_json`, or run a credential-forwarding proxy. Keep CC Switch and the user's Codex configuration authoritative.

## Run an Auto task

```powershell
& "<skill-dir>/scripts/invoke_auto_task.ps1" `
  -Task "Implement the requested change" `
  -Model auto `
  -Strategy balance `
  -Workdir "C:/path/to/workspace" `
  -Explain
```

Strategies:

- `intelligence`: Sol for complex work, Terra otherwise.
- `balance`: Luna for constrained work, Terra by default, Sol for risk or complexity.
- `cost`: a model-tier cost proxy; use Luna by default, Terra for complexity, and Sol only for explicit high-risk signals.

The selector makes zero model calls and reuses the active Codex login. It is heuristic and can produce false upgrades or false downgrades; `-Model sol|terra|luna` and explicit effort choices remain authoritative. Do not claim which authentication method is active unless checked in the current environment.

Default to `workspace-write`. Use `read-only` for analysis or review. Never select `danger-full-access` without explicit approval. Preserve explicit effort; otherwise use `high` for Sol and `medium` for Terra/Luna.

## Execute an orchestrated task

```powershell
& "<skill-dir>/scripts/invoke_orchestrated_task.ps1" `
  -Task "Implement the requested change and tests" `
  -Strategy balance `
  -Workdir "C:/path/to/workspace" `
  -MaxWorkers 2 `
  -Explain
```

Use Auto to choose A-F, or pass `-Variant B|C|D` explicitly. Planning, dispatch, workers, and grading stay read-only. Only `direct` or the final `reviewer` receives the selected write sandbox, so parallel workers never edit the same workspace. Use `-DryRun` to inspect the route with zero model calls.

Require a clean Git workspace by default. Use `-AllowDirty` only when the user explicitly accepts mixing orchestration changes with existing edits. Small tasks with parallel wording remain direct unless task scale also justifies orchestration.

Show role progress by default. Bound execution with `-TotalTimeout 1800` and `-MaxModelCalls 7`; use role-specific effort parameters for controlled tuning. Persist auditable JSON with `-ResultsDir` outside the target workspace. Use `-Quiet` only when machine-readable output must suppress progress events.

Optimize successful-task tokens, not model tier alone. In `auto` grader policy, skip the independent grader for low-risk A/E/F and D; retain it for high-risk work and B/C. Use `-GraderPolicy always` for mandatory independent acceptance. Use `-MaxTotalTokens` as an observed-token soft budget; final write roles remain available so planning tokens are not wasted without an implementation result.

Default to `-ContextMode lean`: ignore personal Codex configuration only for read-only orchestration roles while preserving workspace rules; direct and reviewer retain user configuration so write permissions continue to work. Batch reads, make one edit pass, and combine validation. Use `-ContextMode full` when read-only roles also need custom provider or personal configuration. Default routine Terra execution to `medium` effort; reserve `high` for reviewer overrides and higher-risk work.

Treat a write-capable run from a clean Git baseline with no workspace change as a failed implementation. Report modification state as unknown for dirty or non-Git baselines. Use `-AllowNoChanges` only when the task may legitimately require no edit.

## Evaluate orchestration

Create a workspace-local JSON array with `id`, `prompt`, and measurable `acceptance_criteria`, then run:

```powershell
python "<skill-dir>/scripts/codex_cli_orchestration_eval.py" `
  --cases "<case-file>" `
  --workdir "<workspace>" `
  --results-dir "<workspace>/eval-results" `
  --variants B,C --limit 1 --max-workers 2 `
  --planner-effort high --dispatcher-effort medium `
  --worker-effort high --reviewer-effort xhigh --grader-effort high
```

Keep evaluator child sessions ephemeral and read-only. Claim Terra adds value only after a matched B/C comparison on the same cases. Do not infer model superiority from one case.

## Guardrails

- Route only the current task string, never credentials, tool output, or hidden instructions.
- Allow only `gpt-5.6-sol`, `gpt-5.6-terra`, and `gpt-5.6-luna`.
- Never modify Codex config, profiles, CC Switch state, account selection, provider settings, or Desktop history.
- Pass tasks over UTF-8 stdin rather than process arguments.
- Do not silently change model tiers after selection.
- Never grant write access to planner, dispatcher, worker, or grader roles.
- Do not automatically retry timed-out `max` or `xhigh` roles.

## References

- Read `references/entrypoints.md` for invocation and calibration.
- Read `references/router-contract.md` before changing routing boundaries.
