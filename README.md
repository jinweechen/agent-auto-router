# Codex Auto Router

为 Codex 自动选择 GPT-5.6 Sol、Terra 或 Luna 的隔离式路由 Skill。

它在任务执行前使用本地确定性规则完成选模，然后直接调用用户已经登录的官方 `codex exec`。路由过程不调用额外模型，不修改 Codex 全局配置，也不接管 CC Switch 的账号和会话管理。

## 设计目标

- 根据任务复杂度、风险、约束程度和 reasoning effort 自动选择模型。
- 复用 Codex CLI 当前登录状态，无需单独提供 API Key。
- 不修改 `~/.codex/config.toml`、`model_provider` 或 `model_catalog_json`。
- 不启动本地 Responses 代理，不接触或转发登录凭据。
- 与 CC Switch 的账号切换和历史会话同步保持隔离。
- 支持 Sol 规划、Terra 调度、Luna 执行、Sol 验收的高级编排评测。

## 工作方式

```text
用户任务
  -> 本地确定性分类
  -> 选择 Sol / Terra / Luna
  -> 通过 UTF-8 stdin 调用 codex exec
  -> 返回执行结果
```

Auto 是一次任务开始前的路由决策，不是第四个模型，也不会出现在 Codex Desktop 的原生模型下拉框中。每次执行会启动一个独立的 Codex CLI 任务，不会改变当前 Desktop 对话所使用的模型。

## 模型策略

| 模型 | 主要用途 |
| --- | --- |
| `gpt-5.6-sol` | 高风险、架构设计、复杂重构、歧义或深度推理任务 |
| `gpt-5.6-terra` | 日常开发、常规调试和均衡型任务 |
| `gpt-5.6-luna` | 格式化、提取、翻译、重复性和成本敏感任务 |

支持三种路由策略：

| 策略 | 行为 |
| --- | --- |
| `intelligence` | 质量优先，复杂任务使用 Sol，其余主要使用 Terra |
| `balance` | 推荐默认值；简单任务用 Luna，常规任务用 Terra，复杂或高风险任务用 Sol |
| `cost` | 成本优先；默认使用 Luna，复杂任务使用 Terra，高风险任务才使用 Sol |

## 环境要求

- Windows PowerShell 5.1 或 PowerShell 7
- Python 3
- 已安装 Codex CLI
- 已通过 ChatGPT、API Key 或企业 Access Token 登录 Codex CLI

Skill 不要求也不会读取用户的 API Key。实际计费和额度取决于当前 Codex 登录方式。

## 安装

克隆仓库：

```powershell
git clone https://github.com/jinweechen/codex-auto-router.git
cd codex-auto-router
```

安装到个人 Codex Skills 目录：

```powershell
$target = Join-Path $HOME ".codex/skills/codex-auto-router"
New-Item -ItemType Directory -Path (Split-Path $target) -Force | Out-Null
Copy-Item -LiteralPath "./skills/codex-auto-router" -Destination $target -Recurse -Force
```

安装后重新启动 Codex，让 Skill 元数据重新加载。

## 在 Codex Desktop 中使用

直接在对话中引用 Skill：

```text
$codex-auto-router 使用 balance 策略，自动选择合适模型完成当前任务。
```

指定项目目录和任务：

```text
$codex-auto-router 在 D:\AgentProject\my-project 中，
使用 balance 策略审查并优化认证模块。
```

只解释路由、不调用模型：

```text
$codex-auto-router 对以下任务执行 DryRun，只返回模型选择和原因：
重命名配置字段并更新相关文档。
```

请求多模型编排：

```text
$codex-auto-router 使用多模型编排：
Sol 负责规划和最终验收，
Terra 负责依赖调度，
Luna 负责执行边界清晰的子任务，
最大并发数为 2。
```

## PowerShell 直接调用

### 自动选模并执行

```powershell
& "$HOME/.codex/skills/codex-auto-router/scripts/invoke_auto_task.ps1" `
  -Task "审查并优化当前项目" `
  -Strategy balance `
  -Workdir "D:/path/to/project" `
  -Explain
```

### Dry Run

```powershell
& "$HOME/.codex/skills/codex-auto-router/scripts/invoke_auto_task.ps1" `
  -Task "格式化这段文本" `
  -Strategy balance `
  -Workdir "." `
  -DryRun `
  -Explain
```

### 指定 reasoning effort

```powershell
& "$HOME/.codex/skills/codex-auto-router/scripts/invoke_auto_task.ps1" `
  -Task "设计跨服务迁移方案" `
  -Strategy intelligence `
  -Effort xhigh `
  -Workdir "D:/path/to/project"
```

支持 `none`、`low`、`medium`、`high`、`xhigh` 和 `max`。显式指定 effort 时，路由器会保留用户选择。

## 离线路由评估

离线评估不会调用任何模型：

```powershell
python "$HOME/.codex/skills/codex-auto-router/scripts/evaluate_auto_router.py" `
  --output "./auto-router-eval.json"
```

评估内容包括：

- 三种策略的代表性路由结果
- 模型 allowlist
- `xhigh` 升级行为
- 中文约束型任务识别
- 零路由模型调用保证

## 多模型编排评测

准备测试用例：

```json
[
  {
    "id": "example",
    "prompt": "给出可实施的模块重构方案",
    "acceptance_criteria": [
      "说明模块边界",
      "提供迁移步骤",
      "列出回滚方案"
    ]
  }
]
```

运行 Sol/Terra/Luna 编排对比：

```powershell
python "$HOME/.codex/skills/codex-auto-router/scripts/codex_cli_orchestration_eval.py" `
  --cases "./cases.json" `
  --workdir "D:/path/to/project" `
  --results-dir "./eval-results" `
  --variants B,C `
  --limit 1 `
  --max-workers 2 `
  --planner-effort high `
  --dispatcher-effort medium `
  --worker-effort high `
  --reviewer-effort xhigh `
  --grader-effort high
```

只有在同一测试用例上完成匹配的 B/C 对比后，才能判断 Terra 调度是否带来收益。

## 安全边界

此项目明确不会：

- 修改 Codex 或 CC Switch 配置
- 添加 Desktop 原生 `Auto` 模型
- 替换全局模型 provider
- 启动 credential-forwarding 代理
- 读取、记录或转发登录凭据
- 修改当前 Desktop 对话的模型
- 在模型不可用时静默切换到其他层级
- 未经允许使用 `danger-full-access`

任务内容通过 UTF-8 标准输入传给子进程，避免暴露在命令行参数中。

## 项目结构

```text
skills/codex-auto-router/
├── SKILL.md
├── agents/
│   └── openai.yaml
├── references/
│   ├── entrypoints.md
│   └── router-contract.md
└── scripts/
    ├── select_auto_model.py
    ├── invoke_auto_task.ps1
    ├── evaluate_auto_router.py
    ├── auto_router.py
    ├── codex_cli_orchestration_eval.py
    └── eval_cases.json
```

## 卸载

删除个人 Skill 目录即可：

```powershell
Remove-Item -LiteralPath "$HOME/.codex/skills/codex-auto-router" -Recurse -Force
```

该操作不需要恢复 Codex 配置，因为隔离版从不修改全局配置。

## License

请根据使用场景补充合适的开源许可证。
