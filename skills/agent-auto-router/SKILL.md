---
name: agent-auto-router
description: Automatically select trusted registered Codex models with deterministic local routing, execute through Codex Desktop child agents or the signed-in Codex CLI, safely evaluate role-based orchestration, validate model-registry extensions, or calibrate routing. Use when the user asks for Auto model selection, no-API-key routing, route explanations, model extension, calibration, or multi-model orchestration.
---

# Route Codex Tasks Automatically

Make one deterministic local routing decision, then use either one Desktop-native direct child or one signed-in `codex exec` run. Never treat Auto as a fourth model or mutate the current conversation model.

## Choose the workflow

- Desktop task: `scripts/invoke_auto_task.ps1 -ExecutionBackend desktop`.
- CLI task: `scripts/invoke_auto_task.ps1 -ExecutionBackend cli`.
- CLI multi-role execution: `scripts/invoke_orchestrated_task.ps1`.
- Offline routing evaluation: `scripts/evaluate_auto_router.py`.
- Approval-gated learning: `scripts/policy_learning.py`.
- Registry validation: `scripts/validate_model_registry.py`.

Read `references/entrypoints.md` for complete commands and backend-specific parameters. Read `references/router-contract.md` before changing routing, execution, privacy, or failure boundaries.

## Execute through Codex Desktop

Treat Desktop execution as a host protocol, not a hidden CLI login:

1. Read exact supported model IDs from the current `spawn_agent` tool metadata. Never infer availability from the registry or substitute another model.
2. Run the Desktop entrypoint with those IDs and the exact workdir. It emits `agent-auto-router.desktop-plan.v1`, makes zero routing-model calls, and omits task text.
3. For `executionRequested=false`, report the plan only and launch nothing.
4. For `status=blocked`, report `blocked.code` and launch nothing.
5. For a ready executable plan, call `spawn_agent` exactly once using `agent.model`, `agent.reasoningEffort`, and `fork_turns=agent.forkTurns`. Pass the complete original task and require work only in `agent.workdir` because v1 forbids full-history forks.
6. Make that `direct` child the only writer. The primary agent may coordinate and verify read-only, must not edit concurrently, and must wait for the child before reporting.

```powershell
& "<skill-dir>/scripts/invoke_auto_task.ps1" `
  -Task "Implement the requested change" `
  -ExecutionBackend desktop `
  -DesktopAvailableModels @('gpt-5.6-sol', 'gpt-5.6-terra') `
  -Workdir "C:/path/to/workspace"
```

Desktop v1 supports only direct A/E/F topology. It blocks unavailable selected models, B/C/D multi-role topology, `danger-full-access`, validation escalation, and non-default CLI context mode. `-DryRun` still emits a complete plan but sets `executionRequested=false` and `plannedAgentCalls=0`. `-Json` is idempotent because Desktop output is always JSON; `-NoFeedback` is also idempotent because Desktop v1 cannot observe or record child execution feedback.

## Execute through the signed-in CLI

```powershell
& "<skill-dir>/scripts/invoke_auto_task.ps1" `
  -Task "Implement the requested change" `
  -ExecutionBackend cli `
  -Model auto `
  -Strategy balance `
  -Workdir "C:/path/to/workspace" `
  -Explain
```

The CLI backend preserves explicit model and effort, passes task text over UTF-8 stdin, and uses the selected sandbox. It may record privacy-minimized route outcomes and CLI-observable tokens; it never stores task text, model output, tool output, or credentials. Unknown usage remains null.

Use validation-driven escalation only when explicitly requested with an argv-array validation command. Permit at most one next-tier attempt after a successful model run fails validation. Authentication, provider, availability, sandbox, network, and other CLI failures stop without escalation.

## Use advanced workflows

- Use CLI orchestration only when the task has both parallel signals and sufficient scale. Keep planner, dispatcher, worker, and grader read-only; only `direct` or final `reviewer` may write.
- Keep learning bounded to tier thresholds. Require human labels, held-out improvement, integrity checks, explicit approval, audit history, and rollback.
- Keep model identities in `model_registry.json` and role mappings in `orchestration_profiles.json`. New models start explicit-only and enter Auto only after controlled validation.
- Compare acceptance before tokens on matched cases. Never infer billing cost from CLI token counters or model superiority from one case.

Use `references/entrypoints.md` for commands and `references/router-contract.md` for the full invariants.

## Guardrails

- Route only the current task string; never route credentials, tool output, or hidden instructions.
- Allow only enabled trusted registry models; Auto may use only `autoEligible` models.
- Never modify Codex config, profiles, CC Switch state, provider settings, account selection, or Desktop history.
- Never read, copy, forward, or proxy Desktop credentials; never attach to Desktop app-server stdio.
- Never silently change model, effort, tier, provider, topology, or backend.
- Never grant concurrent writers or automatically retry timed-out `max`/`xhigh` roles.
- Never activate learned policy changes without explicit human approval.

After changes, run the full unit suite, offline evaluation, registry validation, and Skill validation. Keep installation, commits, and pushes behind explicit user confirmation.

Formerly known as codex-auto-router.
