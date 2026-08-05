# Entrypoints

## Signed-in CLI workflow

Use `scripts/invoke_auto_task.ps1`. It classifies locally with zero routing-model calls and launches the selected model through `codex exec`.

```powershell
& "$HOME/.codex/skills/codex-auto-router/scripts/invoke_auto_task.ps1" `
  -Task "Implement the requested change" `
  -Model auto `
  -Strategy balance `
  -Workdir "C:/path/to/repo" `
  -Explain
```

Use `-DryRun` for a route explanation without a model call and `-Json` for JSONL execution output. The explanation includes model, effort, topology, context budget, and bounded escalation eligibility. Task text travels over UTF-8 stdin.

Successful and failed executions record a route outcome in `~/.codex/auto-router/feedback.jsonl`. The outcome deliberately omits task text and execution output. Add `-NoFeedback` to disable collection, `-StateDir` to isolate all learning state, or `-FeedbackFile` to choose a specific JSONL file. Use `-Explain` to display the route ID.

Use `-Model <alias-or-id>` to override Auto for one task. The alias or ID must be enabled in the packaged trusted model registry; explicit-only models may keep `autoEligible: false`. Explicit effort remains authoritative. Auto effort follows tier/risk; an explicit model without `-Effort` uses its registry default.

Explicitly opt into one validation-driven tier escalation only when a deterministic project command can verify the result:

```powershell
& "$HOME/.codex/skills/codex-auto-router/scripts/invoke_auto_task.ps1" `
  -Task "Implement the requested change" `
  -Model auto `
  -Workdir "C:/path/to/repo" `
  -ValidationCommand @('python', '-m', 'unittest', 'discover', '-s', 'tests') `
  -EscalateOnValidationFailure
```

The command is executed as an argv array, not as a shell expression. Escalation is warned, limited to one next-tier attempt after a successful model run fails validation, and unavailable for explicit-model overrides. CLI, authentication, provider, model-availability, sandbox, and network failures are returned without escalation.

The script uses the existing Codex authentication and provider. It does not edit `config.toml`, install a provider, start a proxy, or change CC Switch state.

## Orchestrated execution

Use `scripts/invoke_orchestrated_task.ps1` for a real multi-model task:

```powershell
& "$HOME/.codex/skills/codex-auto-router/scripts/invoke_orchestrated_task.ps1" `
  -Task "Implement the requested change and tests" `
  -Strategy balance `
  -Workdir "C:/path/to/repo" `
  -MaxWorkers 2 `
  -Explain
```

Auto selects A-F. Use `-Variant C` to force Sol planning, Terra dispatch, Luna analysis workers, and Sol implementation/review. Non-final roles always use `read-only`; only `direct` or `reviewer` can receive `workspace-write`. Use `-DryRun` to route without launching models.

Use `-TotalTimeout`, `-MaxModelCalls`, and role-specific effort parameters to bound long runs. Use `-ResultsDir` to persist the route, calls, workspace states, and grade. Progress events are JSON lines on stderr; `-Quiet` suppresses them.

## Offline calibration

```powershell
python "$HOME/.codex/skills/codex-auto-router/scripts/evaluate_auto_router.py" `
  --output "./auto-router-eval.json"
```

The evaluator makes zero model calls.

## Matched efficiency evaluation

Prepare privacy-safe results with one record per case/configuration. Allowed fields are `caseId`, `configuration`, optional `model`/`effort`, external `accepted`, optional observable `tokens`, `durationMs`, and optional `retries`. Do not include prompts or outputs.

```powershell
python "$HOME/.codex/skills/codex-auto-router/scripts/evaluate_development_routes.py" `
  --results "./matched-results.json" `
  --output "./matched-summary.json"
```

The summary reports acceptance, token coverage, observed tokens per accepted case only with complete coverage, and matched pairwise token deltas only where both routes passed. It does not estimate billing cost.

## Model registry validation

After editing `scripts/model_registry.json` or `scripts/orchestration_profiles.json`, validate every model and A-F role without launching models:

```powershell
python "$HOME/.codex/skills/codex-auto-router/scripts/validate_model_registry.py"
```

For a candidate file outside the installed Skill, pass `--registry` and optionally `--profiles`. Validation confirms schema, aliases, roles, tier resolution, the high-risk primary capability, explicit-only models, and the registry digest. It does not prove that the active provider exposes the model; perform a separate controlled `read-only` explicit invocation for that.

## Approval-gated learning

Inspect state and label a route. `status.efficiency` reports token coverage, labeled outcomes, pass rate by final model, and observed tokens per pass only when every labeled route has token telemetry:

```powershell
python "$HOME/.codex/skills/codex-auto-router/scripts/policy_learning.py" status
python "$HOME/.codex/skills/codex-auto-router/scripts/policy_learning.py" label `
  --route-id "<route-id>" --preferred-model gpt-5.6-terra --outcome pass
```

Create and explicitly approve a candidate:

```powershell
python "$HOME/.codex/skills/codex-auto-router/scripts/policy_learning.py" propose `
  --output "./candidate-policy.json"
python "$HOME/.codex/skills/codex-auto-router/scripts/policy_learning.py" approve `
  --candidate "./candidate-policy.json" --approved-by "<reviewer>"
```

On and after the twentieth usable label, `label` automatically writes a candidate under `~/.codex/auto-router/candidates`; use `--no-auto-propose` to suppress it. The explicit `propose` command uses the same deterministic train/validation split and writes a candidate even when it is not approval-eligible. `approve` replays current feedback and rejects stale, tampered, unsafe, or non-improving candidates. Restore the latest previous version explicitly:

```powershell
python "$HOME/.codex/skills/codex-auto-router/scripts/policy_learning.py" rollback `
  --approved-by "<reviewer>"
```

The active policy, audit log, and rollback history live under `~/.codex/auto-router` and survive skill reinstallations.

## Desktop boundary

This workflow does not add `Auto` to the Desktop picker or switch the current conversation model. It starts a separate `codex exec` task with the selected real model, avoiding global provider and history conflicts.
