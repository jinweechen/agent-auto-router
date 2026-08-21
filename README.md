# Agent Auto Router

**English** | [简体中文](README.zh-CN.md)

A local, deterministic model router for Codex, Claude Code, and other agent hosts.

It selects a trusted model from task complexity, risk, constraints, and reasoning needs, then produces a bounded execution plan. Routing is local and makes zero model calls; only the approved execution step can call a model.

Current project version: `0.15.0+codex.20260820120232`.

## Why use it?

- Choose between trusted `fast`, `balanced`, and `frontier` model tiers.
- Preview the route before spending a model call.
- Run through Codex Desktop, a signed-in CLI, or a generic host.
- Keep permissions bounded and multi-role work under a single-writer rule.
- Record privacy-minimized outcomes without tasks, prompts, responses, or credentials.
- Learn conservatively through explicit `off`, `observe`, and `guarded` modes.

`Auto` is a per-task decision, not a new model and not a Codex model-picker option. It never changes global Codex configuration, providers, accounts, or login state.

## Quick start

Requirements: Python 3.10+, PowerShell 5.1 on Windows or PowerShell 7 on any supported platform, and an independently signed-in target CLI when using CLI execution.

### Install as a Codex plugin

From the repository root:

```powershell
python "./scripts/install_personal_plugin.py"
```

The installer validates and copies the plugin, preserves existing marketplace entries, and registers it from `~/.agents/plugins/marketplace.json`. It does not install a CLI or copy credentials. After installation, start a new Codex task so the refreshed Skill is discovered.

Do not enable both the plugin edition and a standalone Skill named `agent-auto-router` in the same Codex environment.

### Use it in Codex

```text
$agent-auto-router Use the balance strategy to select a model and execute this task.
```

Preview only:

```text
$agent-auto-router DryRun this task and explain the route without executing a model.
```

### Use the signed-in CLI

Run a zero-call diagnostic:

```powershell
& "./skills/agent-auto-router/scripts/aar.ps1" doctor
```

Preview a route:

```powershell
& "./skills/agent-auto-router/scripts/aar.ps1" run `
  "Rename the configuration field and update its tests" -DryRun
```

Execute it:

```powershell
& "./skills/agent-auto-router/scripts/aar.ps1" run `
  "Rename the configuration field and update its tests" `
  -Workdir "D:/path/to/project"
```

For machine-readable diagnostics, run `doctor.py --json`.

## Profiles and defaults

| Profile | Permissions | Repository scan | Learning policy | Feedback | Orchestration |
| --- | --- | --- | --- | --- | --- |
| `standard` | `workspace-write` limited to `Workdir` | Adaptive | On | On | `recommend` only |
| `safe` | `read-only` | Off | Off | Off | Direct |

`standard` does not start extra agents. It may recommend a role split while still executing directly. Select the safer preset with `-Profile safe`. Use `-NoFeedback` to disable persistence for one run without disabling policy loading. The expert entrypoints keep `-EnableLearningPolicy` and `-EnableFeedback` explicit.

The orchestration policies are `direct`, `recommend`, and `auto`; only explicit `auto` can authorize a multi-role plan. Cross-run affinity is also opt-in. See the advanced documentation before enabling either feature.

## Safety boundaries

- Model IDs, permissions, and runtime availability must come from trusted configuration or host metadata, never task text.
- Unavailable models fail explicitly; the router does not silently switch provider, tier, effort, or backend.
- Desktop plans omit task text and stay within the host's current permissions and call budget.
- Planner, dispatcher, worker, and grader roles are read-only; only the declared writer can modify the workspace.
- Feedback excludes raw workspace paths, task text, prompts, model output, tool output, and credentials.
- One successful call is not a learning label and cannot change active thresholds by itself.
- High-risk tasks always retain the frontier/high-risk capability floor.

For the complete contract and audit history, read [router-contract.md](skills/agent-auto-router/references/router-contract.md) and [SECURITY.md](SECURITY.md).

## Advanced usage

The beginner wrapper intentionally exposes only fixed profiles. Use the detailed references for host integration and expert controls:

- [Entrypoints and host workflows](skills/agent-auto-router/references/entrypoints.md)
- [Routing, permission, privacy, and failure contract](skills/agent-auto-router/references/router-contract.md)
- [Guarded automatic learning](skills/agent-auto-router/references/guarded-auto-learning.md)
- [Benchmark-prior update rules](skills/agent-auto-router/references/benchmark-routing.md)
- [Development benchmarks](benchmarks/README.md)

These references define `agent-auto-router.desktop-plan`, `agent-auto-router.host-plan`, `agent-auto-router.host-permissions`, execution receipts, model affinity, A-F orchestration variants, validation escalation, and guarded canary/probation behavior.

Development benchmarks that make real model calls live under [benchmarks/](benchmarks/README.md), with inputs in `benchmarks/cases/` and tools in `benchmarks/tools/`. Results default to the user-state evaluation directory and can be redirected with `AGENT_AUTO_ROUTER_EVALUATIONS_DIR`; `--route-only` makes no model call.

## Development

Run the offline checks after changing routing behavior:

```powershell
python -m unittest discover -s tests
python "./skills/agent-auto-router/scripts/evaluate_auto_router.py"
python "./skills/agent-auto-router/scripts/validate_model_registry.py" --fail-on-stale
python "./scripts/validate_skill.py" "./skills/agent-auto-router"
python "./scripts/validate_plugin.py" "."
```

CI covers Windows, Ubuntu, and macOS. Registry validation, diagnostics, and the offline evaluator make zero model calls.

## Standalone Skill installation

Hosts that do not use Codex plugins can install the Skill directly:

```powershell
git clone https://github.com/jinweechen/agent-auto-router.git
cd agent-auto-router
& "./skills/agent-auto-router/scripts/install.ps1" -Backup
```

See [entrypoints.md](skills/agent-auto-router/references/entrypoints.md) for integration requirements. Every host must independently provide model availability, authentication, and trusted permission metadata.

## Uninstall

Remove the plugin registration using the marketplace name printed by the installer (`personal` is the default):

```powershell
codex plugin remove agent-auto-router@personal
```

This does not delete the source package or learning state under `~/.codex/auto-router`. Review and back up that state separately before removing it.

## License

MIT. See [LICENSE](LICENSE).
