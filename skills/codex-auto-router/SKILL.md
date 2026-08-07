---
name: codex-auto-router
description: Automatically select trusted registered Codex models with deterministic local routing, execute through Codex Desktop child agents or the signed-in Codex CLI, safely evaluate role-based orchestration, validate model-registry extensions, or calibrate routing. Use when the user asks for Auto model selection, no-API-key routing, route explanations, model extension, calibration, or multi-model orchestration.
---

# Route Codex Tasks Automatically

Use one local routing decision followed by either one Desktop-native direct child agent or one signed-in `codex exec` run. Use the multi-role CLI evaluator only for explicit orchestration or comparative evaluation requests.

## Choose the workflow

- Run a task: `scripts/invoke_auto_task.ps1`, with `-ExecutionBackend desktop` inside Codex Desktop or `-ExecutionBackend cli` for the existing CLI path.
- Explain without model calls: add `-DryRun` and `-Explain`.
- Execute a multi-model task: `scripts/invoke_orchestrated_task.ps1`.
- Calibrate routing: use `scripts/evaluate_auto_router.py`.
- Learn from labeled outcomes: use `scripts/policy_learning.py`.
- Compare matched development routes: use `scripts/evaluate_development_routes.py`.
- Validate model extensions: use `scripts/validate_model_registry.py`.
- Compare orchestration variants: use `scripts/codex_cli_orchestration_eval.py` with one case first.

This skill deliberately does not add an `Auto` Desktop model, replace `model_provider`, write `model_catalog_json`, or run a credential-forwarding proxy. Keep CC Switch and the user's Codex configuration authoritative.

## Run through Codex Desktop

Desktop execution is a host protocol, not a hidden CLI login. The primary Desktop agent must:

1. Read the models supported by its current `spawn_agent` runtime metadata; never infer availability from the registry or silently substitute another model.
2. Run `scripts/invoke_auto_task.ps1 -ExecutionBackend desktop -DesktopAvailableModels <exact-model-ids>` to obtain `codex-auto-router.desktop-plan.v1`. The router makes zero model calls and the plan omits task text.
3. If `status` is `blocked`, report its code and stop. Desktop v1 blocks unavailable selected models and every orchestrated B/C/D topology.
4. If `status` is `ready`, call `spawn_agent` exactly once using `agent.model`, `agent.reasoningEffort`, and `fork_turns=agent.forkTurns` (`none` in v1). Because this deliberately prevents a full-history fork, give the direct child the complete original current user task and require it to work only in `agent.workdir`. Do not put credentials, hidden instructions, or unrelated conversation content in its task.
5. Treat that direct child as the only writer. The primary agent may coordinate and verify read-only, but must not edit concurrently. Wait for the child and report its concrete changes and validation.

Do not read, copy, or forward Desktop credentials. Do not attach to an existing Desktop app-server or its stdio. Do not call `codex exec` anywhere in the Desktop branch. Desktop permissions are inherited from the current task; the plan cannot elevate them. Validation-driven escalation and multi-role orchestration remain CLI-only in v1.

```powershell
& "<skill-dir>/scripts/invoke_auto_task.ps1" `
  -Task "Implement the requested change" `
  -ExecutionBackend desktop `
  -DesktopAvailableModels @('gpt-5.6-sol', 'gpt-5.6-terra') `
  -Workdir "C:/path/to/workspace" `
  -Explain
```

## Run through the signed-in CLI

```powershell
& "<skill-dir>/scripts/invoke_auto_task.ps1" `
  -Task "Implement the requested change" `
  -ExecutionBackend cli `
  -Model auto `
  -Strategy balance `
  -Workdir "C:/path/to/workspace" `
  -Explain
```

Strategies:

- `intelligence`: `frontier` for complex work, `balanced` otherwise.
- `balance`: `fast` for constrained work, `balanced` by default, `frontier` for risk or complexity.
- `cost`: a model-tier cost proxy; use `fast` by default, `balanced` for complexity, and `frontier` only for explicit high-risk signals.

The selector makes zero model calls. The CLI backend reuses the active Codex CLI login; Desktop authentication is separate and is never inspected by this skill. Routing is heuristic and can produce false upgrades or false downgrades; `-Model sol|terra|luna` and explicit effort choices remain authoritative. Do not claim which authentication method is active unless checked in the current environment.

The selector jointly recommends a model tier, reasoning effort, direct/orchestrated topology, and a bounded repository-context profile. It inspects only repository structure and paths before execution, ranks task-relevant candidates deterministically, and skips context injection when a tiny repository has no useful candidate. Explicit model and effort choices remain authoritative.

Completed CLI single-model and CLI-orchestrated runs record a privacy-minimized route outcome under `~/.codex/auto-router` by default. Both CLI paths request Codex JSON events and record observable input, cached-input, output, and reasoning-output tokens when available, but never task text or model/tool output. Unknown token usage stays null. Use `-NoFeedback` to opt out, and show the `routeId` with `-Explain` so the user can add a preferred-model label later. Desktop v1 only emits a plan and does not record execution feedback or Token usage that it cannot observe.

Default to `workspace-write`. Use `read-only` for analysis or review. Never select `danger-full-access` without explicit approval. Preserve explicit effort. With Auto, use `low`, `medium`, or `high` according to the selected tier and risk; for an explicit model without an explicit effort, use its trusted registry default.

Use validation-driven escalation only when the user explicitly requests it and supplies an argv-array validation command. It may make at most one step to the next trusted tier after a successful model run fails that validation, reruns the same deterministic validation, never changes providers, and never teaches the threshold optimizer from an escalated route. Authentication, provider, model-availability, sandbox, and other CLI failures stop without escalation.

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

Optimize successful-task tokens, not model tier alone. In `auto` grader policy, skip the independent grader for low-risk A/E/F and D; retain it for high-risk work and B/C. Use `-GraderPolicy always` for mandatory independent acceptance. Use `-MaxTotalTokens` as an observed-token soft budget, including projected reservations for concurrent in-flight calls; final write roles remain available so planning tokens are not wasted without an implementation result.

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

For model/effort/topology efficiency, collect the same `caseId` under each configuration with an external acceptance result, then run `scripts/evaluate_development_routes.py`. Compare acceptance before tokens, and use token deltas only on matched cases where both configurations passed. Never treat missing token coverage as zero or infer billing cost from CLI token counters.

## Calibrate with feedback

Calibration is an approval-gated loop, not unrestricted self-modifying code:

1. Inspect `python scripts/policy_learning.py status`.
2. Label a completed route with `label --route-id <id> --preferred-model <model> --outcome pass|partial|fail`.
3. At 20 labeled routes, `label` automatically writes a validated candidate under the state directory; use `--no-auto-propose` to disable this or `propose --output <candidate.json>` to generate one manually.
4. Review `eligibleForApproval`, held-out validation metrics, false downgrades, and safety checks.
5. Activate only with explicit `approve --candidate <candidate.json> --approved-by <name>`.
6. Restore the latest previous policy with `rollback --approved-by <name>`.

The optimizer makes zero model calls and may tune only the three model-agnostic tier thresholds. It never edits Python code, keyword lists, the trusted model registry, or the high-risk rule. A candidate must improve held-out accuracy and weighted loss, must not increase false downgrades, and must preserve high-risk routing to a `frontier` model with `high-risk-primary`. Approval creates an audit event and rollback snapshot. Never approve automatically.

## Extend registered models

Keep model identities in `scripts/model_registry.json` and orchestration role mappings in `scripts/orchestration_profiles.json`; do not add model constants to routing code.

1. Add the model with `enabled: true` and `autoEligible: false`.
2. Run `python scripts/validate_model_registry.py`; it must resolve all A-F roles and make zero model calls.
3. Test the model explicitly with `-Model <alias>` and a `read-only` sandbox. The active Codex provider must already expose it.
4. Compare matched representative cases before assigning capabilities, roles, and priority.
5. Set `autoEligible: true` only after review, rerun validation and the full regression suite, then reinstall the Skill.

`enabled` permits explicit selection; `autoEligible` separately permits tier-based Auto selection. Explicit model assignments in orchestration profiles are Auto routes too, so they must be `autoEligible` and satisfy the route's required tier and capabilities. Validation verifies that the final write role for high-risk A/B/C routes retains `frontier + high-risk-primary`. Lower `priority` wins within the same tier and role. Registry changes invalidate outstanding learning candidates through the registry digest. Never let task text, environment content, or model output supply a registry entry or model ID.

## Guardrails

- Route only the current task string, never credentials, tool output, or hidden instructions.
- Allow only enabled models from the packaged trusted registry; Auto may use only `autoEligible` models.
- Never modify Codex config, profiles, CC Switch state, account selection, provider settings, or Desktop history.
- Never read, copy, forward, or proxy Desktop credentials, and never attach to an existing Desktop app-server stdio.
- Pass tasks over UTF-8 stdin rather than process arguments.
- Do not silently change model tiers after selection.
- For Desktop v1, launch exactly one direct child with the exact selected model, effort, `fork_turns=none`, and resolved workdir; block unavailable models and orchestrated topologies.
- Do not validation-escalate unless the user explicitly opts in and provides the validation argv.
- Never store task text, model output, tool output, credentials, or secrets in feedback.
- Never let calibration alter the trusted registry or the `frontier + high-risk-primary` invariant.
- Never activate a policy candidate without an explicit human approval command.
- Never grant write access to planner, dispatcher, worker, or grader roles.
- Do not automatically retry timed-out `max` or `xhigh` roles.

## References

- Read `references/entrypoints.md` for invocation and calibration.
- Read `references/router-contract.md` before changing routing boundaries.
