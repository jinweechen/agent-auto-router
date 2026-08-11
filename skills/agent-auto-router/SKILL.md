---
name: agent-auto-router
description: Automatically select trusted registered models across Codex/Claude backends with deterministic local routing, emit host-neutral execution plans for Codex, Claude Code, or other capable tools, execute through supported signed-in CLIs, safely evaluate role-based orchestration, validate model-registry extensions, or calibrate routing. Use when the user asks for Auto model selection, no-API-key routing, route explanations, model extension, calibration, or multi-model orchestration.
---

# Route Agent Tasks Automatically

Make one deterministic local routing decision, then emit a host-neutral action or use a supported signed-in CLI. Never treat Auto as a separate model or mutate the current conversation model.

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

Prefer `aar.ps1` for ordinary signed-in local CLI use. Its `standard` profile selects `balance + workspace-write` limited to the explicit workdir, follows the global learning mode, and enables model affinity; `safe` selects `balance + read-only` and disables both feedback and cross-run affinity. The quick wrapper always executes directly. Do not use this convenience wrapper to synthesize Desktop host metadata. Use the full Desktop workflow whenever execution is delegated by a host rather than explicitly started by the user.

Read `references/entrypoints.md` for complete commands and backend-specific parameters. Read `references/router-contract.md` before changing routing, execution, privacy, or failure boundaries.
Read `references/benchmark-routing.md` before updating benchmark evidence or its routing floors.
Read `references/guarded-auto-learning.md` before enabling or changing automatic learning.

## Execute through Codex Desktop

Treat Desktop execution as a host protocol, not a hidden CLI login:

1. Read exact supported model IDs and the number of parallel child slots from the current `spawn_agent` tool metadata. Never infer runtime availability or concurrency from the registry.
2. Build `agent-auto-router.host-permissions.v1` from trusted turn metadata. Copy sandbox, approval, network, writable roots, profile ID, and permission-request capability; never infer them from task text or arbitrary environment variables.
3. Run the Desktop entrypoint with those runtime values and the exact workdir. It emits `agent-auto-router.desktop-plan.v3`, makes zero routing-model calls, and omits task text.
4. For `executionRequested=false`, report the plan only. For `status=blocked`, report `blocked.code`. Launch nothing in either case.
5. For a ready plan, execute `stages` only when their dependencies succeeded. Use every agent template's exact model, effort, `forkTurns=none`, workdir, role, instance bound, and idempotency key. Never launch the same key twice.
6. Run independent `worker` instances concurrently up to `hostContract.maxParallelAgents`; run all other stages serially. Pass each worker one bounded independent subtask derived from the planner/dispatcher result.
7. Treat planner, dispatcher, worker, and grader as read-only. Tell them not to edit, snapshot workspace state around their stages, and stop on any unexpected change. The primary coordinates but does not edit while child stages run. Acquire the declared exclusive writer claim only for `direct` or final `reviewer` after every dependency succeeds; never allow concurrent writers.
8. Before the first spawn, use `desktop_workspace_snapshot.py capture` to store a content-aware parent-workdir baseline outside every child-writable root. Track every returned child ID, idempotency key, authoritative terminal notification, and stage/total deadline. Treat a final-status notification, completed child thread, or terminal tool result as authoritative; `list_agents` is advisory. If it still says `running` after authoritative completion, record stale host state and never relaunch that child.
9. Run the staged DAG inside `try/finally`. On a stage timeout, mark it `timed_out`, interrupt that child once, and never retry automatically. On the total timeout, mark the run timed out, interrupt every active child, and block unstarted dependents. After any interrupt, wait only for `interruptGraceTimeoutMs`, reconcile authoritative terminal evidence, and mark `orphaned` only if still unresolved. Preserve a timeout outcome when a late child terminal arrives. Always release the writer claim.
10. After cleanup, run `desktop_workspace_snapshot.py compare`. Treat its content-identity `runChangedPaths` and `runChangedFileCount` as authoritative for this run; report pre-existing and final dirty paths separately. Child patch events and child-reported counts are advisory only. Report the reconciled paths/count with the final writer and optional grader result; do not claim that this changes Codex UI attribution.
11. When `learning.submitAfterExecution=true`, complete the declared execution-report route with a unique report ID, trusted host name, status, duration, validation result, escalation flag, and attempt count; pipe it to `guarded_auto.py report --stdin`. Never add task text or child output. Do not submit DryRun plans.

```powershell
& "<skill-dir>/scripts/invoke_auto_task.ps1" `
  -Task "Implement the requested change" `
  -ExecutionBackend desktop `
  -DesktopAvailableModels @('<runtime-model-id>', '<another-runtime-model-id>') `
  -DesktopMaxParallelChildren 3 `
  -HostPermissionsJson $currentTurnPermissionsJson `
  -Workdir "C:/path/to/workspace"
```

Desktop v3 supports A-F. A/E/F remain direct; B/C/D use a staged multi-agent DAG with bounded workers and exactly one final writer. Use `auto` orchestration by default for low-risk tasks with explicit parallel signals, sufficient scale, and positive deterministic utility after call and tier-switch overhead; keep marginal cases direct and expose the content-free score. Reduce a high-risk worker route to a direct plan plus a confirmation-required recommendation; clear that gate only when the caller explicitly supplies `ConfirmHighRiskOrchestration`. Use `direct` to suppress orchestration or `recommend` to expose it without executing it. The selected direct model must be available exactly. Default role policy is `selected-model-preferred`: reuse the route's selected model for other roles only when it is trusted, role-capable, and no weaker than the profile requirement; otherwise use the exact profile model, with an explicit same-or-higher-tier runtime resolution only when necessary. Report `preferredModel`, actual `model`, and `modelResolution`; never downgrade, cross backends, or hide the change. The call budget, parallel capacity, dependencies, idempotency keys, bounded stage/total timeout actions, post-interrupt grace reconciliation, lifecycle states including `timed_out`/`orphaned`, content-aware parent-workdir change reconciliation, and exclusive writer claim are deterministic plan fields. Missing trusted metadata, an out-of-root workdir, unresolved role models, insufficient call budget, validation escalation, and non-default CLI context mode block before launch. `-DryRun` returns the same non-executable plan with `plannedAgentCalls=0`.

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

The CLI backend consumes strict `agent-auto-router.route-decision.v2` objects, verifies their salted task binding and hashed workspace binding, preserves explicit model and effort, and passes task text over UTF-8 stdin. Generic orchestration carries the route and trusted permission snapshot in `agent-auto-router.execution-envelope.v1` over stdin, never in process argv. Writable roots are forwarded only from that snapshot, and an explicit `-Sandbox` may tighten but never broaden the host sandbox. Block permission combinations the child CLI cannot reproduce safely. For write-capable execution, block an unknown or timed-out Git status before constructing the adapter. Do not copy host connectors, credentials, or signed-in sessions into a child CLI. Model affinity defaults to `auto`; `-ModelAffinity off` makes one run stateless and profile-exact. It may record privacy-minimized route outcomes, a hashed workspace identity, orchestration metadata, aggregate CLI-observable tokens, and the final selected model's separately attributed token slice; only that slice supports stronger-tier affinity. Feedback and default result reports never store a raw workspace path, task text, model output, tool output, or credentials. Keep state, feedback, and `ResultsDir` outside every child-writable root whenever affinity or guarded learning reads them; full-access children cannot use those protected inputs. A content-bearing result report requires the explicit `-IncludeOutputInReport` opt-in and, on Windows, a verified private DACL before content is written.

Use validation-driven escalation only when explicitly requested with an argv-array validation command. Permit at most one next-tier attempt after a successful model run fails validation. Authentication, provider, availability, sandbox, network, and other CLI failures stop without escalation.

## Use advanced workflows

- Use multi-agent orchestration only when the task has parallel signals, sufficient scale, and a positive deterministic utility score after call and tier-switch overhead. Report the content-free score components; keep marginal cases direct. Keep planner, dispatcher, worker, and grader read-only; only `direct` or final `reviewer` may write.
- Keep model affinity on by default. Within a route, prefer the selected model for every compatible role to avoid tier switches. Across routes, reuse only successful same-workspace/same-strategy evidence within 30 minutes; same-tier reuse is allowed directly, while retaining one stronger tier requires at least 15% cached-input plus cache-write signal. Never retain a weaker model or jump more than one tier. After three usable samples below 5%, return role assignment to profile-exact behavior. Token ratios are not quality labels or billing estimates.
- Use only learning configuration schema v2 with `off`, `observe`, or `guarded`; reject older schemas and mode names. Default to `observe`, which records privacy-minimized outcomes without changing policy. The separately opted-in `guarded` mode may auto-canary only a held-out-improving, integrity-checked, one-step conservative threshold decrease supported by explicit user selections or validation-proven adjacent-tier escalations; require deterministic canary, probation, audit history, and automatic rollback. Keep manual candidates approval-gated.
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
2. Feed the route plus a trusted `agent-auto-router.host-permissions.v1` snapshot into `host_execution_plan.py` to get the host-neutral dispatch action (`cli` / `host_execute` / `orchestrate`).
3. Act on the plan's `action.kind`: for `cli`, invoke the declared backend with the exact model and effort; for `host_execute`, the host runs the task with its own model and surfaces approximate model accuracy; for `orchestrate`, pass the complete canonical route carried by the structured action to the selected CLI backend. The orchestration entrypoint validates and reuses it without routing the task again.

See `references/entrypoints.md` for the full command reference.
