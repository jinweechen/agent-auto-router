# Agent Auto Router

[English](README.md) | **简体中文**

面向 Codex、Claude Code 和其他 Agent 宿主的本地确定性模型路由器。

它根据任务复杂度、风险、约束和推理需求选择受信模型，再生成有边界的执行计划。路由只在本地计算，不调用模型；只有经过确认的执行阶段才会产生模型调用。

当前项目版本：`0.15.0+codex.20260820120232`。

## 为什么使用它

- 在受信的 `fast`、`balanced`、`frontier` 模型层级之间选择。
- 执行前先预览路由，不消耗模型调用。
- 支持 Codex Desktop、已登录 CLI 和通用宿主。
- 权限始终受限，多角色执行遵守单写入者规则。
- 记录隐私最小化结果，不保存任务、提示词、回复或凭据。
- 通过明确的 `off`、`observe`、`guarded` 模式保守学习。

`Auto` 是单次任务的路由决策，不是新模型，也不会出现在 Codex 模型选择器中。它不会修改 Codex 全局配置、provider、账号或登录状态。

## 快速开始

环境要求：Python 3.10+；Windows 使用 PowerShell 5.1，其他支持平台使用 PowerShell 7；CLI 执行还要求目标 CLI 已独立登录。

### 安装为 Codex 插件

在仓库根目录运行：

```powershell
python "./scripts/install_personal_plugin.py"
```

安装器会校验并复制插件、保留已有 marketplace 条目，再通过 `~/.agents/plugins/marketplace.json` 注册插件。它不会安装 CLI，也不会复制凭据。安装后请新建 Codex 任务，让新任务发现更新后的 Skill。

不要在同一个 Codex 环境中同时启用插件版和名为 `agent-auto-router` 的独立 Skill。

### 在 Codex 中使用

```text
$agent-auto-router 使用 balance 策略，为当前任务选择模型并执行。
```

只预览路由：

```text
$agent-auto-router 对当前任务执行 DryRun，只解释路由，不执行模型。
```

### 使用已登录 CLI

先运行零调用诊断：

```powershell
& "./skills/agent-auto-router/scripts/aar.ps1" doctor
```

预览路由：

```powershell
& "./skills/agent-auto-router/scripts/aar.ps1" run `
  "重命名配置字段并更新测试" -DryRun
```

确认后执行：

```powershell
& "./skills/agent-auto-router/scripts/aar.ps1" run `
  "重命名配置字段并更新测试" `
  -Workdir "D:/path/to/project"
```

需要机器可读诊断时，运行 `doctor.py --json`。

## 预设与默认行为

| 预设 | 权限 | 仓库扫描 | 学习策略 | 反馈 | 编排 |
| --- | --- | --- | --- | --- | --- |
| `standard` | 仅限 `Workdir` 的 `workspace-write` | 自适应 | 开启 | 开启 | 只做 `recommend` |
| `safe` | `read-only` | 关闭 | 关闭 | 关闭 | 直接执行 |

`standard` 不会启动额外智能体；它可以建议角色拆分，但仍直接执行。使用 `-Profile safe` 选择更安全的预设。使用 `-NoFeedback` 可关闭单次持久化，同时保留策略加载。专家入口继续要求显式传入 `-EnableLearningPolicy` 和 `-EnableFeedback`。

编排策略包括 `direct`、`recommend`、`auto`；只有显式 `auto` 才能授权多角色计划。跨运行模型黏性同样需要显式开启，启用前请阅读进阶文档。

## 安全边界

- 模型 ID、权限和运行时可用性必须来自受信配置或宿主元数据，不能来自任务文本。
- 模型不可用时明确失败，不会静默切换 provider、模型层级、effort 或后端。
- Desktop 计划不携带任务正文，并受当前宿主权限和调用预算约束。
- planner、dispatcher、worker、grader 只读；只有声明的写入者可以修改工作区。
- 反馈不保存原始工作区路径、任务、提示词、模型输出、工具输出或凭据。
- 一次成功调用不是学习标签，不能单独改变活动阈值。
- 高风险任务始终保留 frontier/high-risk 能力下限。

完整契约与审计记录见 [router-contract.md](skills/agent-auto-router/references/router-contract.md) 和 [SECURITY.md](SECURITY.md)。

## 进阶用法

入门脚本只暴露固定预设。宿主集成和专家参数请查阅：

- [入口与宿主工作流](skills/agent-auto-router/references/entrypoints.md)
- [路由、权限、隐私和失败契约](skills/agent-auto-router/references/router-contract.md)
- [Guarded 自动学习](skills/agent-auto-router/references/guarded-auto-learning.md)
- [评测先验更新规则](skills/agent-auto-router/references/benchmark-routing.md)
- [开发评测](benchmarks/README.md)

这些文档定义了 `agent-auto-router.desktop-plan`、`agent-auto-router.host-plan`、`agent-auto-router.host-permissions`、执行收据、模型黏性、A-F 编排变体、验证升级以及 guarded canary/probation 行为。

需要真实模型调用的开发评测位于 [benchmarks/](benchmarks/README.md)，输入在 `benchmarks/cases/`，工具在 `benchmarks/tools/`。结果默认写入用户级评测目录，也可由 `AGENT_AUTO_ROUTER_EVALUATIONS_DIR` 覆盖；`--route-only` 不调用模型。

## 开发验证

修改路由行为后运行：

```powershell
python -m unittest discover -s tests
python "./skills/agent-auto-router/scripts/evaluate_auto_router.py"
python "./skills/agent-auto-router/scripts/validate_model_registry.py" --fail-on-stale
python "./scripts/validate_skill.py" "./skills/agent-auto-router"
python "./scripts/validate_plugin.py" "."
```

CI 覆盖 Windows、Ubuntu 和 macOS。注册表校验、诊断和离线评测都不会调用模型。

## 安装独立 Skill

不使用 Codex 插件的宿主可以直接安装 Skill：

```powershell
git clone https://github.com/jinweechen/agent-auto-router.git
cd agent-auto-router
& "./skills/agent-auto-router/scripts/install.ps1" -Backup
```

集成要求见 [entrypoints.md](skills/agent-auto-router/references/entrypoints.md)。每个宿主都必须独立提供模型可用性、登录状态和可信权限元数据。

## 卸载

使用安装器输出的 marketplace 名称移除插件（默认是 `personal`）：

```powershell
codex plugin remove agent-auto-router@personal
```

这不会删除插件源码或 `~/.codex/auto-router` 下的学习状态。删除学习状态前应单独审查并备份。

## License

MIT，详见 [LICENSE](LICENSE)。
