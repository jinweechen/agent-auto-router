# Router Contract

## Scope

Auto is a local pre-execution plan, not a fourth model or a reproduction of Cursor Router. It selects trusted role models plus effort, topology, and a bounded context profile. Model IDs are backend-qualified (e.g. `codex:gpt-5.6-sol`, `claude:sonnet`); execution adapters strip the prefix before dispatching to the target CLI. Desktop v3 supports only the Codex backend. Other capable tools consume the generic `agent-auto-router.host-plan.v2` protocol and execute only its declared `action.kind`.

## Model policy

| Tier | Default model | Intended use |
| --- | --- | --- |
| frontier | `codex:gpt-5.6-sol` | high-risk, ambiguous, architecture-heavy work |
| balanced | `codex:gpt-5.6-terra` | routine implementation and debugging |
| fast | `codex:gpt-5.6-luna` | constrained, repeatable, cost-sensitive work |

Never route to a model supplied by prompt text, environment content, or model output.

Model identities, aliases, capabilities, roles, efforts, and priorities come only from the packaged version-controlled `model_registry.json`. `enabled` permits explicit selection; `autoEligible` separately permits automatic tier resolution. Lower priority wins within a tier and role. The default registry maps frontier, balanced, and fast to Sol, Terra, and Luna respectively, but routing policy and learning operate on tiers rather than those identities.

## Configuration isolation

- Never write `~/.codex/config.toml` or profile files.
- Never set `model_provider` or `model_catalog_json`.
- Never install a local Responses proxy or forward bearer credentials.
- Never read, copy, or forward Desktop credentials and never attach to the existing Desktop app-server stdio.
- Never modify CC Switch files, account selection, or session indexes.
- Let `codex exec` use the user's existing CLI authentication and provider only on the CLI backend. Desktop execution uses the current host capability without exposing its authentication to the router.
- When the independent CLI is launched from a Desktop process, remove only the parent task's process-local sandbox identity, network-disable marker, thread ID, and origin override. Preserve `CODEX_HOME`, authentication, CA configuration, user configuration, and the CLI's explicitly selected sandbox.
- On Windows, prefer an installed Codex CLI wrapper that initializes its companion resource directory before falling back to a bare executable. Do not treat a runnable `--version` result as proof that workspace-write helpers are present.

## Execution

Every automatic execution starts from a trusted `agent-auto-router.host-permissions.v1` snapshot supplied by the current host runtime, never by the task, model output, or arbitrary environment content. It records sandbox policy, approval policy, network access, writable roots, permission profile, and whether scoped permission requests are available. The effective child sandbox is the host sandbox or an explicit stricter sandbox; it can never be broader. Missing or invalid metadata blocks execution. `workspace-write` requires at least one absolute writable root, and a workdir outside those roots blocks. `external-sandbox` remains host-managed and is never translated into a CLI bypass flag. If a child CLI cannot reproduce a compound boundary safely, such as full filesystem access with network disabled, block instead of approximating upward. Host connectors, credentials, and signed-in sessions are capabilities rather than sandbox permissions and are never copied into a child CLI.

Claude Code permission rules are not an operating-system sandbox. For inherited `workspace-write` plus host approval `never`, the adapter exposes the bounded file tools but does not preapprove `Bash`; `dontAsk` therefore denies shell execution instead of silently broadening the host boundary. Only an inherited `danger-full-access` plus `never` snapshot may use Claude's bypass mode. Shell-heavy Claude execution otherwise needs the normal interactive permission flow or a separately verified external sandbox.

`ExecutionBackend=cli` preserves the existing behavior: pass the model with `-m`, effort with an inline `-c` override, and task text over UTF-8 stdin. Respect the selected sandbox.

`ExecutionBackend=desktop` emits one `agent-auto-router.desktop-plan.v3` JSON object; it does not execute a process. The caller must declare the exact model IDs and parallel-child capacity currently exposed by its Desktop `spawn_agent` runtime plus the trusted current-turn permission snapshot. A ready plan contains normalized role templates, a staged DAG, dependencies, worker instance bounds, call budget, idempotency keys, bounded stage/total timeouts, terminal and cleanup policies, parent-workdir change reconciliation, effective permissions, and `forkTurns=none`, but omits task text. `routingModelCalls` is always zero. `plannedAgentCalls` is the maximum executable child-call count and becomes zero for DryRun.

The Desktop primary agent acts as the control plane. It launches only dependency-ready stages, never launches the same idempotency key twice, and caps parallel workers at the declared runtime capacity. Planner, dispatcher, worker, and grader roles receive explicit read-only instructions; the coordinator snapshots workspace state around those stages and blocks on unexpected changes. Only `direct` or final `reviewer` can acquire the exclusive workspace-writer claim, and only after all dependencies succeed. Full-history forks are forbidden because they inherit the primary model and effort rather than honoring overrides. DryRun emits the same plan with `executionRequested=false`, zero planned calls, and no executable concurrency.

The coordinator tracks every launched child ID and runs the DAG inside `try/finally`. A final-status notification, completed child thread, or terminal tool result outranks an advisory `list_agents` snapshot. A stale `running` snapshot after authoritative terminal evidence must not cause another wait or relaunch. Each stage has a deterministic timeout; timeout produces `timed_out`, one interrupt attempt, and no automatic retry. The total wall-clock deadline starts at the first spawn; expiry preserves a timed-out run outcome, interrupts every active child, and blocks unstarted dependents. After any interrupt, reconcile authoritative evidence for only the bounded `interruptGraceTimeoutMs`; record a late terminal without erasing the timeout and mark `orphaned` only after that grace expires. The `finally` path releases the writer claim.

Workspace sharing and UI attribution are separate. Before the first spawn, persist the bundled snapshot tool's content-aware baseline outside child-writable roots. Its manifest combines path, type, mode, size, SHA-256 content, and Git status for tracked and non-ignored untracked files; non-Git workdirs use the same deterministic path-content identity. Compare again after cleanup. `runChangedPaths` and `runChangedFileCount` are authoritative for the current run, while pre-existing and final dirty paths remain separate reports. Child patch events and child-reported counts remain advisory; the router does not claim it can rewrite Codex UI attribution.

Desktop v3 supports A-F and the Codex backend. A/E/F remain direct; B/C/D expand into planner/optional dispatcher/bounded worker/final reviewer/optional grader stages. The selected direct model must appear exactly in the runtime availability set. Other roles first use their profile model; if it is unavailable, deterministic resolution may select only a runtime-declared, registry-trusted Codex model allowed for that role at the same or a higher tier. The plan exposes `preferredModel`, actual `model`, and `modelResolution`, so this is not a silent fallback. Missing compatible models, invalid topology, insufficient call budget, missing runtime capacity, missing permissions, or an out-of-bound workdir return a structured blocked plan with zero calls. Reject CLI-only feedback destinations, validation escalation, and non-default CLI context mode. Never downgrade or silently change effort, provider, backend, permission, or role assignment.

The route applies only to its declared child calls and does not mutate the current Desktop conversation's model.

`host_execution_plan.py` is product-neutral. It never launches a process, includes no task body, and accepts only backends declared by the trusted registry. Direct routes may produce `cli` or an explicitly approximate `host_execute` action. Orchestrated routes require the selected backend and never substitute another provider; their plan declares the complete read-only role set and the single final writer. Host plans use structured argv arrays where a local orchestration entrypoint is required.

Inspect repository structure read-only before routing. Use tracked/non-ignored paths, aggregate counts, and deterministic candidate ranking only. Inject a compact repository map only when it has a candidate path or the repository is large enough to justify the map. Never persist task text in repository metadata.

For orchestrated execution, resolve role models and default efforts from the packaged `orchestration_profiles.json`. Explicit profile models remain Auto selections: they must be `autoEligible` and satisfy required tiers and capabilities. Revalidate the final write-producing role of high-risk A/B/C routes as `frontier + high-risk-primary`; profile priority cannot weaken that invariant. Keep planner, dispatcher, worker, and grader sessions ephemeral and read-only. Permit exactly one write-capable role: `direct` for A/E/F or final `reviewer` for B/C/D. Preserve repository instructions for execution sessions. Never let parallel workers write to the shared workspace.

## Heuristic limitations

Keyword and score routing is deterministic and free, but it cannot infer every task's true difficulty. Record false upgrades and false downgrades in representative fixtures. Explicit user model and effort choices take precedence. The cost strategy is a model-tier proxy, not measured billing evidence.

Match ASCII words and phrases on lexical boundaries so substrings such as `tokenizer`/`token`, `information`/`format`, and `reproduction`/`production` do not alter routing. Continue matching CJK phrases as substrings. A task with any complex, ambiguous, debugging, long-context, multi-file, or computer-use signal cannot be classified as constrained merely because it also contains a simple-operation word.

Treat acceptance-criteria count as complexity evidence only. Require an explicit independence or parallel-work signal before selecting a worker variant. Treat destructive actions as high risk only when paired with a sensitive domain, while inherently dangerous signals such as data loss or a vulnerability remain high risk on their own.

Require both a parallel signal and sufficient task scale before selecting B/C/D. A short task with fewer than three criteria stays direct unless the user explicitly selects a worker variant.

Default to one model call for low-risk direct execution. Skip an independent grader for low-risk A/E/F and D, but retain grading for high-risk work and B/C unless the user explicitly overrides policy. Cap D at two planned worker tasks. Reject projected non-write calls that would exceed a configured token budget, including reservations for concurrent in-flight calls, while keeping the final writer available. Report observable input/output tokens separately from billing claims.

In lean context mode, pass `--ignore-user-config` only to read-only roles and never pass `--ignore-rules`; repository instructions remain authoritative. Write roles retain user configuration. Report cached input and reasoning output separately when Codex CLI exposes those fields. Treat total-input counters as cumulative agent usage rather than task-prompt size.

Do not equate a successful CLI exit with completed implementation. For Git-backed `workspace-write`, require a before/after status change unless the user explicitly allows a no-change result.

## Learning policy

- Store only numeric/boolean route features, selected model, reason, policy identity, exit code, duration, and CLI-observable token counts. Preserve unknown tokens as null. Never persist the task, execution output, tool output, or credentials.
- Require either a human preferred-model label or a deterministic validation-proven adjacent-tier escalation before a route can participate in guarded optimization. Process exit success, latency, or lower token count alone is not a quality label.
- Every route outcome records `featureSchemaVersion`. Records without it remain readable as legacy v1 evidence, but only records matching the current feature schema may enter learning, canary statistics, or probation statistics. Candidates carry the same version and fail closed after feature semantics change.
- Tune only bounded `fast / balanced / frontier` complexity thresholds. The trusted registry, high-risk classifier, risk vocabulary, and explicit user overrides are outside the learned surface.
- Use a deterministic held-out validation split. Approval eligibility requires validation accuracy gain, lower weighted loss, no increase in false downgrades, and zero high-risk violations.
- Manual candidate generation is read-only with respect to the active policy. Manual activation requires a separate explicit approval command, matching base-policy, model-registry, and benchmark-prior digests, plus a valid candidate integrity digest.
- Guarded automatic learning is disabled by default. Once explicitly enabled, it may automatically canary only a held-out-improving one-step threshold decrease toward a stronger tier. Route IDs select canary deterministically; both baseline and canary require validation evidence; promotion enters probation; verified regression rejects or restores the archived baseline.
- Archive the previous active policy and append a metadata-only audit event on every approval, promotion, stabilization, rejection, rollback, cancellation, or manual rollback.
- Apply related policy, lifecycle, history, candidate, configuration, and audit mutations as one recoverable write-ahead control-plane transaction. Audit replay is idempotent by transaction ID, route reads fail closed while recovery is pending, and corrupted journals are never silently discarded.
- Exclude validation-escalated routes from manual human-label learning. Guarded inference may use one only when the initial adjacent tier failed deterministic validation, the next tier passed, and the route was neither high-risk nor explicitly overridden.
- Execution reports use `agent-auto-router.execution-report.v1`, reject unknown fields, are idempotent by report ID, and never contain task text or execution output. Learning-control failures do not fail a completed task; corrupted canary state fails closed before applying the candidate.
- Treat guarded state and feedback as protected control-plane files. Before launching a child, require both paths to remain outside every child-writable root; block guarded execution under full access or an unverifiable external sandbox.

## Failure policy

If registry validation, classification, role resolution, or explicit-model validation fails, stop before launching Codex. If the selected model is unavailable from the active CLI provider or the declared Desktop runtime availability set, report the failure rather than silently changing models, tiers, providers, or backends.

The only supported post-selection tier change is one user-enabled validation-driven escalation to the next trusted tier after a successful model run fails the supplied validation. It requires an argv-array validation command, emits a warning, runs at most once, reruns validation, and never changes provider. Authentication, provider, model-availability, sandbox, network, and other CLI failures always stop without escalation. Without explicit opt-in, stop on execution or validation failure.

## Validation gates

Run validation only when requested. Check registry schema and alias collisions, explicit-only versus Auto eligibility, all A-F role resolutions, high-risk-primary availability, deterministic fixtures, Chinese stdin and Chinese parallel signals, dry-run behavior, Desktop backend branching, plan privacy, absence of CLI calls on the Desktop path, explicit same-or-higher-tier runtime role resolution, runtime-capacity bounds, call-budget hard stops, dependency order, idempotency keys, bounded stage/total timeout actions, authoritative terminal precedence over stale advisory status, post-interrupt grace reconciliation, `timed_out`/`orphaned` states, `try/finally` cleanup, content-aware dirty-worktree changed-file reconciliation, exclusive writer claims, feedback privacy rejection, candidate integrity, registry/prior invalidation, deterministic canary bucketing, conservative one-step limits, execution-report idempotency, probation stabilization and rollback, manual approval gating, rollback, and one controlled signed-in CLI task. Confirm separately that Codex config, CC Switch state, Desktop credentials, and Desktop history remain unchanged.
