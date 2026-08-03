# Entrypoints

## Signed-in CLI workflow

Use `scripts/invoke_auto_task.ps1`. It classifies locally with zero routing-model calls and launches the selected model through `codex exec`.

```powershell
& "$HOME/.codex/skills/codex-auto-router/scripts/invoke_auto_task.ps1" `
  -Task "Implement the requested change" `
  -Strategy balance `
  -Workdir "C:/path/to/repo" `
  -Explain
```

Use `-DryRun` for a route explanation without a model call and `-Json` for JSONL execution output. Task text travels over UTF-8 stdin.

The script uses the existing Codex authentication and provider. It does not edit `config.toml`, install a provider, start a proxy, or change CC Switch state.

## Offline calibration

```powershell
python "$HOME/.codex/skills/codex-auto-router/scripts/evaluate_auto_router.py" `
  --output "./auto-router-eval.json"
```

The evaluator makes zero model calls.

## Desktop boundary

This workflow does not add `Auto` to the Desktop picker or switch the current conversation model. It starts a separate `codex exec` task with the selected real model, avoiding global provider and history conflicts.
