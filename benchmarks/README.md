# Development benchmarks

These tools are development-only and are intentionally excluded from the installed
Skill package. The deterministic router and offline evaluator do not require them.

`codex_cli_orchestration_eval.py` launches real signed-in Codex CLI model calls unless
`--route-only` is supplied. Run it only in an isolated workspace, provide an explicit
results directory outside the target workspace, and obtain approval for the expected
model-call and token budget first.

Example zero-model-call route check:

```powershell
python ./benchmarks/codex_cli_orchestration_eval.py `
  --route-only `
  --routing-mode balance `
  --results-dir "$env:TEMP/agent-auto-router-routes"
```
