# Entrypoints

## Contents

- [Beginner local CLI](#beginner-local-cli)
- [Codex Desktop workflow](#codex-desktop-workflow)
- [Signed-in CLI workflow](#signed-in-cli-workflow)
- [Orchestrated execution](#orchestrated-execution)
- [Offline calibration](#offline-calibration)
- [Matched efficiency evaluation](#matched-efficiency-evaluation)
- [Zero-call diagnostics](#zero-call-diagnostics)
- [Model registry validation](#model-registry-validation)
- [Learning modes and guarded optimization](#learning-modes-and-guarded-optimization)
- [Generic host plan](#generic-host-plan)
- [Conversation boundary](#conversation-boundary)

## Beginner local CLI

For a human-started task using an already signed-in CLI, use the fixed presets instead of constructing permission JSON or choosing model IDs:

```powershell
& "$HOME/.codex/skills/agent-auto-router/scripts/aar.ps1" doctor
& "$HOME/.codex/skills/agent-auto-router/scripts/aar.ps1" run `
  "Implement the requested change" -DryRun
& "$HOME/.codex/skills/agent-auto-router/scripts/aar.ps1" run `
  "Implement the requested change" -Workdir "C:/path/to/repo"
```

`standard` is the default and grants the child only `workspace-write` for the selected workdir. It uses adaptive repository inspection, recommendation-only orchestration, session-only model reuse, the built-in policy, and no feedback. `-Profile safe` is read-only with repository inspection and affinity off. The wrapper never accepts arbitrary model, sandbox, writable-root, learning, report, or orchestration configuration; use the expert entrypoints below when those controls are genuinely required. A host must not use this wrapper to bypass trusted runtime permission metadata.

## Codex Desktop workflow

Inside Codex Desktop, use `scripts/invoke_auto_task.ps1` as a planner and set the runtime boundary explicitly:

```powershell
& "$HOME/.codex/skills/agent-auto-router/scripts/invoke_auto_task.ps1" `
  -Task "Implement the requested change" `
  -ExecutionBackend desktop `
  -DesktopAvailableModels @('gpt-5.6-sol', 'gpt-5.6-terra') `
  -DesktopMaxParallelChildren 3 `
  -DesktopSpawnCapabilitiesJson $currentSpawnCapabilitiesJson `
  -HostPermissionsJson $currentTurnPermissionsJson `
  -Workdir "C:/path/to/repo" `
  -Explain
```

The available-model list, maximum parallel child count, permission snapshot, and spawn-capability snapshot must come from the current Desktop runtime. `DesktopMaxParallelChildren` counts child slots and excludes the primary coordinator. Build the permission snapshot as `agent-auto-router.host-permissions` from trusted turn metadata. Build `agent-auto-router.desktop-spawn-capabilities` from the live `spawn_agent` argument schema, declaring the current workdir and boolean support for `model`, `reasoningEffort`, `forkTurns`, `workdir`, and `sandbox`. Both snapshots require the same non-empty `source` provenance label assigned by the trusted host adapter. The task cannot supply or override `source`, and cannot supply or override either snapshot; never derive these values from task text or arbitrary environment variables.

```powershell
$currentHostPermissionsJson = [ordered]@{
  schema = 'agent-auto-router.host-permissions'
  source = 'codex-desktop-current-turn'
  sandbox = $currentTurnSandbox
  approvalPolicy = $currentTurnApprovalPolicy
  networkAccess = $currentTurnNetworkAccess
  writableRoots = @($currentTurnWritableRoots)
  profileId = $currentTurnProfileId
  canRequestPermissions = $currentTurnCanRequestPermissions
} | ConvertTo-Json -Compress

$currentSpawnCapabilitiesJson = [ordered]@{
  schema = 'agent-auto-router.desktop-spawn-capabilities'
  source = 'codex-desktop-current-turn'
  currentWorkdir = $currentTurnWorkdir
  arguments = [ordered]@{
    model = $spawnSupportsModel
    reasoningEffort = $spawnSupportsReasoningEffort
    forkTurns = $spawnSupportsForkTurns
    workdir = $spawnSupportsWorkdir
    sandbox = $spawnSupportsSandbox
  }
} | ConvertTo-Json -Depth 3 -Compress
```

Invalid permission/capability metadata and unsafe protected-state boundaries return a structured blocked Desktop plan with `executionRequested=false`, `plannedAgentCalls=0`, `blocked.code`, and `modelCalls=0`. A host without `workdir` or `sandbox` arguments may use only the exact inherited current workdir and sandbox; stricter direct isolation or read-only orchestration roles otherwise block before launch. They never launch a child and do not emit argparse usage text.

The commands below reference model IDs as they appear in the Desktop runtime (bare IDs such as `gpt-5.6-sol`) or CLI (`codex exec --model`). Internally, the router uses backend-qualified IDs. The command prints one `agent-auto-router.desktop-plan` JSON object and makes no model or CLI call. The plan excludes the task body while carrying the normalized workdir, role models, staged dependencies, effective permissions, call budget, idempotency keys, and lifecycle states.

For `executionRequested=false`, report the plan and launch nothing. For `status=blocked`, launch nothing and report the structured reason. Otherwise the primary executes dependency-ready `stages`: serial planner/dispatcher, bounded parallel workers, one final reviewer, and an optional read-only grader. A/E/F usually contain only `direct`; high-risk direct work may add a grader. Use the exact role model, effort, `forkTurns=none`, workdir, and instance bounds. The primary provides the original task and upstream results because children receive no full-history fork.

Before every non-writer stage, explicitly prohibit edits. Acquire `coordination.writerClaim` only for the declared `hostContract.onlyWriter`, after its dependencies succeed. Record required lifecycle events without task text or child output, never reuse an `idempotencyKeyTemplate` instance, and never exceed `callBudget.maximum` or `hostContract.maxParallelAgents`.

Initialize a map of returned child IDs, idempotency keys, deadlines, and terminal evidence. Enforce each agent's `timeoutMs` and the wall-clock `coordination.timeoutPolicy.totalTimeoutMs`, starting at the first spawn. A final-status notification, completed child thread, or terminal tool result is authoritative terminal evidence. Treat `list_agents` and Desktop UI labels as advisory. Keep only one open Desktop run in a parent turn and reconcile it before starting another routed run.

Wrap the entire DAG in `try/finally`. On a stage deadline, mark the instance `timed_out`, interrupt it once, and stop dependent stages. On the total deadline, mark the run timed out, interrupt every active child, and mark unstarted dependent stages `blocked`. After an interrupt, wait at most `interruptGraceTimeoutMs` for authoritative terminal evidence and reconcile again. Record a late child terminal but preserve the run/stage timeout outcome; mark a child `orphaned` only when the grace deadline expires without authoritative terminal evidence. An interrupted child without a final outcome remains `incomplete`. In `finally`, skip already-terminal IDs and release the writer claim. Complete terminal reconciliation before spawning a dependent stage and again before the final response.

After each attempted stage instance, materialize the plan's `coordination.executionReceipt` contract with `scripts/execution_receipt.py` or an equivalent trusted-host implementation. Pass the exact executable Desktop plan plus `stageId`; do not reconstruct a free-standing route/agent mapping. The closed `agent-auto-router.execution-receipt` preserves the plan's canonical agent binding, binds route, stage, role, writer flags, instance limits, identity, attempt, and idempotency key, and gives the plan attempt a stable `attemptBindingId`. Its `receiptId` is `complete-content-sha256`, so a changed observation or decision is a different receipt. Record requested, resolved, and actual model/effort separately. Actual identity must be host-observed or explicitly unresolved. Supply trusted monotonic `deadlineSequence` and `terminalSequence` observations so event order, rather than simultaneous booleans, decides whether terminal completion preceded timeout or arrived late. Terminal success remains separate from acceptance: acceptance also requires a matching actual identity and at least one required check passed by the host runtime, a deterministic validator, or an independent reviewer. For `wouldWrite=true`, the `changed-required` policy additionally requires trusted content-digested workspace evidence with `state=changed`; unchecked evidence remains pending and unchanged evidence rejects acceptance. Agent-authored claims remain pending evidence. Receipts contain no task, agent/tool output, or raw workspace paths; optional artifacts are immutable content-addressed references.

Workspace-change detection belongs to the host runtime. The router never automatically invokes, retries, or waits for `desktop_workspace_snapshot.py`; the bundled tool remains available only when a user or host explicitly requests content-aware diagnostics. Snapshot absence never blocks routing or model execution.

After a non-DryRun execution terminates, use `learning.route` unchanged, add a unique report ID and trusted host name, and fill the required result metadata from the actual run. Submit `agent-auto-router.execution-report` to `guarded_auto.py report --stdin`. Do not use an execution receipt as a learning label: guarded learning still requires its existing human or deterministic validation evidence. Do not submit task text, agent output, tool output, or a DryRun. Report ingestion is idempotent and advances guarded learning with zero model calls.

Desktop `-DryRun` still requires all runtime metadata and emits the same plan schema with `executionRequested=false`, `plannedAgentCalls=0`, and `hostContract.action=report_plan`. `wouldPlanAgentCalls` retains the non-executable minimum/maximum estimate. Lifecycle, timeout, cleanup, workspace-reconciliation, model-affinity, and planned-switch fields remain visible so the protocol can be audited without launching children. `-Json` and `-NoFeedback` are idempotent: Desktop output is already JSON and the Desktop protocol never records child output. `-FeedbackFile`, validation commands/escalation, and `-ContextMode full` are rejected as CLI-only or unsupported. Desktop never accesses credentials or app-server stdio and never falls back to CLI, another provider, lower model tier, effort, or permission level. A non-direct role may reuse the selected model only with `modelResolution=selected-model-affinity` and a satisfied tier/capability floor; otherwise a same-or-higher-tier runtime substitute must explicitly declare `modelResolution=runtime-tier-upgrade`.

`invoke_auto_task.ps1` defaults to `-OrchestrationPolicy recommend`: it reports an eligible worker plan while executing directly with no extra model calls. Select `direct` to suppress recommendations or `auto` to allow B/C/D. A high-risk route remains direct under `auto` and sets `blockedByRiskGate=true`; pass `-ConfirmHighRiskOrchestration` explicitly to authorize the recommended worker plan. The dedicated `invoke_orchestrated_task.ps1` entrypoint remains an explicit advanced workflow.

In route explanations, `recommended=true` means the utility gate and selected policy favor orchestration; under `auto`, the effective topology may already be orchestrated. `requiresExplicitOptIn=true` appears only for advisory `recommend` mode or the high-risk confirmation gate. Variant route labels are backend-neutral; use the backend-qualified selected and resolved role-model fields for exact model identity.

## Signed-in CLI workflow

Use `scripts/invoke_auto_task.ps1 -ExecutionBackend cli` for expert controls. It classifies locally with zero routing-model calls and launches the selected model through `codex exec`.

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

Use `-DryRun` for a route explanation without a model call and `-Json` for JSONL execution output. The explanation includes selector and final model, role-model policy, effort, topology, context budget, bounded escalation eligibility, and the exact packaged rule terms that matched. It never repeats the full task or workspace path. Task text travels over UTF-8 stdin. The standard route uses `-RepositoryContextMode adaptive`, `-OrchestrationPolicy recommend`, and `-ModelAffinity session`: plain-answer tasks skip scanning, execution remains direct, and no feedback is read or written. Use `auto` explicitly for forced scanning, cross-run affinity, or multi-agent execution. Active-policy loading and feedback persistence still require `-EnableLearningPolicy` and `-EnableFeedback`.

For a long conversation, a trusted host can carry an explicit model pin without exposing or persisting the raw conversation ID:

```powershell
& "$HOME/.codex/skills/agent-auto-router/scripts/invoke_auto_task.ps1" `
  -Task "Continue the current implementation" `
  -ModelAffinity sticky `
  -ConversationKeyHash $trustedConversationHmacSha256 `
  -PinnedModel "codex:gpt-5.6-terra" `
  -PinnedEffort high `
  -PinTurns 4 `
  -LastSwitchAgeSeconds 900 `
  -HostPermissionsJson $currentHostPermissionsJson `
  -Workdir "C:/path/to/repo"
```

`-ConversationKeyHash` must be a lowercase HMAC-SHA256 digest produced by the trusted host, not a plain hash of an enumerable conversation ID. `sticky` reads no feedback and stores no pin database. Omitted residency values preserve backward-compatible keep-only behavior. A stronger model or effort requirement upgrades immediately. An unavailable pin is replaced when the trusted host supplies an exact runtime list through Desktop `-DesktopAvailableModels` or orchestration `-AvailableModels`. A lower selector is used only with `-CheckpointReached -ConfirmPinDowngrade`, at least 3 pin turns, and at least 600 seconds since the previous switch. The route returns `pinUpdateRequired`, `pinUpdateModel`, and `pinUpdateEffort`; the host applies that update atomically and resets its residency counters. Supplying conversation pin state outside `sticky` fails closed. The exact availability list is used for validation but is not copied into feedback.

Repository inspection injects aggregate statistics and deterministically ranked candidate paths, not file bodies. A complete relative path explicitly present in the task is pinned ahead of generic term matches, including hidden paths such as `.codex-plugin/plugin.json`. The model must still use its permitted read tools to inspect file contents. A successful CLI exit does not prove exact-output acceptance; use a deterministic validation command when formatting or content must be enforced, and opt into escalation separately if desired.

No execution outcome is recorded by default. Add `-EnableFeedback` to allow the configured `observe` or `guarded` mode to write a privacy-minimized outcome to `~/.codex/auto-router/feedback.jsonl`; `-FeedbackFile` requires that opt-in. The outcome omits raw workspace paths, task text, and execution output. Add `-EnableLearningPolicy` separately to route with the active learned policy, or `-ModelAffinity auto` to read bounded same-workspace evidence. Whenever any protected router state is enabled, state and feedback must remain outside child-writable roots. `-NoFeedback` remains accepted as a compatibility no-op for scripts that previously disabled recording.

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

Auto selects A-F. With default `-ModelAffinity auto`, each role first attempts to reuse the selected model without weakening its profile requirement; `sticky` accepts the same trusted-host conversation pin parameters as the single-task entrypoint; `off` executes the exact A-F profile. For example, forced variant C in profile mode uses Sol planning, Terra dispatch, Luna analysis workers, and Sol implementation/review. Non-final roles always use `read-only`; only `direct` or `reviewer` can inherit a write-capable sandbox. One orchestration run uses one backend. An explicit `-Backend` only constrains Auto to that backend and never authorizes its explicit-trial models; those require an explicit user model choice. Use `-DryRun` to route without launching models.

Use `-TotalTimeout`, `-MaxModelCalls`, and role-specific effort parameters to bound long runs. Git status has a five-second timeout and an explicit `clean / dirty / non_git / unknown` result; write-capable execution blocks on `unknown` before constructing an adapter. `-ResultsDir` must remain outside every child-writable root and cannot be used with `danger-full-access`. It creates a UUID-suffixed, non-overwriting, privacy-minimized report, requests owner-only POSIX permissions, and strips task, prompt, output, rationale, error, tool-content, workspace-path, and response-ID fields. `-IncludeOutputInReport` explicitly opts into a content-bearing report and is invalid without `-ResultsDir`; Windows verifies a protected current-user/System/Administrators DACL before content is written. Progress events are JSON lines on stderr; `-Quiet` suppresses them.

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

The repository-only development harness also defaults to direct routing. Add `--orchestration-policy auto` only when the benchmark is explicitly evaluating Auto topology selection; `--routing-mode balance` by itself selects a model tier but does not enable orchestration or affinity.

## Zero-call diagnostics

Check the current platform, Python version, Git, PowerShell wrappers, supported CLI commands, registry/profile validity, and registry review age without reading task content, printing environment values, inspecting credentials, or launching a model. Default command discovery uses `PATH`; packaged absolute paths and env-derived Codex locations are redacted:

```powershell
python "$HOME/.codex/skills/agent-auto-router/scripts/doctor.py"
```

Use `--verbose-paths` only when absolute packaged-asset paths and `CODEX_CLI_PATH`/`LOCALAPPDATA`-derived discovery are needed for local troubleshooting. The default output is a one-screen human summary. Add `--json` for the complete machine-readable result, which always reports `modelCalls: 0` and whether paths were included. Missing PowerShell affects wrapper usability, not the cross-platform Python routing entrypoints. Use `--fail-on-issues` only when every optional CLI/wrapper prerequisite is required by the deployment.

## Model registry validation

After editing `scripts/model_registry.json` or `scripts/orchestration_profiles.json`, validate every model and A-F role without launching models:

```powershell
python "$HOME/.codex/skills/agent-auto-router/scripts/validate_model_registry.py" --fail-on-stale
```

For a candidate file outside the installed Skill, pass `--registry` and optionally `--profiles`. Validation confirms schema, aliases, roles, tier resolution, the high-risk primary capability, explicit-only models, the registry digest, and the `reviewedAt` age. `--max-review-age-days` changes the review window. It does not prove that the active provider exposes the model; perform a separate controlled `read-only` explicit invocation for that.

## Learning modes and guarded optimization

Configuration schema v2 accepts only `off`, `observe`, and `guarded`. `observe` is the default and records metadata without changing thresholds; `off` creates no feedback; `guarded` enables the bounded automatic lifecycle. Old mode names and old schemas are rejected.

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

`Observe` mode remains the default. To authorize the narrow guarded loop, inspect its configuration and enable it explicitly:

```powershell
python "$HOME/.codex/skills/agent-auto-router/scripts/guarded_auto.py" status
python "$HOME/.codex/skills/agent-auto-router/scripts/guarded_auto.py" configure `
  --mode guarded
python "$HOME/.codex/skills/agent-auto-router/scripts/guarded_auto.py" cycle --dry-run
```

CLI outcome recording automatically runs one cycle in `observe` or `guarded`; `off` returns without persisting the route. Each cycle atomically retains the latest outcome/label pair for at most 5,000 routes from the last 90 days. Run `guarded_auto.py feedback` for a read-only storage preview, or add `--maximum-routes`, `--retention-days`, and `--apply` for explicit custom compaction. Completed execution-report markers use a separate 90-day / 5,000-ID window; inspect it with `guarded_auto.py reports`. Pending and incomplete markers are preserved for review. Run `guarded_auto.py shadow` for a read-only aggregate baseline/candidate comparison with no activation authority. The guarded loop accepts only explicit user selections or deterministic validation-proven adjacent-tier escalations, may only lower one threshold by one step, and must pass held-out evaluation, deterministic canary, and probation before stabilizing. Its defaults are 12 strong signals, a 20% canary, six verified reports per comparison arm, and twelve probation reports. Re-evaluation uses the newest strong-evidence timestamp, so rolling retention does not freeze learning. Registry, benchmark, candidate, or active-policy drift fails closed; verified regression rejects or rolls back automatically. Disable automatic transitions with `configure --mode observe`, or disable persistence with `configure --mode off`. See `guarded-auto-learning.md` for the report schema and full boundary. Cost-saving threshold increases and every protected surface remain manual and approval-gated.

Keep `--state-dir` and any `--feedback-file` outside every child-writable root whenever guarded learning or model affinity uses them. The CLI entrypoints check this before a non-DryRun launch and block protected-input execution under full access or an unverifiable external sandbox; tighten the child to a protected workspace boundary, disable affinity for a stateless run, or return from `guarded` to `observe` without letting a model write its own evidence.

## Conversation boundary

Neither backend adds `Auto` to the Desktop picker or switches the current conversation model. CLI starts separate signed-in CLI tasks. Desktop starts only the bounded child calls declared by a ready plan and otherwise blocks explicitly.

## Generic host plan

Consumes `agent-auto-router.host-request` and emits `agent-auto-router.host-plan` for Codex, Claude Code, and other capable hosts. The request contains the current task and its bound `routeDecision`; the plan omits the task body. A host may execute the task itself, invoke a declared CLI backend, or run multi-role orchestration. Automatic execution requires a trusted permission snapshot and includes the normalized effective permissions in both the host contract and action.

```powershell
python "<skill-dir>/scripts/host_execution_plan.py" --workdir <dir> --host-permissions-json <json> [--available-backends codex|claude] [--dry-run]
```

Hosts act on `action.kind`:
- `cli` — invoke the selected backend CLI with the model and effort.
- `host_execute` — the host executes the task with its own model and surfaces approximate accuracy.
- `orchestrate` — materialize the plan's `agent-auto-router.execution-envelope` stdin template with the current task, then dispatch multi-role orchestration through the selected CLI backend. The route and permission snapshot are never carried in argv. The entrypoint verifies the task/workspace bindings and reuses the decision without rerouting; never switch models, effort, strategy, variant, or backend silently.

`--available-backends` accepts a comma-separated list (`codex,claude`) or `auto` (default; probes PATH). `--dry-run` emits the same plan schema with `executionRequested=false` and `plannedCalls=0`.
