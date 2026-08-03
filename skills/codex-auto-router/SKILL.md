---
name: codex-auto-router
description: Automatically select and invoke GPT-5.6 Sol, Terra, or Luna for Codex tasks with deterministic local routing, or evaluate bounded Sol/Terra/Luna orchestration through the signed-in Codex CLI. Use when the user asks for Auto model selection, no-API-key routing, route explanations or calibration, or role-based multi-model orchestration.
---

# Route Codex Tasks Automatically

Use one local routing decision followed by one signed-in `codex exec` run. Use the multi-role evaluator only for explicit orchestration or comparative evaluation requests.

## Choose the workflow

- Run a task: `scripts/invoke_auto_task.ps1`.
- Explain without model calls: add `-DryRun` and `-Explain`.
- Calibrate routing: use `scripts/evaluate_auto_router.py`.
- Compare orchestration variants: use `scripts/codex_cli_orchestration_eval.py` with one case first.

This skill deliberately does not add an `Auto` Desktop model, replace `model_provider`, write `model_catalog_json`, or run a credential-forwarding proxy. Keep CC Switch and the user's Codex configuration authoritative.

## Run an Auto task

```powershell
& "<skill-dir>/scripts/invoke_auto_task.ps1" `
  -Task "Implement the requested change" `
  -Strategy balance `
  -Workdir "C:/path/to/workspace" `
  -Explain
```

Strategies:

- `intelligence`: Sol for complex work, Terra otherwise.
- `balance`: Luna for constrained work, Terra by default, Sol for risk or complexity.
- `cost`: Luna by default, Terra for complexity, Sol only for explicit high-risk signals.

The selector makes zero model calls and reuses the active Codex login. Do not claim which authentication method is active unless checked in the current environment.

Default to `workspace-write`. Use `read-only` for analysis or review. Never select `danger-full-access` without explicit approval. Preserve explicit effort; otherwise use `high` for Sol/Terra and `medium` for Luna.

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
- Do not automatically retry timed-out `max` or `xhigh` roles.

## References

- Read `references/entrypoints.md` for invocation and calibration.
- Read `references/router-contract.md` before changing routing boundaries.
