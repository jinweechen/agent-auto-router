# Agent Auto Router

[English](README.md) | **简体中文**

面向 Codex、Claude Code 和通用 Agent 宿主的本地确定性模型路由插件与 Skill。

它在执行前根据任务复杂度、风险、约束程度和 reasoning effort 选择受信模型，并生成有界执行计划。标准路由会安全启用自适应仓库检查、编排建议和本次计划内模型复用，但不读取历史状态，也不启动额外智能体。跨任务黏性、学习策略、反馈写入和多角色执行仍需显式开启。路由过程本身不调用模型，不修改 Codex 全局配置，也不读取或转发登录凭据。

当前项目版本：`0.15.0+codex.20260813062845`。

## 先看结论

- `Auto` 是一次任务级路由决策，不是新的模型，也不会出现在 Codex 模型下拉框中。
- 默认 `balance` 策略在 `fast / balanced / frontier` 三个能力层之间选择模型。
- Codex Desktop 通过原生子代理协议执行；CLI 模式使用用户已经登录的官方 CLI。
- Desktop 计划不包含任务正文，所有执行都受当前宿主权限、调用预算和单写入者规则约束。
- 标准路由是零状态的：普通回答不扫描仓库；代码、路径、依赖或调试任务会执行一次有界自适应扫描。它不加载学习策略、不读黏性反馈、不持久化结果，也不自动编排。
- 新模型可以先显式试用，验证后再进入 Auto；模型不可用时明确失败，不静默回退。

## 能做什么

| 能力 | 说明 |
| --- | --- |
| 自动选模 | 从版本化受信注册表选择模型、effort、能力层和上下文预算 |
| 统一路由协议 | Python 与 PowerShell 入口统一消费带任务及工作区绑定的严格 `agent-auto-router.route-decision` |
| Desktop 执行计划 | 输出 `agent-auto-router.desktop-plan`，由当前主代理执行有界 DAG |
| CLI 执行 | 通过 UTF-8 stdin 调用已登录的 Codex CLI 或编排后端 |
| 多角色编排 | 支持 planner、dispatcher、worker、reviewer、grader，并保证只有一个写入者 |
| 通用宿主协议 | 输出带 stdin 执行信封模板的 `agent-auto-router.host-plan` |
| 模型注册表 | 分离 `enabled` 与 `autoEligible`，支持受控扩展和显式试用 |
| 隐私最小化反馈 | 只保存路由元数据、验证状态、耗时和可观测 Token，不保存任务与回复 |
| 受控学习 | 使用明确的 `off / observe / guarded` 模式，并提供 canary、probation 和自动回滚 |
| Codex 插件 | 通过标准插件清单分发现有 Skill，不改变跨宿主核心 |

## 不会做什么

- 不修改 `~/.codex/config.toml`、provider、账号或 CC Switch 状态。
- 不读取、复制、代理或转发 Desktop/CLI 凭据。
- 不从任务文本、模型输出或普通环境变量注入模型 ID 和权限。
- 不在模型不可用时静默切换 provider、模型、tier、effort 或后端。
- 不允许 planner、dispatcher、worker 或 grader 修改共享工作区。
- 不把任务正文、模型输出、工具输出或凭据写入反馈日志。
- 不依据一次成功调用自动改变活动阈值。
- 不通过学习降低高风险任务的 `frontier + high-risk-primary` 边界。

完整运行时契约见 [router-contract.md](skills/agent-auto-router/references/router-contract.md)；逐项问题判断、修复证据和剩余限制见 [SECURITY.md](SECURITY.md)。

## 工作流程

```text
任务
  -> 本地确定性特征提取
  -> 使用内置策略与离线评测先验；可选加载活动策略
  -> 选择 tier、model、effort、context 和 A-F 变体
  -> 校验宿主权限、模型可用性、调用预算和工作区边界
  -> Desktop：输出无任务正文的 staged plan
     CLI：调用对应已登录 CLI
  -> 在 observe/guarded 模式记录隐私最小化结果
  -> 可选：人工标注或 guarded 学习
```

路由与执行是两个阶段：路由只做本地计算；只有执行阶段才可能产生模型调用。

编排模式为 `direct`、`recommend`、`auto`。标准路由使用 `recommend`，可以说明有价值的角色拆分，但仍只输出单次调用的直接计划；只有显式 `auto` 才可能规划多智能体执行。

## 快速开始

### 环境要求

- Python 3.10 或更高版本
- PowerShell 包装脚本：Windows 可用 Windows PowerShell 5.1；Windows、Linux、macOS 可用 PowerShell 7
- Desktop 模式：当前 Codex Desktop runtime 提供子代理能力
- CLI 模式：目标 CLI 已安装并独立登录

Python 路由器仅使用标准库，没有第三方运行时依赖。

### 本地三步路径

大多数已登录 CLI 的用户只需要使用精简入口 `aar.ps1`。首先运行一屏式零调用诊断：

```powershell
& "./skills/agent-auto-router/scripts/aar.ps1" doctor
```

先做不调用模型的路由预览：

```powershell
& "./skills/agent-auto-router/scripts/aar.ps1" run `
  "重命名配置字段并更新测试" -DryRun
```

确认路由后再执行：

```powershell
& "./skills/agent-auto-router/scripts/aar.ps1" run `
  "重命名配置字段并更新测试" `
  -Workdir "D:/path/to/project"
```

默认 `standard` 预设只允许写入 `Workdir`：仅在代码、路径或调试任务需要时自适应检查仓库，只报告编排建议而不执行，并只在本次运行内复用所选模型。`-Profile safe` 是只读模式，同时关闭仓库检查和模型黏性。两者都不加载学习策略、不写反馈、不启动多智能体执行。这些预设是经过校验的固定安全组合，不是可任意改写权限的捷径。

诊断默认只输出简洁摘要。通过 `aar.ps1 -Json`，或运行 `doctor.py --json`，可获得机器可读详情；它不读取任务、不打印环境变量值、不检查凭据，也不会调用模型。`--verbose-paths` 是额外的显式排障选项。

### Codex 插件安装

仓库根目录是一个标准 Codex 插件根，清单位于 [.codex-plugin/plugin.json](.codex-plugin/plugin.json)。首次安装到当前用户的 Codex，直接在仓库根目录运行：

```powershell
python "./scripts/install_personal_plugin.py"
```

该脚本先校验源码插件，在临时目录组装最小发行包，再安装到 `~/plugins/agent-auto-router`；随后创建或保留 `~/.agents/plugins/marketplace.json` 中的个人 marketplace，最后执行 `codex plugin add agent-auto-router@<marketplace-name>`。已有插件包发生变化时会备份到 `~/.codex/plugin-backups/`，已有 marketplace 的名称、显示名、顺序和其他插件条目都会保留。相同版本重复执行是幂等的。在 Windows Codex Desktop 环境中，如果本机存在 `CodexSandboxUsers` 组，安装器还会为插件包递归授予只读和执行权限，避免管理员安装后 Codex 沙箱无法读取插件。

脚本不会安装 Codex CLI、Claude CLI、Python、PowerShell，也不会复制任何登录凭据。若 Codex CLI 不在 `PATH`，已准备好的本地插件包和 marketplace 会保留，按错误消息中的命令重试即可。测试或预配置环境可使用 `--home <临时目录> --skip-codex-install`，它不会调用 Codex CLI。

安装成功后新建一个 Codex 任务，再调用 `$agent-auto-router`，让新任务加载插件中的 Skill。

不要在同一个 Codex 环境中同时启用插件版和下面的独立 Skill 版，以免出现两个同名 `$agent-auto-router`。如果 `~/.codex/skills/agent-auto-router` 仍存在，插件安装器会在任何写入前停止。从独立 Skill 迁移时，先备份并只删除这个已安装副本，再重新运行安装器；不要删除 `~/.codex/auto-router` 学习状态。

### 独立 Skill 与其他宿主

Codex 传统安装、Claude Code、Hermes 和其他宿主仍可以 Clone 仓库并使用原有脚本：

```powershell
git clone https://github.com/jinweechen/agent-auto-router.git
cd agent-auto-router
& "./skills/agent-auto-router/scripts/install.ps1" -Backup
```

对 Codex 传统安装，目标是 `$CODEX_HOME/skills/agent-auto-router`；未设置 `CODEX_HOME` 时使用 `~/.codex/skills/agent-auto-router`。`-Backup` 会把旧版本保存到 `~/.codex/skill-backups/agent-auto-router`。

安装脚本通过暂存目录替换旧副本，可以重复执行，不会产生嵌套的 `agent-auto-router/agent-auto-router`。其他宿主不需要识别 `.codex-plugin/plugin.json`：普通已登录 CLI 用户使用 `aar.ps1`，宿主集成再调用 `host_execution_plan.py`、`invoke_auto_task.ps1` 或 `invoke_orchestrated_task.ps1`；专家入口见 [entrypoints.md](skills/agent-auto-router/references/entrypoints.md)。各宿主必须独立提供模型可用性、登录状态和可信权限边界。

### 在 Codex 中使用

```text
$agent-auto-router 使用 balance 策略，为当前任务自动选择模型并执行。
```

指定工作目录：

```text
$agent-auto-router 在 D:\path\to\project 中完成当前修改，使用 balance 策略。
```

只查看路由，不执行模型：

```text
$agent-auto-router 对“重命名配置字段并更新文档”执行 DryRun，只返回计划和原因。
```

这是普通用户的推荐入口。Codex 主代理会从当前 turn 的可信运行时元数据取得可用模型、子代理容量和权限快照，不需要用户手工构造这些值。

## 路由规则

### 能力层

默认 Codex 映射来自 [model_registry.json](skills/agent-auto-router/scripts/model_registry.json)：

| 能力层 | 默认模型 | 典型任务 |
| --- | --- | --- |
| `frontier` | `codex:gpt-5.6-sol` | 高风险、架构、复杂重构、深度调试和开放性任务 |
| `balanced` | `codex:gpt-5.6-terra` | 常规开发、一般调试和均衡型任务 |
| `fast` | `codex:gpt-5.6-luna` | 提取、转换、格式化和边界清晰的任务 |

模型 ID 使用 `{backend}:{model}`。注册表还包含：

- `claude:sonnet`：`balanced`，允许 Auto。
- `claude:haiku`：`fast`，允许 Auto。
- `claude:opus`：`frontier`，默认仅允许显式试用。

注册表是信任与能力声明，不代表当前账号一定能访问对应模型。实际可用性仍由当前 Desktop runtime 或 CLI provider 验证。`reviewedAt` 记录最近一次人工复核日期，CI 会拒绝超过配置新鲜度窗口的注册表。

### 策略

| 策略 | 选择倾向 |
| --- | --- |
| `intelligence` | 质量优先；复杂任务使用 `frontier`，其余主要使用 `balanced` |
| `balance` | 推荐默认；简单、常规、复杂任务分别倾向 `fast`、`balanced`、`frontier` |
| `cost` | 使用模型层级作为成本代理；复杂任务仍保持能力下限，高风险始终使用 `frontier` |

`cost` 不等于实际账单优化。CLI Token 是可观测运行数据，不是价格或最终账单。

### 特征与先验

路由使用确定性特征，包括：

- 复杂度、风险动作和敏感领域
- 是否为边界清晰的简单操作
- 歧义、调试、长上下文、多文件和界面操作
- 验收条件数量和仓库规模
- 是否存在确定性验证命令

ASCII 关键词按词法边界匹配，避免 `tokenizer`/`token`、`information`/`format` 等子串误判；中文短语继续按子串匹配。`-Explain` 只报告命中的内置规则词，不回显完整任务文本，因此既能追踪决策，也不会复制敏感内容。

[benchmark_priors.json](skills/agent-auto-router/scripts/benchmark_priors.json) 是版本化离线快照，运行时不联网。它只提供能力层下限，不替代当前仓库的真实验收。更新证据前阅读 [benchmark-routing.md](skills/agent-auto-router/references/benchmark-routing.md)。

### A-F 执行变体

| 变体 | 拓扑 | 默认角色层级 |
| --- | --- | --- |
| A | direct | `frontier` direct |
| B | orchestrated | `frontier` planner → `fast` workers → `frontier` reviewer |
| C | orchestrated | B + `balanced` dispatcher |
| D | orchestrated | `balanced` planner → `fast` workers → `balanced` reviewer |
| E | direct | `balanced` direct |
| F | direct | `fast` direct |

A/E/F 是直接执行。标准入口和可复用 Python API 默认使用 `recommend`：评估并展示 B/C/D 建议，但仍直接执行且不增加模型调用；只有显式选择 `auto` 才启动多智能体。Python 模型黏性默认 `session`，未指定 effort 时使用所选层级的推荐值，而不是被视为显式 `medium`。`auto` 仍要求低风险任务具有明确并行信号、足够规模，并且扣除模型调用与层切换开销后收益为正。高风险任务还必须显式传入 `-ConfirmHighRiskOrchestration`。

默认 `-ModelAffinity session` 只在当前计划的兼容角色间复用所选模型，不读取历史状态，并使用 `selected-model-preferred` 角色策略。显式 `-ModelAffinity auto` 才会在同一工作区摘要、同一策略下复用最近 30 分钟内成功使用的模型，并继续受既有 tier 与缓存信号边界约束；`-ModelAffinity off` 使用精确角色预设。这些比例只用于路由，不是供应商账单估算。

角色默认值来自 [orchestration_profiles.json](skills/agent-auto-router/scripts/orchestration_profiles.json)。

## 执行方式

### Codex Desktop

Desktop 是宿主协议，不是隐藏的 CLI 登录流程。

主代理把当前 runtime 明确声明的模型与参数能力、并行子代理容量、`agent-auto-router.host-permissions` 权限快照，以及同源的 `agent-auto-router.desktop-spawn-capabilities` 快照交给路由器。路由器返回：

- 确切的角色模型和 effort
- staged DAG 与依赖顺序
- 最大调用数和最大并发数
- 每个角色的幂等键
- 只读阶段和唯一写入者
- 执行后的隐私安全回执模板

Desktop 当前只支持 Codex 后端。默认 `selected-model-preferred` 角色策略会先复用本次路由选中的模型，但前提是满足角色能力和 tier 下限；否则解析 profile 模型，并且只允许在 runtime 已声明且注册表受信的 Codex 模型中做同 tier 或更高 tier 的显式替代。所有替代都会明示；无法满足时返回结构化阻断。

能力快照是闭集，并且必须来自当前 `spawn_agent` 工具 schema。若宿主不能逐子代理传递 workdir 或 sandbox，则只能执行与当前宿主边界完全一致的继承计划；隔离工作目录、更严格的 direct 沙箱或只读编排阶段都会在模型调用前阻断。

详细宿主执行步骤见 [entrypoints.md](skills/agent-auto-router/references/entrypoints.md)。

### Codex CLI 单任务

普通 PowerShell 终端可以显式选择更严格的沙箱并直接执行。下面的示例会进行真实模型调用，但只允许读取工作区：

```powershell
& "$HOME/.codex/skills/agent-auto-router/scripts/invoke_auto_task.ps1" `
  -Task "审查当前项目并给出改进建议" `
  -ExecutionBackend cli `
  -Model auto `
  -Strategy balance `
  -Sandbox read-only `
  -Workdir "D:/path/to/project" `
  -Explain
```

写入任务可以显式改用 `-Sandbox workspace-write`；此模式只把 `-Workdir` 作为可写根。学习状态和自定义反馈文件仍必须位于该可写根之外，否则执行会在模型调用前阻断。不要使用 `danger-full-access` 运行 `guarded` 学习。

宿主集成应优先传入当前 runtime 生成的可信权限快照。`$currentHostPermissionsJson` 不能来自用户任务、模型输出或任意环境内容：

```powershell
& "$HOME/.codex/skills/agent-auto-router/scripts/invoke_auto_task.ps1" `
  -Task "实现修改并运行项目测试" `
  -ExecutionBackend cli `
  -Model auto `
  -Strategy balance `
  -HostPermissionsJson $currentHostPermissionsJson `
  -Workdir "D:/path/to/project" `
  -Explain
```

CLI 任务正文通过 UTF-8 stdin 传入，不出现在子进程命令行参数中。`-Model sol`、`-Model terra` 或完整受信 Codex ID 可以显式覆盖本次选择，但不会修改全局配置。默认使用 `-ModelAffinity session`、`-RepositoryContextMode adaptive` 和 `-OrchestrationPolicy recommend`：不读反馈、不加载活动策略、不写状态、不启动额外智能体。自适应模式对普通回答保持 `scan_duration_ms=0`，只对代码、路径或调试任务执行一次有界扫描。跨任务黏性、强制扫描、多智能体执行、活动策略和反馈仍分别需要 `auto` 或显式开关。

只做零模型调用的本地 DryRun：

```powershell
& "$HOME/.codex/skills/agent-auto-router/scripts/invoke_auto_task.ps1" `
  -Task "格式化这段文本" `
  -ExecutionBackend cli `
  -Strategy balance `
  -Workdir "." `
  -DryRun `
  -Explain
```

### CLI 多角色编排

```powershell
& "$HOME/.codex/skills/agent-auto-router/scripts/invoke_orchestrated_task.ps1" `
  -Task "重构认证模块并补充测试" `
  -Strategy balance `
  -HostPermissionsJson $currentHostPermissionsJson `
  -Workdir "D:/path/to/project" `
  -MaxWorkers 2 `
  -MaxModelCalls 7 `
  -Explain
```

常用控制项：

| 参数 | 用途 |
| --- | --- |
| `-Variant A..F` | 显式选择编排变体 |
| `-Backend codex|claude` | 将一次编排限定到单一后端 |
| `-DryRun` | 只输出路由，不调用模型 |
| `-Sandbox read-only` | 禁止最终角色写入 |
| `-RepositoryContextMode adaptive|auto|off` | 仅为代码/路径/调试任务扫描、强制有界单次扫描或关闭检查 |
| `-ModelAffinity session|auto|off` | 本次计划内复用、读取有界同工作区证据或使用精确角色预设 |
| `-AllowDirty` | 明确接受在非干净 Git 工作区执行的风险 |
| `-AllowNoChanges` | 允许写任务成功但 Git 状态无变化 |
| `-MaxTotalTokens` | 对 CLI 可观测 Token 设置软预算 |
| `-GraderPolicy auto|always|never` | 控制独立 grader |
| `-ContextMode lean|full` | 控制只读角色是否忽略个人 CLI 配置 |

默认要求正式编排在干净 Git 工作区运行。Git 状态检查最多五秒，结果区分为 `clean / dirty / non_git / unknown`；写模式遇到 `unknown` 会在构造适配器和调用模型前阻断。并行角色全部只读，只有 direct 或最终 reviewer 可以获得排他写入权。限制 `-Backend` 不会让显式试用模型取得 Auto 资格；只有用户显式选择具体模型时才能使用它。

`-ResultsDir` 必须位于所有子进程可写根目录之外，`danger-full-access` 下不可使用。报告采用带随机 UUID 的名称、排他创建和 POSIX 所有者权限，并默认移除任务、prompt、输出、rationale、错误、工具结果、工作区路径和 response ID 等字段。`-IncludeOutputInReport` 必须与 `-ResultsDir` 同时使用；Windows 会在写入内容前验证 DACL 仅允许当前用户、System 和 Administrators。该文件应按敏感数据保护和保留。

### 通用宿主

`host_execution_plan.py` 从包含当前任务及其绑定路由的宿主请求和可信权限快照生成 `agent-auto-router.host-plan`，但不启动任何进程，也不会在输出中返回任务正文。宿主根据 `action.kind` 决定：

- `cli`：调用声明的后端和模型。
- `host_execute`：由宿主原生模型近似执行，并明确标记准确度边界。
- `orchestrate`：用当前任务实例化声明的 `execution-envelope` stdin 模板，再调用本地多角色编排入口。路由和权限快照不再进入进程 argv；入口校验任务及工作区绑定，不再二次路由。

通用宿主不得把连接器、登录会话或凭据复制到独立 CLI。

## 权限与单写入者边界

自动执行使用 `agent-auto-router.host-permissions`。可信快照包含：

- sandbox 与 approval policy
- network access
- 绝对可写根目录
- 是否允许请求更高权限
- 可选的宿主 permission profile ID

有效子权限只能等于或弱于宿主权限。缺少可信快照、`workspace-write` 没有绝对可写根、工作目录不在允许根中，或子 CLI 无法安全复现组合边界时，执行会在模型启动前阻断。

Desktop 多角色计划遵守以下规则：

1. planner、dispatcher、worker、grader 只读。
2. 同一时间不允许多个写入者。
3. direct 或 reviewer 只有在依赖成功后才能获得写入 claim。
4. 只读阶段发生意外工作区变化时停止执行。
5. 不自动重试超时的 `xhigh` 或 `max` 角色。

## 反馈与阈值学习

### 保存什么

传入 `-EnableFeedback` 后，配置的 `observe` 或 `guarded` 模式才会把 CLI 的隐私最小化结果写入 `~/.codex/auto-router/feedback.jsonl`：

- route ID、策略、effort、能力层和模型
- SHA-256 工作区标识、拓扑、变体、角色模型策略和预计角色层切换次数
- 数值/布尔路由特征
- policy、registry 和 feature schema 摘要
- 退出码、耗时、验证状态和尝试次数
- CLI 实际暴露的聚合 input、cached input、cache-write、output 和 reasoning output Token，以及单独归属于最终选中模型、仅供黏性判断使用的 Token 切片

无法观测的 Token 保持 `null`。反馈不保存原始工作区路径、任务正文、模型回复、工具输出或凭据。显式启用反馈、活动学习策略或模型黏性时，状态目录和反馈文件必须位于所有子进程可写根之外。在 `observe` 和 `guarded` 下，每次学习循环都会原子保留最近 90 天、最多 5000 个路由的最后结果与人工标签。`status.feedbackStorage` 会显示事件数、字节数和待清理数量。可以先只读检查，再显式应用自定义窗口：

```powershell
python "$HOME/.codex/skills/agent-auto-router/scripts/guarded_auto.py" feedback
python "$HOME/.codex/skills/agent-auto-router/scripts/guarded_auto.py" feedback --maximum-routes 2000 --retention-days 30 --apply
```

第一条命令不会写文件。压缩过程使用反馈追加锁和原子替换，保留 route/label 配对，结果仍明确返回 `storesTaskText=false` 与 `modelCalls=0`。默认不记录；只有传入 `-EnableFeedback` 才会启用，`-FeedbackFile` 也要求该开关。`-NoFeedback` 继续作为兼容参数接受，`-StateDir` 可隔离状态。

Desktop 执行报告 ID 使用独立的无正文幂等标记。已完成标记同样默认保留 90 天、最多 5000 个；pending/incomplete 标记永不自动删除，并明确提示需要人工检查。由于这是有界精确窗口，标记过期后再次提交相同报告会按新报告处理。可以先检查，再显式应用自定义窗口：

```powershell
python "$HOME/.codex/skills/agent-auto-router/scripts/guarded_auto.py" reports
python "$HOME/.codex/skills/agent-auto-router/scripts/guarded_auto.py" reports --maximum-markers 2000 --retention-days 30 --apply
python "$HOME/.codex/skills/agent-auto-router/scripts/guarded_auto.py" recover-report --report-id REPORT_ID
python "$HOME/.codex/skills/agent-auto-router/scripts/guarded_auto.py" recover-report --report-id REPORT_ID --action release-for-retry --confirm-report-id REPORT_ID --resolved-by OPERATOR
python "$HOME/.codex/skills/agent-auto-router/scripts/guarded_auto.py" recover-report --report-id REPORT_ID --action acknowledge-recorded --confirm-report-id REPORT_ID --resolved-by OPERATOR
```

恢复流程必须先检查。只有标记阶段和匹配反馈都为空时，`release-for-retry` 才会先归档标记、再释放报告 ID；`acknowledge-recorded` 要求恰好一条匹配的路由结果及预期标签证据，不删除也不重写反馈，未完成的学习周期会留给显式 `cycle` 命令。两种变更都要求报告 ID 精确确认与操作人标识，模型调用数为零，也没有策略激活权限。

### 学习模式

配置只接受以下三种模式；旧 `manual` 和 `guarded-auto` 值会明确报错，不做静默迁移：

| 模式 | 保存路由结果 | 自动调整阈值 |
| --- | --- | --- |
| `off` | 否 | 否 |
| `observe` | 是，默认 | 否 |
| `guarded` | 是 | 仅通过有界 canary 生命周期 |

所有模式都保留人工审批候选命令：

```powershell
python "$HOME/.codex/skills/agent-auto-router/scripts/policy_learning.py" status

python "$HOME/.codex/skills/agent-auto-router/scripts/policy_learning.py" label `
  --route-id "<route-id>" `
  --preferred-model gpt-5.6-terra `
  --outcome pass

python "$HOME/.codex/skills/agent-auto-router/scripts/policy_learning.py" propose `
  --output "./candidate-policy.json"

python "$HOME/.codex/skills/agent-auto-router/scripts/policy_learning.py" approve `
  --candidate "./candidate-policy.json" `
  --approved-by "reviewer-name"
```

默认至少需要 20 条有效人工标签。候选必须通过独立验证集、完整性摘要、当前活动策略、模型注册表和评测先验复核。生成候选不会修改活动策略；批准会归档旧版本并写入审计记录。

回滚最近的不同版本：

```powershell
python "$HOME/.codex/skills/agent-auto-router/scripts/policy_learning.py" rollback `
  --approved-by "reviewer-name"
```

### Guarded 学习

默认 `observe` 不会自动调整阈值。显式启用 `guarded` 后也只接受两类强信号：

1. 用户明确选择了更合适的模型。
2. 初始层确定性验证失败，相邻更强层验证通过，且任务非高风险、非显式覆盖。

普通成功、退出码 0、低延迟或更少 Token 不是质量标签。

```powershell
python "$HOME/.codex/skills/agent-auto-router/scripts/guarded_auto.py" status
python "$HOME/.codex/skills/agent-auto-router/scripts/guarded_auto.py" configure --mode guarded
python "$HOME/.codex/skills/agent-auto-router/scripts/guarded_auto.py" cycle --dry-run
python "$HOME/.codex/skills/agent-auto-router/scripts/guarded_auto.py" shadow
python "$HOME/.codex/skills/agent-auto-router/scripts/guarded_auto.py" configure --mode observe
python "$HOME/.codex/skills/agent-auto-router/scripts/guarded_auto.py" configure --mode off
```

较激进的 guarded 默认值在 12 个强信号后评估，使用 20% canary，canary 与 baseline 各要求 6 个确定性验证报告，probation 要求 12 个报告。自动候选对每个阈值仍最多向更强能力层移动一步，并依次经过 held-out 验证、确定性 canary、probation 和回滚门禁。重新评估改用最新强证据时间，而不是只增不减的事件总数，因此达到反馈保留上限后不会让学习永久停滞。状态、配置、审批和回滚使用有界 OS 文件锁；JSONL 流使用独立追加锁。

`shadow` 是当前 canary/probation 候选（或显式 `--candidate` 文件）的只读 A/B 预览。它使用同一批保留证据和确定性 holdout 比较 baseline/candidate，并增加 Wilson 准确率区间、配对精确符号检验、最小效应门槛，以及按策略、风险、标签来源划分的隐私安全统计；少于 3 个样本的分层会被抑制。结果区分证据不足、回归、有改善迹象但置信度不足，以及具有统计支持的候选优势。它不返回 route ID，并始终返回 `activationAuthorized=false`、`modelCalls=0`；影子结果绝不会激活策略。

学习状态和反馈属于受保护控制面，必须位于所有子进程可写根之外。`danger-full-access`、不可验证的外部沙箱或子进程可写学习证据时，`guarded` 会阻断执行。

特征语义使用 `featureSchemaVersion` 版本化。当前 v3 数据可以参与学习；缺失版本的历史记录在反馈保留窗口内仍可按 legacy v1 读取，v2 等旧版本也仅保留审计可读性，不能进入新候选、canary 或 probation 统计。

完整协议见 [guarded-auto-learning.md](skills/agent-auto-router/references/guarded-auto-learning.md)。

## 验证失败后的单次升级

只有用户显式提供确定性验证命令并启用 `-EscalateOnValidationFailure` 时，CLI 单任务才允许从当前 tier 升级到相邻更强 tier，最多一次：

```powershell
& "$HOME/.codex/skills/agent-auto-router/scripts/invoke_auto_task.ps1" `
  -Task "实现修改并通过测试" `
  -Model auto `
  -HostPermissionsJson $currentHostPermissionsJson `
  -Workdir "D:/path/to/project" `
  -ValidationCommand @('python', '-m', 'unittest', 'discover', '-s', 'tests') `
  -EscalateOnValidationFailure
```

显式模型不允许自动升级。认证、provider、模型不可用、网络或权限错误也不会触发升级。

## 扩展模型注册表

模型定义在 [model_registry.json](skills/agent-auto-router/scripts/model_registry.json)。关键字段：

| 字段 | 含义 |
| --- | --- |
| `enabled` | 允许用户显式选择 |
| `autoEligible` | 允许 Auto 能力层解析器选择 |
| `reviewedAt` | 新鲜度门禁使用的 ISO 复核日期 |
| `tier` | `fast`、`balanced` 或 `frontier` |
| `priority` | 同层同角色的选择优先级，数值越小越优先 |
| `capabilities` | 模型可承担的能力 |
| `allowedRoles` | 模型可用于哪些编排角色 |

新模型推荐上线顺序：

1. 添加模型，设置 `enabled: true`、`autoEligible: false`。
2. 运行注册表校验。
3. 在隔离、只读环境中显式调用该模型。
4. 使用相同用例和外部验收标准进行匹配评测。
5. 达标后再设置 `autoEligible: true`，复核 tier、role 和 priority。
6. 运行完整测试、离线评测和 DryRun，人工审核后安装。

```powershell
python "./skills/agent-auto-router/scripts/validate_model_registry.py" --fail-on-stale
```

## 评测与开发

### 离线路由评测

```powershell
python "./skills/agent-auto-router/scripts/evaluate_auto_router.py" `
  --output "./auto-router-eval.json"
```

该命令不调用模型，检查三种策略、A-F 可达性、高风险边界、中文路由、词法边界、注册表和固定评测先验。

### 匹配效率评测

```powershell
python "./skills/agent-auto-router/scripts/evaluate_development_routes.py" `
  --results "./matched-results.json" `
  --output "./matched-summary.json"
```

输入只能包含 case ID、configuration、可选 model/effort、外部验收结果、可选 Token、耗时和重试次数，不能包含提示词或输出。工具先比较验收通过率，只在同一用例双方都通过且 Token 完整时计算差异。

需要真实 CLI 模型调用的开发评测已移出安装包，位于 [benchmarks/](benchmarks/README.md)。可复现用例放在 `benchmarks/cases/`，工具放在 `benchmarks/tools/`；生成结果默认统一写入用户级 `agent-auto-router/evaluations/<kind>/<run-id>/`，也可由 `AGENT_AUTO_ROUTER_EVALUATIONS_DIR` 覆盖。它不是 Skill 运行依赖；运行前必须明确模型调用预算并使用隔离工作区。`--route-only` 只生成路由报告，不调用模型。

### 测试与 Skill 校验

```powershell
python -m unittest discover -s tests -p "test_*.py"
python "./skills/agent-auto-router/scripts/validate_model_registry.py" --fail-on-stale
python "./skills/agent-auto-router/scripts/doctor.py"
python "./skills/agent-auto-router/scripts/evaluate_auto_router.py"
python "./scripts/validate_skill.py"
python "./scripts/validate_plugin.py"
$pluginTestHome = Join-Path $env:TEMP "agent-auto-router-plugin-test"
python "./scripts/install_personal_plugin.py" --home "$pluginTestHome" --skip-codex-install
python -m compileall -q skills/agent-auto-router/scripts scripts benchmarks tests
```

仓库内的 `validate_skill.py` 不依赖 Codex 的个人安装路径，因此可用于普通开发机和 CI。若当前 Codex 环境安装了系统 `skill-creator`，还可以额外运行其官方校验：

```powershell
python "$HOME/.codex/skills/.system/skill-creator/scripts/quick_validate.py" `
  "./skills/agent-auto-router"
```

CI 在 Windows、Ubuntu 与 macOS、Python 3.10 与 3.12 上运行核心测试；每个平台都会验证注册表新鲜度、零调用诊断和离线路由，Windows 还验证 PowerShell 5.1、编排 DryRun 和重复安装。

## 插件与 Skill 结构

```text
.
├── .codex-plugin/plugin.json      # Codex 插件清单
├── SECURITY.md                    # 审计修复记录与剩余限制
├── benchmarks/                    # 不随 Skill 安装的开发评测资产
│   ├── cases/                     # 可复现输入
│   └── tools/                     # 评测与模拟工具
├── scripts/
│   ├── install_personal_plugin.py # 个人 marketplace 安装
│   ├── validate_plugin.py         # 便携插件校验
│   └── validate_skill.py          # 便携 Skill 校验
└── skills/agent-auto-router/
    ├── SKILL.md                   # Codex 执行指令与引用路由
    ├── agents/openai.yaml         # Skill UI 元数据
    ├── references/
    │   ├── entrypoints.md         # 完整入口与宿主执行流程
    │   ├── router-contract.md     # 路由、权限、隐私和失败契约
    │   ├── benchmark-routing.md   # 评测先验更新规则
    │   └── guarded-auto-learning.md
    └── scripts/
        ├── aar.ps1                # 普通用户 run/doctor 入口
        ├── quick_profiles.json    # 固定 safe/standard 预设
        ├── invoke_auto_task.ps1   # Desktop/CLI 专家入口
        ├── invoke_orchestrated_task.ps1
        ├── route_contract.py      # 严格 route-decision 与执行信封协议
        ├── doctor.py              # 隐私安全的零调用诊断
        ├── host_execution_plan.py # 通用宿主计划
        ├── model_registry.json
        ├── guarded_auto.py
        └── install.ps1            # 独立 Skill 兼容安装
```

`SKILL.md` 只保留另一个 Codex 实例执行任务所需的核心流程；面向用户的安装、示例和维护说明集中在本 README，详细协议按需放在 `references/`。

## 常见阻断

| 状态或错误 | 含义与处理 |
| --- | --- |
| `desktop_host_permissions_required` | 当前 Desktop turn 没有提供可信权限快照；不要从任务文本伪造 |
| `desktop_model_unavailable` | runtime 未声明选中模型；调整可用模型或显式选择，不能静默替代 |
| `guarded-auto-state-writable-by-child` | 子进程可修改学习证据；把状态移出可写根或切回 `observe` |
| `failed_no_workspace_changes` | 写任务成功返回但 Git 状态未变化；核对任务或显式使用 `-AllowNoChanges` |
| `workspace_status_unknown` | 有界 Git 状态无法确认；恢复 Git/元数据访问，或改用只读执行 |
| `results_dir_writable_by_child` | 报告目录对子进程可写；移到所有可写根之外，full-access 执行则省略报告目录 |
| `report_privacy_boundary_unverified` | 无法验证敏感 Windows 报告的 DACL；选择私有目录并恢复 PowerShell ACL 支持 |
| 安装后仍是旧行为 | 重启 Codex，并比较源码与安装副本；不要只修改仓库而忘记重新安装 |
| 出现两个同名 Skill | 插件版和独立 Skill 版同时启用；保留一种安装方式，不要删除学习状态 |

## 卸载

插件版使用安装器输出的 `marketplaceName` 移除；默认新建的个人 marketplace 名称是 `personal`：

```powershell
codex plugin remove agent-auto-router@personal
```

这会移除 Codex 的已安装配置和缓存，不会自动删除 `~/plugins/agent-auto-router` 或个人 marketplace 中的源条目，便于之后重新安装。若安装器输出的名称不是 `personal`，请替换为该实际名称。

独立 Skill 版删除已安装的 Skill 目录：

```powershell
Remove-Item -LiteralPath "$HOME/.codex/skills/agent-auto-router" -Recurse -Force
```

学习状态默认位于 `~/.codex/auto-router`，不会随 Skill 卸载自动删除。这样可以避免误删活动策略、反馈、审计和回滚历史；如需删除，应先单独审查并备份该目录。

## License

本项目采用 MIT License，详见 [LICENSE](LICENSE)。
