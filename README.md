# Agent Auto Router

**English** | [简体中文](README.zh-CN.md)

A local, deterministic model-routing plugin and Skill for Codex, Claude Code, and generic agent hosts.

Before execution, it selects a trusted model from task complexity, risk, constraint level, and reasoning effort, then produces a bounded execution plan. Standard routing safely enables adaptive repository inspection, orchestration recommendations, and within-plan model reuse without reading historical state or starting extra agents. Cross-run affinity, learned policy, feedback writes, and multi-role execution remain explicit opt-ins. Routing itself makes no model call, does not change global Codex configuration, and never reads or forwards login credentials.

Current project version: `0.15.0+codex.20260813062845`.

## At a glance

- `Auto` is a per-task routing decision. It is not a new model and does not appear in the Codex model picker.
- The default `balance` strategy selects among the `fast`, `balanced`, and `frontier` capability tiers.
- Codex Desktop executes through the native child-agent protocol; CLI mode uses an official CLI in which the user is already signed in.
- Desktop plans omit the task body, and every execution is constrained by current host permissions, a call budget, and the single-writer rule.
- Standard routing is zero-state: adaptive inspection skips plain-answer tasks and performs one bounded scan for code, path, dependency, or debugging tasks. It does not load learned policy, read affinity feedback, persist outcomes, or auto-orchestrate.
- New models can be tested explicitly before entering Auto. An unavailable model fails explicitly instead of silently falling back.

## Capabilities

| Capability | Description |
| --- | --- |
| Automatic model selection | Selects a model, effort, capability tier, and context budget from a versioned trusted registry |
| Canonical route decisions | Emits strict `agent-auto-router.route-decision` objects with task and workspace bindings |
| Desktop execution plans | Emits `agent-auto-router.desktop-plan`, which the current primary agent executes as a bounded DAG |
| CLI execution | Invokes a signed-in Codex CLI or orchestration backend through UTF-8 stdin |
| Multi-role orchestration | Supports planner, dispatcher, worker, reviewer, and grader roles while enforcing a single writer |
| Generic host protocol | Emits `agent-auto-router.host-plan` with stdin execution-envelope templates |
| Model registry | Separates `enabled` from `autoEligible` for controlled extensions and explicit trials |
| Privacy-minimized feedback | Stores routing metadata, validation status, duration, and observable tokens, but not tasks or responses |
| Guarded learning | Uses explicit `off / observe / guarded` modes with canary, probation, and automatic rollback |
| Codex plugin | Distributes the existing Skill through a standard plugin manifest without coupling the cross-host core to Codex |

## Non-goals

- It does not modify `~/.codex/config.toml`, providers, accounts, or CC Switch state.
- It does not read, copy, proxy, or forward Desktop or CLI credentials.
- It does not accept model IDs or permissions injected through task text, model output, or arbitrary environment variables.
- It does not silently change providers, models, tiers, effort, or backends when a model is unavailable.
- It does not allow planner, dispatcher, worker, or grader roles to modify the shared workspace.
- It does not write task text, model output, tool output, or credentials to feedback logs.
- It does not change active thresholds because of one successful call.
- Learning cannot weaken the `frontier + high-risk-primary` boundary for high-risk tasks.

See [router-contract.md](skills/agent-auto-router/references/router-contract.md) for the complete runtime contract and [SECURITY.md](SECURITY.md) for the finding-by-finding remediation record and residual limitations.

## Workflow

```text
Task
  -> deterministic local feature extraction
  -> built-in policy and offline benchmark priors; optionally load an active policy
  -> tier, model, effort, context, and A-F variant selection
  -> host-permission, model-availability, call-budget, and workspace checks
  -> Desktop: emit a staged plan without the task body
     CLI: invoke the corresponding signed-in CLI
  -> record a privacy-minimized result in observe/guarded mode
  -> optional human labeling or guarded learning
```

Routing and execution are separate phases. Routing is entirely local; only execution can produce a model call.

The orchestration modes are `direct`, `recommend`, and `auto`. Standard routing uses `recommend`, which may explain a useful role split but still emits a one-call direct plan; only explicit `auto` may plan multi-agent execution.

## Quick start

### Requirements

- Python 3.10 or later
- PowerShell wrappers: Windows PowerShell 5.1 on Windows, or PowerShell 7 on Windows, Linux, and macOS
- Desktop mode: a current Codex Desktop runtime with child-agent support
- CLI mode: the target CLI installed and independently signed in

The Python router uses only the standard library and has no third-party runtime dependency.

### Three-command local path

Most signed-in CLI users need only the small `aar.ps1` wrapper. First run the one-screen, zero-call diagnostic:

```powershell
& "./skills/agent-auto-router/scripts/aar.ps1" doctor
```

Preview a route without a model call:

```powershell
& "./skills/agent-auto-router/scripts/aar.ps1" run `
  "Rename the configuration field and update its tests" -DryRun
```

Run it after reviewing the route:

```powershell
& "./skills/agent-auto-router/scripts/aar.ps1" run `
  "Rename the configuration field and update its tests" `
  -Workdir "D:/path/to/project"
```

`standard` is the default profile and limits writes to `Workdir`. It adaptively inspects repositories only for code/path/debugging tasks, reports orchestration recommendations without executing them, and reuses the selected model only within the current run. `-Profile safe` is read-only with repository inspection and affinity disabled. Neither profile loads learned policy, writes feedback, or starts multi-agent execution. These are fixed, validated presets rather than user-editable permission shortcuts.

The diagnostic prints a concise summary by default. Add `-Json` through `aar.ps1`, or run `doctor.py --json`, for machine-readable details. It never reads tasks, prints environment values, inspects credentials, or calls a model. `--verbose-paths` is an additional explicit troubleshooting option.

### Install as a Codex plugin

The repository root is a standard Codex plugin root, with its manifest at [.codex-plugin/plugin.json](.codex-plugin/plugin.json). To install it for the current Codex user, run this command from the repository root:

```powershell
python "./scripts/install_personal_plugin.py"
```

The installer validates the source plugin, assembles a minimal distribution in a temporary directory, and installs it to `~/plugins/agent-auto-router`. It then creates or preserves the personal marketplace in `~/.agents/plugins/marketplace.json` and runs `codex plugin add agent-auto-router@<marketplace-name>`.

When an existing plugin package changes, it is backed up under `~/.codex/plugin-backups/`. Existing marketplace names, display names, ordering, and unrelated plugin entries are preserved. Reinstalling the same version is idempotent. On Windows Codex Desktop hosts, if the local `CodexSandboxUsers` group exists, the installer recursively grants that group read and execute access so an administrator-installed package remains readable inside the Codex sandbox.

The script does not install Codex CLI, Claude CLI, Python, or PowerShell, and it does not copy login credentials. If Codex CLI is not on `PATH`, the prepared local package and marketplace are preserved; install the CLI and retry the command printed in the error. Tests and preconfigured environments can use `--home <temporary-directory> --skip-codex-install`, which never invokes Codex CLI.

After a successful installation, create a new Codex task and invoke `$agent-auto-router` so the new task loads the Skill from the plugin.

Do not enable both the plugin edition and the standalone Skill edition below in the same Codex environment, because that creates two Skills named `$agent-auto-router`. If `~/.codex/skills/agent-auto-router` still exists, the plugin installer stops before writing anything. To migrate, back up and remove only that installed standalone copy, then rerun the installer. Do not delete the learning state under `~/.codex/auto-router`.

### Standalone Skill and other hosts

Traditional Codex installations, Claude Code, Hermes, and other hosts can still clone the repository and use the existing installation script:

```powershell
git clone https://github.com/jinweechen/agent-auto-router.git
cd agent-auto-router
& "./skills/agent-auto-router/scripts/install.ps1" -Backup
```

For a traditional Codex Skill installation, the target is `$CODEX_HOME/skills/agent-auto-router`, or `~/.codex/skills/agent-auto-router` when `CODEX_HOME` is unset. `-Backup` stores an older version under `~/.codex/skill-backups/agent-auto-router`.

The installer replaces an existing copy through a staging directory, so it is safe to rerun and does not create a nested `agent-auto-router/agent-auto-router`. Other hosts do not need to understand `.codex-plugin/plugin.json`; ordinary signed-in CLI users can call `aar.ps1`, while host integrations can call `host_execution_plan.py`, `invoke_auto_task.ps1`, or `invoke_orchestrated_task.ps1`. See [entrypoints.md](skills/agent-auto-router/references/entrypoints.md) for expert entrypoints. Each host must independently supply model availability, login state, and a trusted permission boundary.

### Use it in Codex

```text
$agent-auto-router Use the balance strategy to select a model automatically and execute the current task.
```

Specify a working directory:

```text
$agent-auto-router Complete the current change in D:\path\to\project using the balance strategy.
```

Inspect routing without executing a model:

```text
$agent-auto-router Run a DryRun for "rename a configuration field and update the docs"; return only the plan and reason.
```

This is the recommended entrypoint for most users. The Codex primary agent obtains available models, child-agent capacity, and the permission snapshot from trusted metadata for the current turn; users do not need to construct those values manually.

## Routing policy

### Capability tiers

The default Codex mapping comes from [model_registry.json](skills/agent-auto-router/scripts/model_registry.json):

| Tier | Default model | Typical tasks |
| --- | --- | --- |
| `frontier` | `codex:gpt-5.6-sol` | High-risk work, architecture, complex refactors, deep debugging, and open-ended tasks |
| `balanced` | `codex:gpt-5.6-terra` | Routine development, ordinary debugging, and balanced tasks |
| `fast` | `codex:gpt-5.6-luna` | Extraction, transformation, formatting, and tightly bounded tasks |

Model IDs use `{backend}:{model}`. The registry also includes:

- `claude:sonnet`: `balanced`, eligible for Auto.
- `claude:haiku`: `fast`, eligible for Auto.
- `claude:opus`: `frontier`, explicit trial only by default.

The registry is a trust and capability declaration; it does not prove that the current account can access a model. The current Desktop runtime or CLI provider still verifies actual availability. `reviewedAt` records the last human review date; CI rejects a registry older than the configured freshness window.

### Strategies

| Strategy | Selection bias |
| --- | --- |
| `intelligence` | Quality first; uses `frontier` for complex tasks and mainly `balanced` otherwise |
| `balance` | Recommended default; favors `fast`, `balanced`, and `frontier` for simple, routine, and complex tasks respectively |
| `cost` | Uses tiers as a cost proxy while retaining capability floors for complex work and always using `frontier` for high-risk work |

`cost` is not billing optimization. CLI token counts are observable execution data, not prices or final billing figures.

### Features and priors

Routing uses deterministic features, including:

- complexity, risky actions, and sensitive domains
- whether the task is simple and tightly bounded
- ambiguity, debugging, long context, multiple files, and computer use
- acceptance-criteria count and repository size
- whether a deterministic validation command is configured

ASCII keywords use lexical-boundary matching to avoid false positives such as `tokenizer`/`token` or `information`/`format`. Chinese phrases continue to use substring matching. `-Explain` reports only the packaged rule terms that matched, never the full task text, so a decision can be traced without duplicating sensitive content.

[benchmark_priors.json](skills/agent-auto-router/scripts/benchmark_priors.json) is a versioned offline snapshot and is never refreshed at runtime. It provides only capability-tier floors and does not replace acceptance testing in the current repository. Read [benchmark-routing.md](skills/agent-auto-router/references/benchmark-routing.md) before updating its evidence.

### A-F execution variants

| Variant | Topology | Default role tiers |
| --- | --- | --- |
| A | direct | `frontier` direct |
| B | orchestrated | `frontier` planner -> `fast` workers -> `frontier` reviewer |
| C | orchestrated | B + `balanced` dispatcher |
| D | orchestrated | `balanced` planner -> `fast` workers -> `balanced` reviewer |
| E | direct | `balanced` direct |
| F | direct | `fast` direct |

A, E, and F are direct variants. The standard entrypoint and reusable Python APIs default to `recommend`: they evaluate B, C, or D and expose a recommendation, but still execute directly with no extra model calls. Expert callers must select `auto` to start multi-agent execution. Python affinity defaults to `session`, and omitted effort uses the selected tier's recommendation rather than acting like an explicit `medium`. Under `auto`, a worker plan requires a low-risk task with explicit parallel signals, enough scale, and a positive deterministic utility score after model-call and model-tier-switch overhead. High-risk tasks remain direct unless an expert caller also passes `-ConfirmHighRiskOrchestration`. The dedicated orchestration entrypoint is itself an explicit advanced workflow.

Default `-ModelAffinity session` reuses the selected model only among compatible roles in the current plan and reads no historical state. Explicit `-ModelAffinity auto` may retain the most recent successful model for the same hashed workspace and strategy for up to 30 minutes, subject to the existing tier and cache-signal safeguards. `-ModelAffinity off` uses exact profile role assignments. These ratios are routing evidence, not provider billing estimates.

Role defaults come from [orchestration_profiles.json](skills/agent-auto-router/scripts/orchestration_profiles.json).

## Execution modes

### Codex Desktop

Desktop execution is a host protocol, not a hidden CLI login flow.

The primary agent gives the router the model IDs and argument support explicitly exposed by the current runtime, parallel child-agent capacity, an `agent-auto-router.host-permissions` permission snapshot, and a source-bound `agent-auto-router.desktop-spawn-capabilities` snapshot. The router returns:

- exact role models and reasoning effort
- a staged DAG and dependency order
- maximum calls and maximum concurrency
- an idempotency key for each role
- read-only stages and the unique writer
- a privacy-safe post-execution report template

Desktop currently supports only the Codex backend. The default `selected-model-preferred` role policy first reuses the route's selected model when it satisfies the role and tier floor. Otherwise it resolves the profile model, allowing only a runtime-declared, registry-trusted Codex model at the same or a higher tier; every substitution is explicit. Otherwise the router returns a structured block.

The capability snapshot is closed and derived from the live `spawn_agent` tool schema. If the host cannot pass a per-child workdir or sandbox, only an exact inherited boundary is executable; isolated workdirs, stricter direct sandboxes, or read-only orchestration stages block before any model call.

See [entrypoints.md](skills/agent-auto-router/references/entrypoints.md) for the complete host execution procedure.

### Single-task Codex CLI execution

A regular PowerShell terminal can explicitly select a stricter sandbox and execute a task. This example makes a real model call but permits only read access to the workspace:

```powershell
& "$HOME/.codex/skills/agent-auto-router/scripts/invoke_auto_task.ps1" `
  -Task "Review the current project and recommend improvements" `
  -ExecutionBackend cli `
  -Model auto `
  -Strategy balance `
  -Sandbox read-only `
  -Workdir "D:/path/to/project" `
  -Explain
```

For a write task, explicitly use `-Sandbox workspace-write`; this mode forwards only `-Workdir` as a writable root. Learning state and custom feedback files must remain outside that root, or execution blocks before a model call. Never run `guarded` learning with `danger-full-access`.

Host integrations should pass a trusted permission snapshot generated by the current runtime. `$currentHostPermissionsJson` must not come from the user task, model output, or arbitrary environment content:

```powershell
& "$HOME/.codex/skills/agent-auto-router/scripts/invoke_auto_task.ps1" `
  -Task "Implement the change and run the project tests" `
  -ExecutionBackend cli `
  -Model auto `
  -Strategy balance `
  -HostPermissionsJson $currentHostPermissionsJson `
  -Workdir "D:/path/to/project" `
  -Explain
```

The CLI task body is passed over UTF-8 stdin and does not appear in child-process command-line arguments. `-Model sol`, `-Model terra`, or a full trusted Codex ID can explicitly override selection for one run without changing global configuration. The default route uses `-ModelAffinity session`, `-RepositoryContextMode adaptive`, and `-OrchestrationPolicy recommend`; it reads no feedback, loads no active policy, persists nothing, and starts no extra agents. Adaptive mode skips plain-answer tasks with `scan_duration_ms=0` and scans code/path/debugging tasks once. Use `-ModelAffinity auto`, `-RepositoryContextMode auto`, or `-OrchestrationPolicy auto` for cross-run affinity, forced inspection, or multi-agent execution. `-EnableLearningPolicy` and `-EnableFeedback` remain explicit.

Run a local DryRun with zero model calls:

```powershell
& "$HOME/.codex/skills/agent-auto-router/scripts/invoke_auto_task.ps1" `
  -Task "Format this text" `
  -ExecutionBackend cli `
  -Strategy balance `
  -Workdir "." `
  -DryRun `
  -Explain
```

### Multi-role CLI orchestration

```powershell
& "$HOME/.codex/skills/agent-auto-router/scripts/invoke_orchestrated_task.ps1" `
  -Task "Refactor the authentication module and add tests" `
  -Strategy balance `
  -HostPermissionsJson $currentHostPermissionsJson `
  -Workdir "D:/path/to/project" `
  -MaxWorkers 2 `
  -MaxModelCalls 7 `
  -Explain
```

Common controls:

| Option | Purpose |
| --- | --- |
| `-Variant A..F` | Explicitly choose an orchestration variant |
| `-Backend codex|claude` | Restrict one orchestration run to one backend |
| `-DryRun` | Emit routing without calling a model |
| `-Sandbox read-only` | Prevent the final role from writing |
| `-RepositoryContextMode adaptive|auto|off` | Scan only code/path/debugging tasks, force one bounded scan, or disable inspection |
| `-ModelAffinity session|auto|off` | Reuse within one plan, read bounded same-workspace evidence, or use exact role profiles |
| `-AllowDirty` | Explicitly accept the risk of running in a dirty Git worktree |
| `-AllowNoChanges` | Allow a write task to succeed without a Git status change |
| `-MaxTotalTokens` | Set a soft budget for CLI-observable tokens |
| `-GraderPolicy auto|always|never` | Control the independent grader |
| `-ContextMode lean|full` | Control whether read-only roles ignore personal CLI configuration |

Formal orchestration requires a clean Git worktree by default. Git status is bounded to five seconds and classified as `clean`, `dirty`, `non_git`, or `unknown`; write-capable execution blocks on `unknown` before any adapter or model call. Parallel roles are read-only; only the direct role or final reviewer can receive the exclusive writer claim. Restricting `-Backend` never makes explicit-trial models Auto-eligible; only an explicit user model choice may select one.

`-ResultsDir` must be outside every child-writable root; it is unavailable with `danger-full-access`. Reports use unpredictable UUID-suffixed names, exclusive creation, owner-only POSIX permissions, and remove task, prompt, output, rationale, error, tool, workspace-path, and response-ID fields by default. `-IncludeOutputInReport` requires `-ResultsDir`; on Windows, a protected DACL limited to the current user, System, and Administrators is verified before content is written. Protect and retain that file as sensitive data.

### Generic hosts

`host_execution_plan.py` builds `agent-auto-router.host-plan` from a host request containing the current task and its bound route plus a trusted permission snapshot, without starting a process or returning the task body. The host acts on `action.kind`:

- `cli`: invoke the declared backend and model.
- `host_execute`: execute through the host's native model and disclose the approximate-model boundary.
- `orchestrate`: materialize the declared `execution-envelope` stdin template with the current task, then invoke the local multi-role entrypoint. The route and permission snapshot never travel in process argv, and the entrypoint verifies the task and workspace bindings instead of routing again.

A generic host must not copy connectors, signed-in sessions, or credentials into an independent CLI.

## Permissions and the single-writer boundary

Automatic execution uses `agent-auto-router.host-permissions`. A trusted snapshot contains:

- sandbox and approval policy
- network access
- absolute writable roots
- whether scoped permission requests are available
- an optional host permission-profile ID

Effective child permissions must be equal to or weaker than host permissions. Execution blocks before model launch when a trusted snapshot is missing, `workspace-write` lacks an absolute writable root, the workdir lies outside allowed roots, or a child CLI cannot safely reproduce the combined boundary.

Desktop multi-role plans enforce these rules:

1. Planner, dispatcher, worker, and grader roles are read-only.
2. Multiple concurrent writers are forbidden.
3. A direct role or reviewer can acquire the writer claim only after its dependencies succeed.
4. Execution stops if a read-only stage unexpectedly changes the workspace.
5. Timed-out `xhigh` or `max` roles are not retried automatically.

## Feedback and threshold learning

### Stored data

With `-EnableFeedback`, the configured `observe` or `guarded` mode writes privacy-minimized CLI results to `~/.codex/auto-router/feedback.jsonl`:

- route ID, strategy, effort, capability tier, and model
- a SHA-256 workspace identity, topology, variant, role-model policy, and estimated role-tier switches
- numeric and boolean routing features
- policy, registry, and feature-schema digests
- exit code, duration, validation status, and attempt count
- aggregate input, cached-input, cache-write, output, and reasoning-output tokens actually exposed by the CLI, plus the final selected model's separately attributed token slice for affinity

Unobservable token counts remain `null`. Feedback never stores the raw workspace path, task text, model responses, tool output, or credentials. When feedback, active learning policy, or affinity is explicitly enabled, the state directory and feedback file must stay outside every child-writable root. In `observe` and `guarded`, each learning cycle atomically keeps the latest outcome and label for routes seen in the last 90 days, capped at 5,000 routes. `status.feedbackStorage` previews counts, bytes, and pending removals. Inspect or explicitly apply a custom retention window with:

```powershell
python "$HOME/.codex/skills/agent-auto-router/scripts/guarded_auto.py" feedback
python "$HOME/.codex/skills/agent-auto-router/scripts/guarded_auto.py" feedback --maximum-routes 2000 --retention-days 30 --apply
```

The first command is read-only. Compaction uses the feedback append lock and an atomic replacement, preserves route/label pairs, and still reports `storesTaskText=false` and `modelCalls=0`. Recording is off unless `-EnableFeedback` is supplied; `-FeedbackFile` requires that switch. `-NoFeedback` remains accepted for compatibility. Use `-StateDir` to isolate state.

Desktop execution-report IDs use separate content-free idempotency markers. Completed markers have the same default 90-day / 5,000-marker window; pending and incomplete markers are never removed automatically and are surfaced for operator review. Because this is a bounded exact window, a report repeated after its marker expires is treated as new. Inspect or explicitly apply a custom window with:

```powershell
python "$HOME/.codex/skills/agent-auto-router/scripts/guarded_auto.py" reports
python "$HOME/.codex/skills/agent-auto-router/scripts/guarded_auto.py" reports --maximum-markers 2000 --retention-days 30 --apply
python "$HOME/.codex/skills/agent-auto-router/scripts/guarded_auto.py" recover-report --report-id REPORT_ID
python "$HOME/.codex/skills/agent-auto-router/scripts/guarded_auto.py" recover-report --report-id REPORT_ID --action release-for-retry --confirm-report-id REPORT_ID --resolved-by OPERATOR
python "$HOME/.codex/skills/agent-auto-router/scripts/guarded_auto.py" recover-report --report-id REPORT_ID --action acknowledge-recorded --confirm-report-id REPORT_ID --resolved-by OPERATOR
```

Recovery is inspection-first. `release-for-retry` is permitted only when neither marker progress nor matching feedback exists; it archives the marker before freeing the ID. `acknowledge-recorded` requires exactly one matching route outcome and the expected label evidence, never deletes or rewrites feedback, and defers an unfinished learning cycle to an explicit `cycle` command. Both mutations require an exact report-ID confirmation and resolver identity, make zero model calls, and cannot activate a policy.

### Learning modes

The configuration schema accepts only these modes; old `manual` and `guarded-auto` values are rejected instead of being silently migrated:

| Mode | Persist route outcomes | Automatic threshold changes |
| --- | --- | --- |
| `off` | No | No |
| `observe` | Yes, default | No |
| `guarded` | Yes | Only through the bounded canary lifecycle |

Human-approved candidate commands remain available in every mode:

```powershell
python "$HOME/.codex/skills/agent-auto-router/scripts/policy_learning.py" status

python "$HOME/.codex/skills/agent-auto-router/scripts/policy_learning.py" label `
  --route-id "<route-id>" `
  --preferred-model gpt-5.6-terra `
  --outcome pass

python "$HOME/.codex/skills/agent-auto-router/scripts/policy_learning.py" propose `
  --output "./candidate-policy.json"

python "$HOME/.codex/skills/agent-auto-router/scripts/policy_learning.py" approve `
  --candidate "./candidate-policy.json" `
  --approved-by "reviewer-name"
```

At least 20 usable human labels are required by default. A candidate must pass a held-out set and validation of its integrity digest, current active policy, model registry, and benchmark priors. Proposing a candidate does not change the active policy; approval archives the previous version and writes an audit record.

Roll back to the most recent distinct version:

```powershell
python "$HOME/.codex/skills/agent-auto-router/scripts/policy_learning.py" rollback `
  --approved-by "reviewer-name"
```

### Guarded learning

Automatic threshold changes are disabled in the default `observe` mode. Explicitly enabling `guarded` accepts only two strong signals:

1. The user explicitly selected a more appropriate model.
2. Deterministic validation failed on the initial tier and passed on the adjacent stronger tier, with no high-risk task or explicit override.

An ordinary success, exit code 0, low latency, or fewer tokens is not a quality label.

```powershell
python "$HOME/.codex/skills/agent-auto-router/scripts/guarded_auto.py" status
python "$HOME/.codex/skills/agent-auto-router/scripts/guarded_auto.py" configure --mode guarded
python "$HOME/.codex/skills/agent-auto-router/scripts/guarded_auto.py" cycle --dry-run
python "$HOME/.codex/skills/agent-auto-router/scripts/guarded_auto.py" shadow
python "$HOME/.codex/skills/agent-auto-router/scripts/guarded_auto.py" configure --mode observe
python "$HOME/.codex/skills/agent-auto-router/scripts/guarded_auto.py" configure --mode off
```

The aggressive guarded defaults evaluate after 12 strong signals, use a 20% canary, require six verified reports on both canary and baseline, and require twelve probation reports. For each threshold it changes, a candidate may still move at most one step toward a stronger capability tier. It must pass held-out validation, deterministic canary, probation, and rollback gates. Re-evaluation uses the latest strong-evidence timestamp instead of an ever-growing event count, so bounded retention cannot freeze learning at the route cap. State, configuration, approval, and rollback mutations use bounded OS file locks; JSONL streams use separate append locks.

`shadow` is a read-only A/B preview for the current canary/probation candidate, or for an explicit `--candidate` file. It scores baseline and candidate on the same retained evidence and deterministic holdout, adds Wilson accuracy intervals, an exact paired sign test, minimum-effect gates, and privacy-safe strategy/risk/label-source strata. Strata with fewer than three samples are suppressed. The assessment distinguishes insufficient evidence, regression, promising-but-unconfirmed effects, and statistically supported favorable candidates. It never returns route IDs and always returns `activationAuthorized=false` with `modelCalls=0`; shadow results can never activate a policy.

Learning state and feedback form a protected control plane and must remain outside every child-writable root. Guarded-auto blocks under `danger-full-access`, an unverifiable external sandbox, or any boundary that lets a child modify learning evidence.

Feature semantics are versioned by `featureSchemaVersion`. Current v3 records may participate in learning. Historical records without a version remain readable as legacy v1 evidence during the feedback retention window, while v2 and other stale records remain audit-readable but cannot enter new candidate, canary, or probation statistics.

See [guarded-auto-learning.md](skills/agent-auto-router/references/guarded-auto-learning.md) for the complete protocol.

## One validation-driven escalation

A single-task CLI run may escalate once from the selected tier to the adjacent stronger tier only when the user explicitly provides a deterministic validation command and enables `-EscalateOnValidationFailure`:

```powershell
& "$HOME/.codex/skills/agent-auto-router/scripts/invoke_auto_task.ps1" `
  -Task "Implement the change and pass the tests" `
  -Model auto `
  -HostPermissionsJson $currentHostPermissionsJson `
  -Workdir "D:/path/to/project" `
  -ValidationCommand @('python', '-m', 'unittest', 'discover', '-s', 'tests') `
  -EscalateOnValidationFailure
```

Explicit model overrides cannot escalate automatically. Authentication, provider, model-availability, network, and permission failures do not trigger escalation.

## Extending the model registry

Models are defined in [model_registry.json](skills/agent-auto-router/scripts/model_registry.json). Important fields:

| Field | Meaning |
| --- | --- |
| `enabled` | Allows explicit user selection |
| `autoEligible` | Allows the Auto tier resolver to select the model |
| `reviewedAt` | ISO review date used by the freshness gate |
| `tier` | `fast`, `balanced`, or `frontier` |
| `priority` | Selection order within the same tier and role; lower values win |
| `capabilities` | Capabilities the model may provide |
| `allowedRoles` | Orchestration roles the model may fill |

Recommended rollout sequence for a new model:

1. Add the model with `enabled: true` and `autoEligible: false`.
2. Validate the registry.
3. Invoke the model explicitly in an isolated, read-only environment.
4. Run a matched evaluation with the same cases and external acceptance criteria.
5. Only after it passes, set `autoEligible: true` and review its tier, roles, and priority.
6. Run the full tests, offline evaluation, and DryRuns; install only after human review.

```powershell
python "./skills/agent-auto-router/scripts/validate_model_registry.py" --fail-on-stale
```

## Evaluation and development

### Offline routing evaluation

```powershell
python "./skills/agent-auto-router/scripts/evaluate_auto_router.py" `
  --output "./auto-router-eval.json"
```

This command makes no model call. It checks all three strategies, A-F reachability, high-risk boundaries, Chinese routing, lexical boundaries, registry behavior, and pinned benchmark priors.

### Matched efficiency evaluation

```powershell
python "./skills/agent-auto-router/scripts/evaluate_development_routes.py" `
  --results "./matched-results.json" `
  --output "./matched-summary.json"
```

Input may contain only case ID, configuration, optional model and effort, external acceptance, optional tokens, duration, and retries. It must not contain prompts or outputs. The tool compares acceptance first and calculates a token delta only for matched cases where both configurations passed and both token counts are complete.

Development benchmarks that make real CLI model calls live outside the installed package under [benchmarks/](benchmarks/README.md). Reproducible inputs live in `benchmarks/cases/`, tools live in `benchmarks/tools/`, and generated output defaults to the user-state path `agent-auto-router/evaluations/<kind>/<run-id>/`; `AGENT_AUTO_ROUTER_EVALUATIONS_DIR` can override that root. They are not Skill runtime dependencies; set an explicit model-call budget and use an isolated workspace before running them. `--route-only` emits a routing report without a model call.

### Tests and validation

```powershell
python -m unittest discover -s tests -p "test_*.py"
python "./skills/agent-auto-router/scripts/validate_model_registry.py" --fail-on-stale
python "./skills/agent-auto-router/scripts/doctor.py"
python "./skills/agent-auto-router/scripts/evaluate_auto_router.py"
python "./scripts/validate_skill.py"
python "./scripts/validate_plugin.py"
$pluginTestHome = Join-Path $env:TEMP "agent-auto-router-plugin-test"
python "./scripts/install_personal_plugin.py" --home "$pluginTestHome" --skip-codex-install
python -m compileall -q skills/agent-auto-router/scripts scripts benchmarks tests
```

The repository's `validate_skill.py` does not depend on a personal Codex installation path, so it works on ordinary development machines and in CI. If the current Codex environment has the system `skill-creator` installed, you can additionally run its official validator:

```powershell
python "$HOME/.codex/skills/.system/skill-creator/scripts/quick_validate.py" `
  "./skills/agent-auto-router"
```

CI runs the core suite on Windows, Ubuntu, and macOS with Python 3.10 and 3.12. Every platform validates registry freshness, zero-call diagnostics, and offline routes; Windows additionally verifies PowerShell 5.1, orchestration DryRuns, and repeat installation.

## Plugin and Skill structure

```text
.
├── .codex-plugin/plugin.json      # Codex plugin manifest
├── SECURITY.md                    # Audit remediation and residual limitations
├── benchmarks/                    # Development evaluation assets excluded from the Skill
│   ├── cases/                     # Reproducible inputs
│   └── tools/                     # Evaluation and simulation programs
├── scripts/
│   ├── install_personal_plugin.py # Personal marketplace installer
│   ├── validate_plugin.py         # Portable plugin validator
│   └── validate_skill.py          # Portable Skill validator
└── skills/agent-auto-router/
    ├── SKILL.md                   # Codex execution instructions and reference routing
    ├── agents/openai.yaml         # Skill UI metadata
    ├── references/
    │   ├── entrypoints.md         # Complete entrypoints and host execution flow
    │   ├── router-contract.md     # Routing, permission, privacy, and failure contract
    │   ├── benchmark-routing.md   # Benchmark-prior update rules
    │   └── guarded-auto-learning.md
    └── scripts/
        ├── aar.ps1                # Beginner run/doctor entrypoint
        ├── quick_profiles.json    # Fixed safe and standard presets
        ├── invoke_auto_task.ps1   # Desktop and CLI expert entrypoint
        ├── invoke_orchestrated_task.ps1
        ├── route_contract.py      # Strict route-decision and execution-envelope contracts
        ├── doctor.py              # Privacy-safe zero-call diagnostics
        ├── host_execution_plan.py # Generic host plan
        ├── model_registry.json
        ├── guarded_auto.py
        └── install.ps1            # Standalone Skill compatibility installer
```

`SKILL.md` contains only the core workflow another Codex instance needs to execute tasks. User-facing installation, examples, and maintenance guidance live in the README, while detailed protocols live under `references/`.

## Common blockers

| Status or error | Meaning and resolution |
| --- | --- |
| `desktop_host_permissions_required` | The current Desktop turn did not provide a trusted permission snapshot; do not synthesize one from task text |
| `desktop_model_unavailable` | The runtime did not declare the selected model; change the available models or select explicitly, without silent substitution |
| `guarded-auto-state-writable-by-child` | A child can modify learning evidence; move state outside writable roots or return to `observe` mode |
| `failed_no_workspace_changes` | A write task returned success without changing Git status; check the task or explicitly use `-AllowNoChanges` |
| `workspace_status_unknown` | Bounded Git status could not be trusted; restore Git/metadata access or use read-only execution |
| `results_dir_writable_by_child` | The report destination is child-writable; move it outside every writable root or omit it for full-access execution |
| `report_privacy_boundary_unverified` | A sensitive Windows report DACL could not be verified; choose a private destination and restore PowerShell ACL support |
| Old behavior after installation | Restart Codex and compare the source with the installed copy; editing only the repository does not reinstall the plugin |
| Two Skills with the same name | The plugin and standalone Skill editions are both enabled; retain one edition without deleting learning state |

## Uninstall

Remove the plugin edition using the `marketplaceName` printed by the installer. A newly created personal marketplace is named `personal` by default:

```powershell
codex plugin remove agent-auto-router@personal
```

This removes the installed Codex configuration and cache. It does not automatically delete `~/plugins/agent-auto-router` or the source entry in the personal marketplace, so the plugin can be installed again. If the installer reported a marketplace name other than `personal`, use that actual name instead.

For the standalone Skill edition, remove the installed Skill directory:

```powershell
Remove-Item -LiteralPath "$HOME/.codex/skills/agent-auto-router" -Recurse -Force
```

Learning state remains under `~/.codex/auto-router` by default and is not deleted with the Skill. This prevents accidental deletion of active policy, feedback, audit, and rollback history. Review and back up that directory separately before removing it.

## License

This project is licensed under the MIT License. See [LICENSE](LICENSE).
