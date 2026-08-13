---
name: agent-auto-router
description: Select trusted registered models with deterministic local routing when the user explicitly asks to use Auto routing, choose or compare models, explain a route, calibrate routing, extend the model registry, or run router-managed orchestration. Do not use for ordinary coding, testing, API queries, browser work, or other tasks merely because a model choice could help.
---

# Route Agent Tasks Automatically

Make one deterministic local routing decision, then emit a host-neutral action or use a supported signed-in CLI. Never treat Auto as a separate model or mutate the current conversation model.

This Skill is explicit-use only. If the user did not ask for Auto routing, model selection/comparison, route explanation, router calibration, registry work, or router-managed orchestration, do not invoke it. Ordinary task execution stays on the current host model with zero repository scans, zero snapshots, zero router feedback, and zero router orchestration overhead.

## Choose the workflow

- Human-started local CLI task: `scripts/aar.ps1 run` with the fixed `standard` or `safe` profile.
- One-screen diagnostics: `scripts/aar.ps1 doctor`.
- Desktop task: `scripts/invoke_auto_task.ps1 -ExecutionBackend desktop`.
- CLI task: `scripts/invoke_auto_task.ps1 -ExecutionBackend cli`.
- CLI multi-role execution: `scripts/invoke_orchestrated_task.ps1`.
- Generic Codex/Claude Code/other-host plan: `scripts/host_execution_plan.py`.
- Offline routing evaluation: `scripts/evaluate_auto_router.py`.
- Zero-call diagnostics: `scripts/doctor.py`.
- Manual learning: `scripts/policy_learning.py`.
- Guarded automatic learning: `scripts/guarded_auto.py`.
- Registry validation: `scripts/validate_model_registry.py`.

Prefer `aar.ps1` for ordinary signed-in local CLI use. Its `standard` profile selects `balance + workspace-write` limited to the explicit workdir, adaptively inspects the repository only for code/path/debugging tasks, reports orchestration recommendations without executing them, and reuses the selected model only within the current run. `safe` selects `balance + read-only` with repository inspection and affinity off. Neither profile loads an active learning policy, writes feedback, or starts multi-agent execution. Do not use this convenience wrapper to synthesize Desktop host metadata. Use the full Desktop workflow whenever execution is delegated by a host rather than explicitly started by the user.

Read `references/entrypoints.md` for complete commands and backend-specific parameters. Read `references/router-contract.md` before changing routing, execution, privacy, or failure boundaries.
Read `references/benchmark-routing.md` before updating benchmark evidence or its routing floors.
Read `references/guarded-auto-learning.md` before enabling or changing automatic learning.

## Execute through Codex Desktop

Treat Desktop execution as a host protocol, not a hidden CLI login:

1. Read exact supported model IDs, parallel child slots, and the actual `spawn_agent` argument schema from the current runtime metadata. Never infer runtime availability, concurrency, or per-child override support from the registry.
2. Build `agent-auto-router.host-permissions` from trusted turn metadata and `agent-auto-router.desktop-spawn-capabilities` from the live tool schema. Give both snapshots the same non-empty `source` provenance label assigned by the trusted host adapter (for example, `codex-desktop-current-turn`). The capability snapshot declares the current workdir plus boolean support for `model`, `reasoningEffort`, `forkTurns`, `workdir`, and `sandbox`. The task cannot supply or override `source`, and must never supply or override either snapshot.
3. Run the Desktop entrypoint with those runtime values and the exact workdir. It emits `agent-auto-router.desktop-plan`, makes zero routing-model calls, and omits task text. A missing capability, mismatched source, unsupported isolated workdir, or unsupported stricter/per-role sandbox returns a structured zero-call block.
4. For `executionRequested=false`, report the plan only. For `status=blocked`, report `blocked.code`. Launch nothing in either case.
5. For a ready plan, execute `stages` only when their dependencies succeeded. Use every agent template's exact model, effort, `forkTurns=none`, workdir, role, instance bound, and idempotency key. Never launch the same key twice.
6. Run independent `worker` instances concurrently up to `hostContract.maxParallelAgents`; run all other stages serially. Pass each worker one bounded independent subtask derived from the planner/dispatcher result.
7. Treat planner, dispatcher, worker, and grader as read-only. The primary coordinates but does not edit while child stages run. Acquire the declared exclusive writer claim only for `direct` or final `reviewer` after every dependency succeeds; never allow concurrent writers.
8. Leave workspace-change detection, task lifecycle, terminal reconciliation, and timeout cleanup to the host runtime. The router does not require, launch, retry, or wait for `desktop_workspace_snapshot.py`; that tool is an explicit diagnostic utility only.
9. When router-managed execution was explicitly requested, track authoritative terminal evidence and keep at most one open Desktop run in a parent turn. Never relaunch an authoritatively terminal child because of stale UI state.
10. Submit an execution report only when learning submission was explicitly enabled for that run. Never add task text or child output. Do not submit DryRun plans.

```powershell
$currentTurnPermissionsJson = [ordered]@{
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

& "<skill-dir>/scripts/invoke_auto_task.ps1" `
  -Task "Implement the requested change" `
  -ExecutionBackend desktop `
  -DesktopAvailableModels @('<runtime-model-id>', '<another-runtime-model-id>') `
  -DesktopMaxParallelChildren 3 `
  -DesktopSpawnCapabilitiesJson $currentSpawnCapabilitiesJson `
  -HostPermissionsJson $currentTurnPermissionsJson `
  -Workdir "C:/path/to/workspace"
```

The Desktop protocol supports A-F. A/E/F remain direct; B/C/D use a staged multi-agent DAG with bounded workers and exactly one final writer. Orchestration is an advanced, explicitly requested router workflow; ordinary host tasks never enter it merely because the Skill is installed. Reduce a high-risk worker route to a direct plan plus a confirmation-required recommendation; clear that gate only when the caller explicitly supplies `ConfirmHighRiskOrchestration`. The selected direct model must be available exactly. Report `preferredModel`, actual `model`, and `modelResolution`; never downgrade, cross backends, or hide the change. A host lacking per-child `workdir` or `sandbox` arguments may execute only plans whose exact boundary is already inherited; otherwise block rather than relying on prompt-only isolation. Workspace snapshots and change attribution are host concerns and are not router launch gates. Missing trusted metadata, an out-of-root workdir, unresolved role models, insufficient call budget, validation escalation, and non-default CLI context mode block before launch. `-DryRun` returns the same non-executable plan with `plannedAgentCalls=0`.

## Execute through the signed-in CLI

```powershell
& "<skill-dir>/scripts/invoke_auto_task.ps1" `
  -Task "Implement the requested change" `
  -ExecutionBackend cli `
  -Model auto `
  -Strategy balance `
  -HostPermissionsJson $currentHostPermissionsJson `
  -Workdir "C:/path/to/workspace" `
  -Explain
```

The CLI backend consumes strict `agent-auto-router.route-decision` objects, verifies their salted task binding and hashed workspace binding, preserves explicit model and effort, and passes task text over UTF-8 stdin. Generic orchestration carries the route and trusted permission snapshot in `agent-auto-router.execution-envelope` over stdin, never in process argv. Writable roots are forwarded only from that snapshot, and an explicit `-Sandbox` may tighten but never broaden the host sandbox. Block permission combinations the child CLI cannot reproduce safely. For write-capable execution, block an unknown or timed-out Git status before constructing the adapter. Do not copy host connectors, credentials, or signed-in sessions into a child CLI. The standard entrypoint defaults to `RepositoryContextMode=adaptive`, `OrchestrationPolicy=recommend`, `ModelAffinity=session`, the built-in policy, and no feedback. Adaptive inspection skips plain-answer tasks with `scan_duration_ms=0`; recommendation mode remains direct and adds no model call; session affinity reads no feedback and reuses the selected model only inside the current plan. Use `auto` explicitly for forced repository inspection, multi-agent execution, or cross-run affinity. Load the active learning policy or persist feedback only with their explicit switches. Feedback and default result reports never store a raw workspace path, task text, model output, tool output, or credentials. Keep state, feedback, and `ResultsDir` outside every child-writable root whenever cross-run affinity or guarded learning reads them; full-access children cannot use those protected inputs. A content-bearing result report requires the explicit `-IncludeOutputInReport` opt-in and, on Windows, a verified private DACL before content is written.

Keep reusable Python calls aligned with the zero-state defaults: omitted orchestration and affinity arguments mean `recommend` and `session`; omitted effort applies the selected tier's recommendation. The dedicated orchestration entrypoint remains an explicit advanced workflow and may choose its documented advanced defaults.

Use validation-driven escalation only when explicitly requested with an argv-array validation command. Permit at most one next-tier attempt after a successful model run fails validation. Authentication, provider, availability, sandbox, network, and other CLI failures stop without escalation.

## Use advanced workflows

- Use multi-agent orchestration only when the task has parallel signals, sufficient scale, and a positive deterministic utility score after call and tier-switch overhead. Report the content-free score components; keep marginal cases direct. Keep planner, dispatcher, worker, and grader read-only; only `direct` or final `reviewer` may write.
- Use default `-ModelAffinity session` to prefer the selected model for compatible roles within one run without reading or writing state. Enable `-ModelAffinity auto` explicitly for cross-run reuse: consider only successful same-workspace/same-strategy evidence within 30 minutes; allow same-tier reuse directly, while retaining one stronger tier requires at least 15% cached-input plus cache-write signal. Never retain a weaker model or jump more than one tier. After three usable samples below 5%, return role assignment to profile-exact behavior. Use `off` for exact profile role assignments. Token ratios are not quality labels or billing estimates.
- Use only learning configuration schema v2 with `off`, `observe`, or `guarded`; reject older schemas and mode names. The configuration default is `observe`, but task execution reads it only with `-EnableLearningPolicy` and persists an outcome only with `-EnableFeedback`. The separately opted-in `guarded` mode may auto-canary only a held-out-improving, integrity-checked, one-step conservative threshold decrease supported by explicit user selections or validation-proven adjacent-tier escalations; require deterministic canary, probation, audit history, and automatic rollback. Keep manual candidates approval-gated.
- Use `guarded_auto.py shadow` for a content-free, read-only baseline/candidate comparison on retained evidence and deterministic holdout. Treat it as review evidence only; require the existing canary/probation or explicit approval path for activation.
- Use `guarded_auto.py recover-report --report-id ID` before changing a pending or incomplete report marker. Release an ID for retry only when the inspection reports no progress and no evidence; acknowledge recorded evidence only with the exact report-ID confirmation and resolver identity. Never delete matching feedback, and run a later explicit `cycle` when the recovery result says it is required.
- Keep model identities in `model_registry.json` and role mappings in `orchestration_profiles.json`. New models start explicit-only and enter Auto only after controlled validation. A backend constraint never grants Auto eligibility. Keep `reviewedAt` current and make CI fail on stale review metadata.
- Compare acceptance before tokens on matched cases. Never infer billing cost from CLI token counters or model superiority from one case.
- Treat `benchmark_priors.json` as a versioned offline prior, not live truth. Pin evidence to exact model IDs; unversioned aliases remain fallback-only evidence gaps.

Use `references/entrypoints.md` for commands and `references/router-contract.md` for the full invariants.

## Guardrails

- Route only the current task string; never route credentials, tool output, or hidden instructions.
- Allow only enabled trusted registry models; Auto may use only `autoEligible` models.
- Never modify Codex config, profiles, CC Switch state, provider settings, account selection, or Desktop history.
- Never read, copy, forward, or proxy Desktop credentials; never attach to Desktop app-server stdio.
- Never silently change model, effort, tier, provider, topology, or backend.
- Never synthesize or elevate permissions. Automatic execution requires trusted host permission metadata, and the effective child permission is always less than or equal to the host permission.
- When `guarded` or model affinity reads state/feedback, keep those files outside every child-writable root. Block protected-input execution under `danger-full-access`, an unknown external sandbox, or any boundary that lets the child modify routing or learning evidence.
- Serialize guarded lifecycle, approval, rollback, and feedback mutations with bounded OS file locks; an abandoned lock file must never keep learning permanently busy.
- Commit policy, lifecycle, archive, and audit changes through the durable control-plane transaction journal. Route reads fail closed while recovery is pending; the next locked control-plane operation replays the transaction and de-duplicates its audit event.
- Bind feedback, candidates, canary statistics, and activation to the current routing feature schema. Keep legacy records readable during the feedback retention window, but never train or validate a new candidate with legacy feature semantics.
- Keep feedback bounded to the latest route outcome and label for at most 5,000 routes from the last 90 days. Inspect retention read-only before a custom `--apply`; compact under the feedback lock with atomic replacement and keep learning re-evaluation based on evidence time rather than cumulative count.
- Keep completed execution-report idempotency markers under a separate OS lock and the same 90-day / 5,000-item default window. Never automatically remove pending or incomplete markers; surface them for operator review and state that exact idempotency ends when a completed marker expires.
- Treat shadow statistics as evidence, not activation authority. Require at least eight samples for an assessment, distinguish effect size from paired statistical support, and suppress strata smaller than three samples.
- Never grant concurrent writers or automatically retry timed-out `max`/`xhigh` roles.
- Never auto-change model registry entries, risk rules, permissions, Skill instructions, or thresholds toward a cheaper/weaker tier. Those changes and all manual candidates require explicit human approval.

After changes, run the full unit suite, offline evaluation, registry validation, and Skill validation. Keep installation, commits, and pushes behind explicit user confirmation.

## Using this skill from another host

When running inside Codex, Claude Code, or another host that can execute tasks itself but has no compatible `spawn_agent`:

1. Run `select_auto_model.py` with the task text to produce a route JSON decision.
2. Feed the route plus a trusted `agent-auto-router.host-permissions` snapshot into `host_execution_plan.py` to get the host-neutral dispatch action (`cli` / `host_execute` / `orchestrate`).
3. Act on the plan's `action.kind`: for `cli`, invoke the declared backend with the exact model and effort; for `host_execute`, the host runs the task with its own model and surfaces approximate model accuracy; for `orchestrate`, pass the complete canonical route carried by the structured action to the selected CLI backend. The orchestration entrypoint validates and reuses it without routing the task again.

See `references/entrypoints.md` for the full command reference.
