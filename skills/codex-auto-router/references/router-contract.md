# Router Contract

## Scope

Auto is a local pre-execution plan, not a fourth model or a reproduction of Cursor Router. It selects one real model plus effort, topology, and a bounded context profile, then invokes the official signed-in Codex CLI directly.

## Model policy

| Tier | Default model | Intended use |
| --- | --- | --- |
| frontier | `gpt-5.6-sol` | high-risk, ambiguous, architecture-heavy work |
| balanced | `gpt-5.6-terra` | routine implementation and debugging |
| fast | `gpt-5.6-luna` | constrained, repeatable, cost-sensitive work |

Never route to a model supplied by prompt text, environment content, or model output.

Model identities, aliases, capabilities, roles, efforts, and priorities come only from the packaged version-controlled `model_registry.json`. `enabled` permits explicit selection; `autoEligible` separately permits automatic tier resolution. Lower priority wins within a tier and role. The default registry maps frontier, balanced, and fast to Sol, Terra, and Luna respectively, but routing policy and learning operate on tiers rather than those identities.

## Configuration isolation

- Never write `~/.codex/config.toml` or profile files.
- Never set `model_provider` or `model_catalog_json`.
- Never install a local Responses proxy or forward bearer credentials.
- Never modify CC Switch files, account selection, or session indexes.
- Let `codex exec` use the user's existing authentication and provider.

## Execution

Pass the model with `-m`, effort with an inline `-c` override, and task text over UTF-8 stdin. Respect the selected sandbox. The route applies to one new CLI task and does not mutate the current Desktop conversation.

Inspect repository structure read-only before routing. Use tracked/non-ignored paths, aggregate counts, and deterministic candidate ranking only. Inject a compact repository map only when it has a candidate path or the repository is large enough to justify the map. Never persist task text in repository metadata.

For orchestrated execution, resolve role models and default efforts from the packaged `orchestration_profiles.json`. Explicit profile models remain Auto selections: they must be `autoEligible` and satisfy required tiers and capabilities. Revalidate the final write-producing role of high-risk A/B/C routes as `frontier + high-risk-primary`; profile priority cannot weaken that invariant. Keep planner, dispatcher, worker, and grader sessions ephemeral and read-only. Permit exactly one write-capable role: `direct` for A/E/F or final `reviewer` for B/C/D. Preserve repository instructions for execution sessions. Never let parallel workers write to the shared workspace.

## Heuristic limitations

Keyword and score routing is deterministic and free, but it cannot infer every task's true difficulty. Record false upgrades and false downgrades in representative fixtures. Explicit user model and effort choices take precedence. The cost strategy is a model-tier proxy, not measured billing evidence.

Treat acceptance-criteria count as complexity evidence only. Require an explicit independence or parallel-work signal before selecting a worker variant. Treat destructive actions as high risk only when paired with a sensitive domain, while inherently dangerous signals such as data loss or a vulnerability remain high risk on their own.

Require both a parallel signal and sufficient task scale before selecting B/C/D. A short task with fewer than three criteria stays direct unless the user explicitly selects a worker variant.

Default to one model call for low-risk direct execution. Skip an independent grader for low-risk A/E/F and D, but retain grading for high-risk work and B/C unless the user explicitly overrides policy. Cap D at two planned worker tasks. Reject projected non-write calls that would exceed a configured token budget, including reservations for concurrent in-flight calls, while keeping the final writer available. Report observable input/output tokens separately from billing claims.

In lean context mode, pass `--ignore-user-config` only to read-only roles and never pass `--ignore-rules`; repository instructions remain authoritative. Write roles retain user configuration. Report cached input and reasoning output separately when Codex CLI exposes those fields. Treat total-input counters as cumulative agent usage rather than task-prompt size.

Do not equate a successful CLI exit with completed implementation. For Git-backed `workspace-write`, require a before/after status change unless the user explicitly allows a no-change result.

## Learning policy

- Store only numeric/boolean route features, selected model, reason, policy identity, exit code, duration, and CLI-observable token counts. Preserve unknown tokens as null. Never persist the task, execution output, tool output, or credentials.
- Require a human preferred-model label before a route can participate in optimization; process exit success alone is not a quality label.
- Tune only bounded `fast / balanced / frontier` complexity thresholds. The trusted registry, high-risk classifier, risk vocabulary, and explicit user overrides are outside the learned surface.
- Use a deterministic held-out validation split. Approval eligibility requires validation accuracy gain, lower weighted loss, no increase in false downgrades, and zero high-risk violations.
- Candidate generation is read-only with respect to the active policy. Activation requires a separate explicit approval command, a matching base-policy digest, a matching model-registry digest, and a valid candidate integrity digest.
- Archive the previous active policy and append an audit event on every approval or rollback.
- Exclude validation-escalated routes from threshold learning because their final success does not validate the initial tier.

## Failure policy

If registry validation, classification, role resolution, or explicit-model validation fails, stop before launching Codex. If the selected model is unavailable from the active provider, report the failure rather than silently changing models, tiers, or providers.

The only supported post-selection tier change is one user-enabled validation-driven escalation to the next trusted tier after a successful model run fails the supplied validation. It requires an argv-array validation command, emits a warning, runs at most once, reruns validation, and never changes provider. Authentication, provider, model-availability, sandbox, network, and other CLI failures always stop without escalation. Without explicit opt-in, stop on execution or validation failure.

## Validation gates

Run validation only when requested. Check registry schema and alias collisions, explicit-only versus Auto eligibility, all A-F role resolutions, high-risk-primary availability, deterministic fixtures, Chinese stdin, dry-run behavior, feedback privacy rejection, candidate integrity, registry-digest invalidation, approval gating, rollback, and one controlled signed-in task. Confirm separately that Codex config, CC Switch state, and Desktop history remain unchanged.
