# Guarded automatic learning

`guarded-auto` is an optional local control loop for routing thresholds. It makes no model calls and is disabled by default. Enabling it once permits only the narrow automatic lifecycle described here; it does not authorize registry, risk, permission, prompt, Skill, provider, or account changes.

## Evidence boundary

An automatic signal must be one of:

- an explicit user preferred-model selection recorded with `preferenceSource=explicit-user-selection`; or
- a deterministic validation-proven escalation where the initial tier failed, exactly the adjacent stronger tier passed, at least two attempts were recorded, and the route was neither high-risk nor explicitly overridden.

Exit code zero, ordinary success, latency, and token count are observations, not quality labels. Task text, model output, child-agent output, tool output, credentials, and hidden instructions are rejected from the report schema and never stored.

## Allowed learned surface

The optimizer may change only `intelligenceFrontier`, `balanceFrontier`, and `costBalanced`. Automatic candidates must:

- use at least the configured number of usable signals and pass the deterministic held-out improvement gate;
- decrease a threshold by at most one, which routes more work to a stronger tier;
- preserve fixed high-risk and benchmark-prior floors;
- match the current base-policy, registry, and benchmark-prior digests;
- pass the candidate integrity digest.

Threshold increases toward cheaper or weaker tiers remain manual and approval-gated through `policy_learning.py`. Registry identities, `autoEligible`, capabilities, role mappings, risk rules, permissions, and Skill files are never automatically edited.

## Lifecycle

`idle -> canary -> probation -> idle`

1. `idle`: build a candidate only after enough strong signals exist.
2. `canary`: deterministically bucket opaque route IDs. At most the configured percentage, capped at 50%, sees the candidate; the rest stays on the baseline. Registry, prior, candidate, or active-policy drift fails closed.
3. `probation`: after both canary and baseline have enough deterministic validation reports and no allowed failure-rate regression, archive the baseline and activate the candidate temporarily.
4. `idle`: stabilize after sufficient probation reports, or automatically restore the archived baseline when verified failures regress. Every transition is metadata-only audited.

After a candidate is rejected, stabilized, or rolled back, the state records the strong-signal count and will not recompute the same evidence. A new evaluation requires another bounded batch of strong signals.

Only reports with deterministic validation results, no explicit override, and no high-risk flag count toward canary or probation acceptance. Learning failures do not turn a completed user task into a failed task, but corrupted canary state blocks candidate routing until it is inspected or guarded mode is disabled.

Feature extraction is explicitly versioned. New outcomes and candidates carry `featureSchemaVersion=2`; a record with no version remains readable as legacy v1 audit evidence, but it is excluded from labeled and inferred samples as well as canary/probation statistics. Any candidate built for another feature schema is stale and cannot be routed, approved, or promoted.

The state directory and any custom feedback file are protected control-plane inputs. Automatic execution checks them before launching a child: they must be outside every child-writable root. Guarded execution is blocked under `danger-full-access`, an unknown external sandbox, or any other boundary that lets the child edit its own evidence. Manual mode is unaffected.

Guarded lifecycle transitions, manual approval/rollback, and configuration changes share one bounded operating-system file lock. Feedback and audit JSONL streams use their own bounded append locks. The operating system releases an active lock when a process exits, so an abandoned `.guarded-auto.lock` file is inert rather than a permanent `busy` state.

Related policy, lifecycle, history, candidate, configuration, and audit mutations use a write-ahead control-plane transaction. A prepared transaction is replayable, audit events carry an idempotent transaction ID, and a revision marker is committed only after every target write succeeds. Route reads fail closed while a prepared transaction remains; the next locked configure, cycle, approval, rollback, or status operation completes recovery. Corrupted journals are preserved for inspection rather than discarded.

## Enable, inspect, and disable

```powershell
python "$HOME/.codex/skills/agent-auto-router/scripts/guarded_auto.py" configure --mode guarded-auto
python "$HOME/.codex/skills/agent-auto-router/scripts/guarded_auto.py" status
python "$HOME/.codex/skills/agent-auto-router/scripts/guarded_auto.py" cycle --dry-run
python "$HOME/.codex/skills/agent-auto-router/scripts/guarded_auto.py" configure --mode manual
```

Use `--state-dir` and `--feedback-file` to isolate tests. CLI entrypoints automatically run one zero-model-call cycle after recording an outcome. Desktop and other hosts submit the plan's route metadata plus actual result:

```powershell
$report | ConvertTo-Json -Depth 12 |
  python "$HOME/.codex/skills/agent-auto-router/scripts/guarded_auto.py" report --stdin
```

The report schema is `agent-auto-router.execution-report.v1`. It requires a unique `reportId`, a trusted short `host`, the exact route metadata emitted by the planner, and result status, duration, verification, validation configuration, escalation flag, and attempt count. Report IDs are idempotent. An incomplete prior recording fails closed for operator review.
