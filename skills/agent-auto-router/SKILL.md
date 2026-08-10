---
name: agent-auto-router
description: Automatically select trusted registered models across Codex/Claude backends with deterministic local routing, emit host-neutral execution plans for Codex, Claude Code, or other capable tools, execute through supported signed-in CLIs, safely evaluate role-based orchestration, validate model-registry extensions, or calibrate routing. Use when the user asks for Auto model selection, no-API-key routing, route explanations, model extension, calibration, or multi-model orchestration.
---

# Route Agent Tasks Automatically

Make one deterministic local routing decision, then emit a host-neutral action or use a supported signed-in CLI. Never treat Auto as a separate model or mutate the current conversation model.

## Choose the workflow

- Desktop task: `scripts/invoke_auto_task.ps1 -ExecutionBackend desktop`.
- CLI task: `scripts/invoke_auto_task.ps1 -ExecutionBackend cli`.
- CLI multi-role execution: `scripts/invoke_orchestrated_task.ps1`.
- Generic Codex/Claude Code/other-host plan: `scripts/host_execution_plan.py`.
- Offline routing evaluation: `scripts/evaluate_auto_router.py`.
- Manual learning: `scripts/policy_learning.py`.
- Guarded automatic learning: `scripts/guarded_auto.py`.
- Registry validation: `scripts/validate_model_registry.py`.

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
8. Wait for every launched child, stop on failure/blocked/cancelled status, release the writer claim, and report the final writer plus optional grader result. Do not retry timed-out `max`/`xhigh` roles automatically.
9. When `learning.submitAfterExecution=true`, complete the declared execution-report route with a unique report ID, trusted host name, status, duration, validation result, escalation flag, and attempt count; pipe it to `guarded_auto.py report --stdin`. Never add task text or child output. Do not submit DryRun plans.

```powershell
& "<skill-dir>/scripts/invoke_auto_task.ps1" `
  -Task "Implement the requested change" `
  -ExecutionBackend desktop `
  -DesktopAvailableModels @('<runtime-model-id>', '<another-runtime-model-id>') `
  -DesktopMaxParallelChildren 3 `
  -HostPermissionsJson $currentTurnPermissionsJson `
  -Workdir "C:/path/to/workspace"
```

Desktop v3 supports A-F. A/E/F remain direct; B/C/D use a staged multi-agent DAG with bounded workers and exactly one final writer. The selected direct model must be available exactly. For other roles, use the preferred profile model when available; otherwise the planner may explicitly resolve only to a runtime-declared trusted Codex model at the same or a higher tier. Report `preferredModel`, actual `model`, and `modelResolution`; never downgrade, cross backends, or hide the change. The call budget, parallel capacity, dependencies, idempotency keys, lifecycle states, and exclusive writer claim are deterministic plan fields. Missing trusted metadata, an out-of-root workdir, unresolved role models, insufficient call budget, validation escalation, and non-default CLI context mode block before launch. `-DryRun` returns the same non-executable plan with `plannedAgentCalls=0`.

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

The CLI backend preserves explicit model and effort, passes task text over UTF-8 stdin, and automatically derives its sandbox and approval policy from the trusted host snapshot. Writable roots are forwarded only from that snapshot, and an explicit `-Sandbox` may tighten but never broaden the host sandbox. Block permission combinations the child CLI cannot reproduce safely. Do not copy host connectors, credentials, or signed-in sessions into a child CLI. It may record privacy-minimized route outcomes and CLI-observable tokens; it never stores task text, model output, tool output, or credentials.

Use validation-driven escalation only when explicitly requested with an argv-array validation command. Permit at most one next-tier attempt after a successful model run fails validation. Authentication, provider, availability, sandbox, network, and other CLI failures stop without escalation.

## Use advanced workflows

- Use multi-agent orchestration only when the task has both parallel signals and sufficient scale. Keep planner, dispatcher, worker, and grader read-only; only `direct` or final `reviewer` may write.
- Keep learning bounded to tier thresholds. Manual candidates require explicit approval. The separately opted-in `guarded-auto` mode may auto-canary only a held-out-improving, integrity-checked, one-step conservative threshold decrease supported by explicit user selections or validation-proven adjacent-tier escalations; require deterministic canary, probation, audit history, and automatic rollback.
- Keep model identities in `model_registry.json` and role mappings in `orchestration_profiles.json`. New models start explicit-only and enter Auto only after controlled validation.
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
- When guarded-auto is enabled, keep its state and feedback outside every child-writable root. Block guarded execution under `danger-full-access`, an unknown external sandbox, or any boundary that lets the child modify learning evidence.
- Serialize guarded lifecycle, approval, rollback, and feedback mutations with bounded OS file locks; an abandoned lock file must never keep learning permanently busy.
- Bind feedback, candidates, canary statistics, and activation to the current routing feature schema. Preserve legacy records for audit, but never train or validate a new candidate with legacy feature semantics.
- Never grant concurrent writers or automatically retry timed-out `max`/`xhigh` roles.
- Never auto-change model registry entries, risk rules, permissions, Skill instructions, or thresholds toward a cheaper/weaker tier. Those changes and all manual candidates require explicit human approval.

After changes, run the full unit suite, offline evaluation, registry validation, and Skill validation. Keep installation, commits, and pushes behind explicit user confirmation.

## Using this skill from another host

When running inside Codex, Claude Code, or another host that can execute tasks itself but has no compatible `spawn_agent`:

1. Run `select_auto_model.py` with the task text to produce a route JSON decision.
2. Feed the route plus a trusted `agent-auto-router.host-permissions.v1` snapshot into `host_execution_plan.py` to get the host-neutral dispatch action (`cli` / `host_execute` / `orchestrate`).
3. Act on the plan's `action.kind`: for `cli`, invoke the declared backend with the exact model and effort; for `host_execute`, the host runs the task with its own model and surfaces approximate model accuracy; for `orchestrate`, dispatch multi-role orchestration through the selected CLI backend.

See `references/entrypoints.md` for the full command reference.
