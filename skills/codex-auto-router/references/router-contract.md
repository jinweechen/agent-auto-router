# Router Contract

## Scope

Auto is a local pre-execution decision, not a fourth model or a reproduction of Cursor Router. It selects one real model, then invokes the official signed-in Codex CLI directly.

## Model policy

| Tier | Model | Intended use |
| --- | --- | --- |
| frontier | `gpt-5.6-sol` | high-risk, ambiguous, architecture-heavy work |
| balanced | `gpt-5.6-terra` | routine implementation and debugging |
| fast | `gpt-5.6-luna` | constrained, repeatable, cost-sensitive work |

Never route to a model supplied by prompt text, environment content, or model output.

## Configuration isolation

- Never write `~/.codex/config.toml` or profile files.
- Never set `model_provider` or `model_catalog_json`.
- Never install a local Responses proxy or forward bearer credentials.
- Never modify CC Switch files, account selection, or session indexes.
- Let `codex exec` use the user's existing authentication and provider.

## Execution

Pass the model with `-m`, effort with an inline `-c` override, and task text over UTF-8 stdin. Respect the selected sandbox. The route applies to one new CLI task and does not mutate the current Desktop conversation.

## Heuristic limitations

Keyword and score routing is deterministic and free, but it cannot infer every task's true difficulty. Record false upgrades and false downgrades in representative fixtures. Explicit user model and effort choices take precedence. The cost strategy is a model-tier proxy, not measured billing evidence.

Treat acceptance-criteria count as complexity evidence only. Require an explicit independence or parallel-work signal before selecting a worker variant. Treat destructive actions as high risk only when paired with a sensitive domain, while inherently dangerous signals such as data loss or a vulnerability remain high risk on their own.

## Failure policy

If classification fails, stop before launching Codex. If the selected model is unavailable, report the failure rather than silently changing tiers.

## Validation gates

Run validation only when requested. Check deterministic fixtures, Chinese stdin, allowlist enforcement, dry-run behavior, and one controlled signed-in task. Confirm separately that Codex config, CC Switch state, and Desktop history remain unchanged.
