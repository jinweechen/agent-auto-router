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
- Approval-gated learning: `scripts/policy_learning.py`.
- Registry validation: `scripts/validate_model_registry.py`.

Read `references/entrypoints.md` for complete commands and backend-specific parameters. Read `references/router-contract.md` before changing routing, execution, privacy, or failure boundaries.
Read `references/benchmark-routing.md` before updating benchmark evidence or its routing floors.

## Execute through Codex Desktop

Treat Desktop execution as a host protocol, not a hidden CLI login:

1. Read exact supported model IDs from the current `spawn_agent` tool metadata. Never infer availability from the registry or substitute another model.
2. Build `agent-auto-router.host-permissions.v1` from the current host's trusted turn metadata. Copy the current sandbox policy, approval policy, network flag, writable roots, permission-profile ID, and permission-request capability; never infer them from task text or arbitrary environment variables.
3. Run the Desktop entrypoint with those IDs, the exact workdir, and the permission snapshot. It emits `agent-auto-router.desktop-plan.v2`, makes zero routing-model calls, and omits task text.
4. For `executionRequested=false`, report the plan only and launch nothing.
5. For `status=blocked`, report `blocked.code` and launch nothing.
6. For a ready executable plan, call `spawn_agent` exactly once using `agent.model`, `agent.reasoningEffort`, and `fork_turns=agent.forkTurns`. Pass the complete original task and require work only in `agent.workdir` because v2 forbids full-history forks.
7. Make that `direct` child the only writer. The primary agent may coordinate and verify read-only, must not edit concurrently, and must wait for the child before reporting.

```powershell
& "<skill-dir>/scripts/invoke_auto_task.ps1" `
  -Task "Implement the requested change" `
  -ExecutionBackend desktop `
  -DesktopAvailableModels @('gpt-5.6-sol', 'gpt-5.6-terra') `
  -HostPermissionsJson $currentTurnPermissionsJson `
  -Workdir "C:/path/to/workspace"
```

Desktop v2 supports only direct A/E/F topology. It automatically inherits `read-only`, `workspace-write`, or `danger-full-access` from the current Desktop turn and never broadens it. Missing or invalid trusted permission metadata, an out-of-root workdir, unavailable models, B/C/D topology, validation escalation, and non-default CLI context mode block before launch. `-DryRun` still emits a complete plan but sets `executionRequested=false` and `plannedAgentCalls=0`.

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

- Use CLI orchestration only when the task has both parallel signals and sufficient scale. Keep planner, dispatcher, worker, and grader read-only; only `direct` or final `reviewer` may write.
- Keep learning bounded to tier thresholds. Require human labels, held-out improvement, integrity checks, explicit approval, audit history, and rollback.
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
- Never grant concurrent writers or automatically retry timed-out `max`/`xhigh` roles.
- Never activate learned policy changes without explicit human approval.

After changes, run the full unit suite, offline evaluation, registry validation, and Skill validation. Keep installation, commits, and pushes behind explicit user confirmation.

## Using this skill from another host

When running inside Codex, Claude Code, or another host that can execute tasks itself but has no compatible `spawn_agent`:

1. Run `select_auto_model.py` with the task text to produce a route JSON decision.
2. Feed the route plus a trusted `agent-auto-router.host-permissions.v1` snapshot into `host_execution_plan.py` to get the host-neutral dispatch action (`cli` / `host_execute` / `orchestrate`).
3. Act on the plan's `action.kind`: for `cli`, invoke the declared backend with the exact model and effort; for `host_execute`, the host runs the task with its own model and surfaces approximate model accuracy; for `orchestrate`, dispatch multi-role orchestration through the selected CLI backend.

See `references/entrypoints.md` for the full command reference.

Formerly known as codex-auto-router.
