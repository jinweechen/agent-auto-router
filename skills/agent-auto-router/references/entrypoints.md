# Entrypoints

## Contents

- [Codex Desktop workflow](#codex-desktop-workflow)
- [Signed-in CLI workflow](#signed-in-cli-workflow)
- [Orchestrated execution](#orchestrated-execution)
- [Offline calibration](#offline-calibration)
- [Matched efficiency evaluation](#matched-efficiency-evaluation)
- [Model registry validation](#model-registry-validation)
- [Manual and guarded automatic learning](#manual-and-guarded-automatic-learning)
- [Generic host plan](#generic-host-plan)
- [Conversation boundary](#conversation-boundary)

## Codex Desktop workflow

Inside Codex Desktop, use `scripts/invoke_auto_task.ps1` as a planner and set the runtime boundary explicitly:

```powershell
& "$HOME/.codex/skills/agent-auto-router/scripts/invoke_auto_task.ps1" `
  -Task "Implement the requested change" `
  -ExecutionBackend desktop `
  -DesktopAvailableModels @('gpt-5.6-sol', 'gpt-5.6-terra') `
  -DesktopMaxParallelChildren 3 `
  -HostPermissionsJson $currentTurnPermissionsJson `
  -Workdir "C:/path/to/repo" `
  -Explain
```

The available-model list, maximum parallel child count, and permission snapshot must come from the current Desktop runtime. `DesktopMaxParallelChildren` counts child slots and excludes the primary coordinator. Build the permission snapshot as `agent-auto-router.host-permissions.v1` from trusted turn metadata, including `sandbox`, `approvalPolicy`, `networkAccess`, `writableRoots`, optional `profileId`, and `canRequestPermissions`. Never derive these values from the task or untrusted environment variables.

The commands below reference model IDs as they appear in the Desktop runtime (bare IDs such as `gpt-5.6-sol`) or CLI (`codex exec --model`). Internally, the router uses backend-qualified IDs. The command prints one `agent-auto-router.desktop-plan.v3` JSON object and makes no model or CLI call. The plan excludes the task body while carrying the normalized workdir, role models, staged dependencies, effective permissions, call budget, idempotency keys, and lifecycle states.

For `executionRequested=false`, report the plan and launch nothing. For `status=blocked`, launch nothing and report the structured reason. Otherwise the primary executes dependency-ready `stages`: serial planner/dispatcher, bounded parallel workers, one final reviewer, and an optional read-only grader. A/E/F usually contain only `direct`; high-risk direct work may add a grader. Use the exact role model, effort, `forkTurns=none`, workdir, and instance bounds. The primary provides the original task and upstream results because children receive no full-history fork.

Before every non-writer stage, snapshot workspace state and explicitly prohibit edits. Stop if it changes. Acquire `coordination.writerClaim` only for the declared `hostContract.onlyWriter`, after its dependencies succeed. Record the required lifecycle events without task text or child output, never reuse an `idempotencyKeyTemplate` instance, and never exceed `callBudget.maximum` or `hostContract.maxParallelAgents`.

Before the first spawn, run `desktop_workspace_snapshot.py capture --workdir <workdir> --output <protected-baseline>`, adding one `--forbidden-root` for every effective child-writable root other than the workdir, which is always forbidden automatically. Keep the baseline outside every child-writable root. It records tracked and non-ignored untracked path identity using type, mode, size, SHA-256 content, and Git porcelain status; this detects a second edit to a file that was already dirty. Initialize a map of returned child IDs, idempotency keys, deadlines, and terminal evidence. Enforce each agent's `timeoutMs` and the wall-clock `coordination.timeoutPolicy.totalTimeoutMs`, starting at the first spawn. A final-status notification, completed child thread, or terminal tool result is authoritative terminal evidence. Treat `list_agents` as advisory: if it reports `running` after an authoritative terminal signal, record stale status, do not wait indefinitely, and never relaunch that key.

Wrap the entire DAG in `try/finally`. On a stage deadline, mark the instance `timed_out`, interrupt it once, and stop dependent stages. On the total deadline, mark the run timed out, interrupt every active child, and mark unstarted dependent stages `blocked`. After an interrupt, wait at most `interruptGraceTimeoutMs` for authoritative terminal evidence and reconcile again. Record a late child terminal but preserve the run/stage timeout outcome; mark a child `orphaned` only when the grace deadline expires without authoritative terminal evidence. In `finally`, skip already-terminal IDs and release the writer claim. Complete terminal reconciliation before spawning a dependent stage and again before the final response.

After cleanup, run `desktop_workspace_snapshot.py compare --workdir <workdir> --baseline <protected-baseline>` with the same `--forbidden-root` values. The tool rejects a writable baseline before reading it. Treat `runChangedPaths` and `runChangedFileCount` as the authoritative current-run result. Report `preexistingDirtyPaths` and `finalDirtyPaths` separately so a dirty worktree neither inflates the run count nor hides a second edit to an already modified file. Child patch events and self-reported file counts are advisory and must not be summed. Report the reconciled paths/count explicitly because Codex UI attribution can remain per child thread.

After a non-DryRun execution terminates, use `learning.route` unchanged, add a unique report ID and trusted host name, and fill the required result metadata from the actual run. Submit `agent-auto-router.execution-report.v1` to `guarded_auto.py report --stdin`. Do not submit task text, agent output, tool output, or a DryRun. Report ingestion is idempotent and advances guarded learning with zero model calls.

Desktop `-DryRun` still requires all runtime metadata and emits the same plan schema with `executionRequested=false`, `plannedAgentCalls=0`, and `hostContract.action=report_plan`. `wouldPlanAgentCalls` retains the non-executable minimum/maximum estimate. Lifecycle, timeout, cleanup, and workspace-reconciliation fields remain visible so the protocol can be audited without launching children. `-Json` and `-NoFeedback` are idempotent: Desktop output is already JSON and Desktop v3 never records child output. `-FeedbackFile`, validation commands/escalation, and `-ContextMode full` are rejected as CLI-only or unsupported. Desktop never accesses credentials or app-server stdio and never falls back to CLI, another provider, lower model tier, effort, or permission level. A non-direct role may use a same-or-higher-tier runtime model only when the plan explicitly preserves `preferredModel` and declares `modelResolution=runtime-tier-upgrade`.

## Signed-in CLI workflow

Use `scripts/invoke_auto_task.ps1 -ExecutionBackend cli`. It classifies locally with zero routing-model calls and launches the selected model through `codex exec`.

```powershell
& "$HOME/.codex/skills/agent-auto-router/scripts/invoke_auto_task.ps1" `
  -Task "Implement the requested change" `
  -ExecutionBackend cli `
  -Model auto `
  -HostPermissionsJson $currentHostPermissionsJson `
  -Strategy balance `
  -Workdir "C:/path/to/repo" `
  -Explain
```

Use `-DryRun` for a route explanation without a model call and `-Json` for JSONL execution output. The explanation includes model, effort, topology, context budget, and bounded escalation eligibility. Task text travels over UTF-8 stdin.

Successful and failed executions record a route outcome in `~/.codex/auto-router/feedback.jsonl`. The outcome deliberately omits task text and execution output. Add `-NoFeedback` to disable collection, `-StateDir` to isolate all learning state, or `-FeedbackFile` to choose a specific JSONL file. Use `-Explain` to display the route ID.

Use `-Model <alias-or-id>` to override Auto for one Codex CLI task. The alias or ID must be an enabled Codex model in the packaged trusted registry. Use the orchestrated entrypoint with an explicit `-Backend`, or the generic host plan, for Claude Code and other backends. Explicit effort remains authoritative; an explicit model without `-Effort` uses its registry default.

Explicitly opt into one validation-driven tier escalation only when a deterministic project command can verify the result:

```powershell
& "$HOME/.codex/skills/agent-auto-router/scripts/invoke_auto_task.ps1" `
  -Task "Implement the requested change" `
  -Model auto `
  -HostPermissionsJson $currentHostPermissionsJson `
  -Workdir "C:/path/to/repo" `
  -ValidationCommand @('python', '-m', 'unittest', 'discover', '-s', 'tests') `
  -EscalateOnValidationFailure
```

The command is executed as an argv array, not as a shell expression. Escalation is warned, limited to one next-tier attempt after a successful model run fails validation, and unavailable for explicit-model overrides. CLI, authentication, provider, model-availability, sandbox, and network failures are returned without escalation.

The script uses the existing Codex authentication and provider. It does not edit `config.toml`, install a provider, start a proxy, or change CC Switch state.

## Orchestrated execution

Use `scripts/invoke_orchestrated_task.ps1` for a real multi-model task:

```powershell
& "$HOME/.codex/skills/agent-auto-router/scripts/invoke_orchestrated_task.ps1" `
  -Task "Implement the requested change and tests" `
  -Strategy balance `
  -HostPermissionsJson $currentHostPermissionsJson `
  -Workdir "C:/path/to/repo" `
  -MaxWorkers 2 `
  -Explain
```

Auto selects A-F. Use `-Variant C` to force Sol planning, Terra dispatch, Luna analysis workers, and Sol implementation/review. Non-final roles always use `read-only`; only `direct` or `reviewer` can inherit a write-capable sandbox. One orchestration run uses one backend; explicit single `-Backend` also allows that backend's explicit-trial models inside Auto tier resolution. Use `-DryRun` to route without launching models.

Use `-TotalTimeout`, `-MaxModelCalls`, and role-specific effort parameters to bound long runs. Use `-ResultsDir` to persist the route, calls, workspace states, and grade. Progress events are JSON lines on stderr; `-Quiet` suppresses them.

## Offline calibration

```powershell
python "$HOME/.codex/skills/agent-auto-router/scripts/evaluate_auto_router.py" `
  --output "./auto-router-eval.json"
```

The evaluator makes zero model calls.

## Matched efficiency evaluation

Prepare privacy-safe results with one record per case/configuration. Allowed fields are `caseId`, `configuration`, optional `model`/`effort`, external `accepted`, optional observable `tokens`, `durationMs`, and optional `retries`. Do not include prompts or outputs.

```powershell
python "$HOME/.codex/skills/agent-auto-router/scripts/evaluate_development_routes.py" `
  --results "./matched-results.json" `
  --output "./matched-summary.json"
```

The summary reports acceptance, token coverage, observed tokens per accepted case only with complete coverage, and matched pairwise token deltas only where both routes passed. It does not estimate billing cost.

## Model registry validation

After editing `scripts/model_registry.json` or `scripts/orchestration_profiles.json`, validate every model and A-F role without launching models:

```powershell
python "$HOME/.codex/skills/agent-auto-router/scripts/validate_model_registry.py"
```

For a candidate file outside the installed Skill, pass `--registry` and optionally `--profiles`. Validation confirms schema, aliases, roles, tier resolution, the high-risk primary capability, explicit-only models, and the registry digest. It does not prove that the active provider exposes the model; perform a separate controlled `read-only` explicit invocation for that.

## Manual and guarded automatic learning

Inspect state and label a route. `status.efficiency` reports token coverage, labeled outcomes, pass rate by final model, and observed tokens per pass only when every labeled route has token telemetry:

```powershell
python "$HOME/.codex/skills/agent-auto-router/scripts/policy_learning.py" status
python "$HOME/.codex/skills/agent-auto-router/scripts/policy_learning.py" label `
  --route-id "<route-id>" --preferred-model gpt-5.6-terra --outcome pass
```

Create and explicitly approve a candidate:

```powershell
python "$HOME/.codex/skills/agent-auto-router/scripts/policy_learning.py" propose `
  --output "./candidate-policy.json"
python "$HOME/.codex/skills/agent-auto-router/scripts/policy_learning.py" approve `
  --candidate "./candidate-policy.json" --approved-by "<reviewer>"
```

On and after the twentieth usable label, `label` automatically writes a candidate under `~/.codex/auto-router/candidates`; use `--no-auto-propose` to suppress it. The explicit `propose` command uses the same deterministic train/validation split and writes a candidate even when it is not approval-eligible. `approve` replays current feedback and rejects stale, tampered, unsafe, or non-improving candidates, including candidates built against an older benchmark-prior snapshot. Restore the latest previous version explicitly:

```powershell
python "$HOME/.codex/skills/agent-auto-router/scripts/policy_learning.py" rollback `
  --approved-by "<reviewer>"
```

The active policy, audit log, and rollback history live under `~/.codex/auto-router` and survive skill reinstallations.

Manual mode remains the default. To authorize the narrow guarded loop once, inspect its configuration and enable it explicitly:

```powershell
python "$HOME/.codex/skills/agent-auto-router/scripts/guarded_auto.py" status
python "$HOME/.codex/skills/agent-auto-router/scripts/guarded_auto.py" configure `
  --mode guarded-auto
python "$HOME/.codex/skills/agent-auto-router/scripts/guarded_auto.py" cycle --dry-run
```

CLI outcome recording automatically runs one cycle. The loop accepts only explicit user selections or deterministic validation-proven adjacent-tier escalations, may only lower one threshold by one step, and must pass held-out evaluation, deterministic canary, and probation before stabilizing. Registry, benchmark, candidate, or active-policy drift fails closed; verified regression rejects or rolls back automatically. Disable future automatic transitions with `configure --mode manual`. See `guarded-auto-learning.md` for the report schema and full boundary. Cost-saving threshold increases and every protected surface remain manual and approval-gated.

Keep `--state-dir` and any `--feedback-file` outside every child-writable root. The CLI entrypoints check this before a non-DryRun launch and block guarded mode under full access or an unverifiable external sandbox; tighten the child to a protected workspace boundary or disable guarded mode rather than letting a model write its own evidence.

## Conversation boundary

Neither backend adds `Auto` to the Desktop picker or switches the current conversation model. CLI starts separate signed-in CLI tasks. Desktop starts only the bounded child calls declared by a ready v3 plan and otherwise blocks explicitly.

## Generic host plan

Emits `agent-auto-router.host-plan.v2` for Codex, Claude Code, and other capable hosts. A host may execute the task itself, invoke a declared CLI backend, or run multi-role orchestration. Automatic execution requires a trusted permission snapshot and includes the normalized effective permissions in both the host contract and action.

```powershell
python "<skill-dir>/scripts/host_execution_plan.py" --workdir <dir> --host-permissions-json <json> [--available-backends codex|claude] [--dry-run]
```

Hosts act on `action.kind`:
- `cli` — invoke the selected backend CLI with the model and effort.
- `host_execute` — the host executes the task with its own model and surfaces approximate accuracy.
- `orchestrate` — dispatch multi-role orchestration through the selected CLI backend; never switch backends silently.

`--available-backends` accepts a comma-separated list (`codex,claude`) or `auto` (default; probes PATH). `--dry-run` emits the same plan schema with `executionRequested=false` and `plannedCalls=0`.
