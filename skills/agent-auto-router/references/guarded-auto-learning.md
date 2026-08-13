# Learning modes and guarded optimization

The learning configuration accepts only `off`, `observe`, and `guarded`. `observe` is the default: it records privacy-minimized outcomes but never changes routing thresholds. `off` persists nothing. `guarded` enables the bounded local threshold-control loop described here. Old configuration schemas and the old `manual` / `guarded-auto` names are rejected rather than migrated silently. None of these modes makes a routing-model call or authorizes registry, risk, permission, prompt, Skill, provider, or account changes.

## Evidence boundary

An automatic quality signal must be one of:

- an explicit user preferred-model selection recorded with `preferenceSource=explicit-user-selection`; or
- a deterministic validation-proven escalation where the initial tier failed, exactly the adjacent stronger tier passed, at least two attempts were recorded, and the route was neither high-risk nor explicitly overridden.

Exit code zero, ordinary success, latency, and token count are observations, not quality labels. Task text, model output, child-agent output, tool output, credentials, and hidden instructions are rejected from the report schema and never stored.

Model affinity is a separate routing heuristic, not policy learning. It may read recent successful route outcomes, hashed workspace identity, and cached-input/cache-write counters to reduce avoidable model switches, but those counters never label model quality or authorize a threshold mutation. Affinity is bounded to 30 minutes, one adjacent stronger tier, the same backend and strategy, and a capability floor that forbids downgrades.

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

After a candidate is rejected, stabilized, or rolled back, the state records the newest strong-evidence timestamp and will not recompute the same evidence. A new evaluation requires another bounded batch of newer strong signals.

Only reports with deterministic validation results, no explicit override, and no high-risk flag count toward canary or probation acceptance. Learning failures do not turn a completed user task into a failed task, but corrupted canary state blocks candidate routing until it is inspected or guarded mode is disabled.

Feature extraction is explicitly versioned. New outcomes and candidates carry `featureSchemaVersion=3`; a record with no version remains readable as legacy v1 audit evidence, and v2 records remain audit-readable, but neither can enter labeled or inferred samples, canary, or probation statistics. Any candidate built for another feature schema is stale and cannot be routed, approved, or promoted.

The state directory and any custom feedback file are protected control-plane inputs whenever guarded learning or cross-run `-ModelAffinity auto` reads them. Execution checks them before launching a child: they must be outside every child-writable root. Protected-input execution is blocked under `danger-full-access`, an unknown external sandbox, or any other boundary that lets the child edit its own routing or learning evidence. Default `session` affinity reads no protected state; `off` also disables within-run selected-model preference.

Guarded lifecycle transitions, manual approval/rollback, and configuration changes share one bounded operating-system file lock. Feedback and audit JSONL streams use their own bounded append locks. The operating system releases an active lock when a process exits, so an abandoned `.guarded-auto.lock` file is inert rather than a permanent `busy` state.

Feedback is a bounded evidence store, not an indefinite raw log. In `observe` and `guarded`, a learning cycle atomically retains the latest route outcome and human label for at most 5,000 routes whose latest event is no older than 90 days. Outcomes may include only a SHA-256 workspace identity, topology/variant/role-policy fields, tier-switch count, and numeric cache telemetry for affinity; they never include the raw path. The default `feedback` command only previews event, route, byte, and age changes; `--apply` is required for a custom retention window. Compaction holds the feedback append lock, uses atomic replacement, preserves outcome/label pairs, stores no task text, and makes zero model calls. Guarded re-evaluation tracks the latest strong-evidence timestamp rather than a cumulative count, so a rolling capped store can continue learning from new evidence.

Execution-report idempotency markers are separately locked and bounded. Completed markers are retained for 90 days and at most 5,000 report IDs by default; pending and incomplete markers are never automatically pruned and set an operator-review flag. Corrupt markers fail closed without deletion. The `reports` command previews retention unless `--apply` is supplied. Exact duplicate rejection therefore applies only while a completed marker remains inside this documented window.

Use `recover-report` without an action to inspect one pending or incomplete marker and count matching outcome/label events without exposing their content. `release-for-retry` requires no phase progress and no matching evidence; it requires an exact report-ID confirmation plus a safe resolver identity and archives the marker before releasing the ID. `acknowledge-recorded` requires exactly one matching route outcome and the expected label evidence, preserves feedback byte-for-byte, and records whether a later explicit `cycle` is required. Recovery never activates or otherwise mutates a policy.

The `shadow` command performs a read-only A/B comparison for the current canary/probation candidate or an explicit candidate file. It verifies candidate integrity, feature schema, registry, benchmark priors, and active-policy lineage, then scores the embedded baseline and candidate on the same retained strong evidence and deterministic holdout. It returns aggregate deltas, Wilson intervals, a two-sided exact paired sign test, minimum-effect/confidence gates, and strategy/risk/label-source strata containing at least three samples. Smaller strata are counted as suppressed rather than emitted. It omits route IDs and task text, makes zero model calls, sets `activationAuthorized=false`, and never changes lifecycle state.

Related policy, lifecycle, history, candidate, configuration, and audit mutations use a write-ahead control-plane transaction. A prepared transaction is replayable, audit events carry an idempotent transaction ID, and a revision marker is committed only after every target write succeeds. Route reads fail closed while a prepared transaction remains; the next locked configure, cycle, approval, rollback, or status operation completes recovery. Corrupted journals are preserved for inspection rather than discarded.

## Enable, inspect, and disable

```powershell
python "$HOME/.codex/skills/agent-auto-router/scripts/guarded_auto.py" configure --mode guarded
python "$HOME/.codex/skills/agent-auto-router/scripts/guarded_auto.py" status
python "$HOME/.codex/skills/agent-auto-router/scripts/guarded_auto.py" cycle --dry-run
python "$HOME/.codex/skills/agent-auto-router/scripts/guarded_auto.py" feedback
python "$HOME/.codex/skills/agent-auto-router/scripts/guarded_auto.py" feedback --maximum-routes 2000 --retention-days 30 --apply
python "$HOME/.codex/skills/agent-auto-router/scripts/guarded_auto.py" reports
python "$HOME/.codex/skills/agent-auto-router/scripts/guarded_auto.py" reports --maximum-markers 2000 --retention-days 30 --apply
python "$HOME/.codex/skills/agent-auto-router/scripts/guarded_auto.py" recover-report --report-id REPORT_ID
python "$HOME/.codex/skills/agent-auto-router/scripts/guarded_auto.py" recover-report --report-id REPORT_ID --action release-for-retry --confirm-report-id REPORT_ID --resolved-by OPERATOR
python "$HOME/.codex/skills/agent-auto-router/scripts/guarded_auto.py" recover-report --report-id REPORT_ID --action acknowledge-recorded --confirm-report-id REPORT_ID --resolved-by OPERATOR
python "$HOME/.codex/skills/agent-auto-router/scripts/guarded_auto.py" shadow
python "$HOME/.codex/skills/agent-auto-router/scripts/guarded_auto.py" configure --mode observe
python "$HOME/.codex/skills/agent-auto-router/scripts/guarded_auto.py" configure --mode off
```

The guarded defaults evaluate after 12 strong signals, expose 20% of deterministic route buckets to canary, require six verified canary and six baseline reports, then require twelve probation reports. Use `--state-dir` and `--feedback-file` to isolate tests. CLI entrypoints automatically run one zero-model-call cycle after recording an outcome in `observe` or `guarded`; `off` returns `feedback-disabled` without creating a feedback or report file. Desktop and other hosts submit the plan's route metadata plus actual result:

```powershell
$report | ConvertTo-Json -Depth 12 |
  python "$HOME/.codex/skills/agent-auto-router/scripts/guarded_auto.py" report --stdin
```

The report schema is `agent-auto-router.execution-report`. It requires a unique `reportId`, a trusted short `host`, the exact route metadata emitted by the planner, and result status, duration, verification, validation configuration, escalation flag, and attempt count. Report IDs are exactly idempotent while their completed markers remain inside the bounded retention window. Markers record route, label, and learning-cycle phase completion. An incomplete prior recording fails closed until inspection and an explicitly confirmed safe recovery action.
