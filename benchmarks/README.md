# Development benchmarks

Development evaluation follows one directory contract:

```text
benchmarks/                         # Version-controlled evaluation assets
├── README.md
├── cases/                          # Reproducible task inputs only
├── research/                       # Versioned design and benchmark research notes
└── tools/                          # Evaluation and simulation programs

<user-state>/agent-auto-router/evaluations/
├── route-only/<run-id>/            # Zero-model-call routing reports
├── codex-cli/<run-id>/             # Real signed-in CLI evaluations
└── legacy/<import-id>/             # Preserved historical artifacts
```

Do not commit generated reports, logs, checkpoints, model output, temporary workspaces,
or environment probes. The optional repository-local `.artifacts/` directory is ignored
and exists only for explicit local overrides or imported archives.

The default generated-results root is:

- `%LOCALAPPDATA%/agent-auto-router/evaluations` on Windows;
- `$XDG_STATE_HOME/agent-auto-router/evaluations` when `XDG_STATE_HOME` is set;
- otherwise `~/.local/state/agent-auto-router/evaluations`.

Set `AGENT_AUTO_ROUTER_EVALUATIONS_DIR` to select another protected root. Every run gets
its own `<kind>/<UTC timestamp>-<nonce>/` directory with `manifest.json` plus stable
result names such as `route-report.json`, `checkpoint.partial.json`, and `results.json`.
An explicit `--results-dir` must point to an empty run directory.

These tools are intentionally excluded from the installed Skill package. The
deterministic router and offline evaluator do not require them. Versioned runtime priors
remain under `skills/agent-auto-router/scripts/benchmark_priors.json`; guarded learning,
feedback, audit, and transaction state remain under the protected router state directory
and must never be placed in benchmark output.

`tools/codex_cli_orchestration_eval.py` launches real signed-in Codex CLI model calls
unless `--route-only` is supplied. Run it only in an isolated workspace and obtain
approval for the expected model-call and token budget first. Keep the generated-results
root outside the target worktree.

Example zero-model-call route check:

```powershell
python ./benchmarks/tools/codex_cli_orchestration_eval.py `
  --route-only `
  --routing-mode balance
```

Example with an explicit empty run directory:

```powershell
python ./benchmarks/tools/codex_cli_orchestration_eval.py `
  --route-only `
  --routing-mode balance `
  --results-dir "$env:TEMP/agent-auto-router-routes/manual-check"
```
