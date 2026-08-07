# Codex Auto Router

为 Codex 从受信模型注册表中自动选择模型的隔离式路由 Skill；默认注册 Sol、Terra 和 Luna。

它在任务执行前使用本地确定性规则完成选模，然后通过 Codex Desktop 原生子代理协议或用户已经登录的官方 `codex exec` 执行。路由过程不调用额外模型，不修改 Codex 全局配置，也不接管 CC Switch 的账号和会话管理。

## 设计目标

- 根据任务复杂度、风险、约束程度和 reasoning effort 自动选择模型。
- Desktop 后端复用当前主代理已有的 `spawn_agent` 能力；CLI 后端保留原有独立登录执行方式。
- 不修改 `~/.codex/config.toml`、`model_provider` 或 `model_catalog_json`。
- 不启动本地 Responses 代理，不接触或转发登录凭据。
- 不读取、复制或转发 Desktop 凭证，不附着现有 Desktop app-server stdio。
- 与 CC Switch 的账号切换和历史会话同步保持隔离。
- 支持 Sol 规划、Terra 调度、Luna 执行、Sol 验收的高级编排评测。
- 支持隐私最小化反馈、候选策略离线验证、人工审批生效和版本回滚。
- 支持通过版本化注册表扩展模型，并把“允许显式试用”和“允许 Auto 选择”分开。
- 联合选择模型、reasoning effort、直接/编排拓扑和仓库上下文预算。
- 单模型与编排路径统一采集 CLI 可观察 Token，并按验收通过结果衡量效率。
- 可选的一次性验证失败升级；必须由用户显式开启并提供确定性验证命令。

## 工作方式

```text
用户任务
  -> 本地确定性分类
  -> 读取已审批的活动策略
  -> 选择能力层、effort、拓扑和上下文预算
  -> 从受信注册表解析具体模型
  -> Desktop: 输出 desktop-plan.v1，由主代理启动唯一 direct 子代理
     或 CLI: 通过 UTF-8 stdin 调用 codex exec
  -> CLI 可记录不含任务正文的路由结果；Desktop v1 仅返回计划
  -> 返回执行结果
```

Auto 是一次任务开始前的路由决策，不是第四个模型，也不会出现在 Codex Desktop 的原生模型下拉框中。Desktop v1 只允许一个 direct 子代理作为唯一写入者；模型不可用或路由要求 B/C/D 多角色拓扑时会显式阻断。两种后端都不会改变当前 Desktop 对话所使用的模型，也不会静默切换模型、effort、provider 或后端。

## 模型策略

默认注册表映射如下：

| 能力层 | 默认模型 | 主要用途 |
| --- | --- | --- |
| `frontier` | `gpt-5.6-sol` | 高风险、架构设计、复杂重构、歧义或深度推理任务 |
| `balanced` | `gpt-5.6-terra` | 日常开发、常规调试和均衡型任务 |
| `fast` | `gpt-5.6-luna` | 格式化、提取、翻译、重复性和成本敏感任务 |

支持三种路由策略：

| 策略 | 行为 |
| --- | --- |
| `intelligence` | 质量优先，复杂任务使用 `frontier`，其余主要使用 `balanced` |
| `balance` | 推荐默认值；简单任务用 `fast`，常规任务用 `balanced`，复杂或高风险任务用 `frontier` |
| `cost` | 模型层级成本代理；默认使用 `fast`，复杂任务使用 `balanced`，高风险任务才使用 `frontier` |

## 扩展其它模型

模型统一登记在 `scripts/model_registry.json`。每个模型需要声明 ID、别名、能力层、选择优先级、质量/成本/延迟等级、默认 effort、能力、允许角色以及两个独立开关：

- `enabled: true`：允许用户通过 `-Model` 显式试用。
- `autoEligible: true`：允许能力层解析器自动选择；新模型初次加入时应保持 `false`。

同一能力层和角色存在多个可自动选择模型时，数值更小的 `priority` 优先。编排角色位于 `scripts/orchestration_profiles.json`，默认 A-F 行为保持不变，但角色绑定的是能力层，不再硬编码模型 ID。profile 中显式填写的模型同样属于 Auto 路由，必须保持 `autoEligible: true` 并满足任务要求的能力层和能力；校验器会额外确认高风险 A/B/C 的最终写入角色仍满足 `frontier + high-risk-primary`。

修改注册表后先执行零模型调用校验：

```powershell
python "./skills/codex-auto-router/scripts/validate_model_registry.py"
```

安全上线顺序：

1. 登记新模型，设置 `enabled: true`、`autoEligible: false`。
2. 使用 `-Model <alias> -Sandbox read-only` 做受控真实调用，确认当前 Codex provider 确实支持该模型。
3. 在相同用例上进行匹配评测，记录质量、Token、耗时和失败率。
4. 评测达标后设置 `autoEligible: true`，确定能力层、角色和 `priority`。
5. 再次运行注册表校验、完整测试和 Dry Run，人工审核后安装。

注册表不会安装模型、切换 provider 或修改 Codex 配置。当前 provider 不支持的模型会明确失败，不会静默回退。

## 路由权衡与限制

确定性路由不产生额外模型调用，行为可解释且延迟低，但无法准确理解所有任务语义：普通文本中偶然出现风险词可能导致误升级，缺少触发词的复杂任务也可能误降级。用户显式指定的模型或 reasoning effort 始终优先，代表性误判应加入测试样例后再调整规则。

`cost` 只根据模型层级选择相对经济的模型。Codex CLI 不提供逐调用账单，因此当前评测不能证明实际节省金额，结果中的未知成本使用 `null`，不能按 `0` 美元解释。

## 环境要求

- Windows PowerShell 5.1 或 PowerShell 7
- Python 3.10 或更高版本
- Desktop 后端：当前 Codex Desktop runtime 提供受支持模型的 `spawn_agent` 能力
- CLI 后端：已安装并独立登录 Codex CLI

Skill 不要求也不会读取用户的 API Key 或 Desktop 凭证。实际计费和额度取决于当前执行后端的登录方式。

## 安装

克隆仓库：

```powershell
git clone https://github.com/jinweechen/codex-auto-router.git
cd codex-auto-router
```

安装到个人 Codex Skills 目录：

```powershell
& "./skills/codex-auto-router/scripts/install.ps1"
```

重复运行安装脚本会通过暂存目录安全替换旧版本，不会生成嵌套的 `codex-auto-router/codex-auto-router`。需要保留旧版本时添加 `-Backup`；备份保存在 `~/.codex/skill-backups/codex-auto-router`，避免被 Codex 重复识别为另一个同名 Skill。

安装后重新启动 Codex，让 Skill 元数据重新加载。

## 在 Codex Desktop 中使用

直接在对话中引用 Skill：

```text
$codex-auto-router 使用 balance 策略，自动选择合适模型完成当前任务。
```

主代理会把当前 Desktop runtime 明确支持的模型 ID 交给本地路由器。路由器输出不含任务正文、但包含规范化工作目录和上下文 profile 的 `codex-auto-router.desktop-plan.v1`；只有 `executionRequested=true` 且 `status=ready` 时，主代理才按精确模型、effort、`forkTurns=none` 和 workdir 启动一个 direct 子代理。Desktop DryRun 返回同一 schema，但计划调用数为零。若模型不可用或要求多角色拓扑，则停止并报告阻断原因，不回退到 CLI。Desktop v1 不记录本地规划器无法观察的子代理执行反馈或 Token。

指定项目目录和任务：

```text
$codex-auto-router 在 D:\AgentProject\my-project 中，
使用 balance 策略审查并优化认证模块。
```

只解释路由、不调用模型：

```text
$codex-auto-router 对以下任务执行 DryRun，只返回路由计划和原因，不启动执行：
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

Desktop-native 计划：

```powershell
& "$HOME/.codex/skills/codex-auto-router/scripts/invoke_auto_task.ps1" `
  -Task "审查并优化当前项目" `
  -ExecutionBackend desktop `
  -DesktopAvailableModels @('gpt-5.6-sol', 'gpt-5.6-terra') `
  -Model auto `
  -Strategy balance `
  -Workdir "D:/path/to/project" `
  -Explain
```

现有 CLI 执行：

```powershell
& "$HOME/.codex/skills/codex-auto-router/scripts/invoke_auto_task.ps1" `
  -Task "审查并优化当前项目" `
  -ExecutionBackend cli `
  -Model auto `
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

### 显式覆盖模型

```powershell
& "$HOME/.codex/skills/codex-auto-router/scripts/invoke_auto_task.ps1" `
  -Task "审查认证模块" `
  -Model sol `
  -Effort xhigh `
  -Workdir "D:/path/to/project"
```

`-Model` 支持 `auto`，以及受信注册表内所有已启用模型的别名和完整 ID。显式模型只覆盖当前任务，不修改 Codex 全局配置；`autoEligible: false` 的模型仍可显式试用。

每次非 Dry Run 的 CLI 单模型或 CLI 编排执行默认记录一个隐私最小化结果：路由 ID、数值/布尔特征、选择的模型、退出码、耗时，以及 CLI JSON 事件实际暴露的 input、cached input、output、reasoning output Token。无法观测时记录为 `null`，不会猜测或按零处理。日志不会保存任务正文、模型回复、工具输出或凭据。使用 `-Explain` 查看路由 ID，使用 `-NoFeedback` 关闭本次记录；`-StateDir` 和 `-FeedbackFile` 可隔离状态位置。Desktop v1 只输出计划，不伪造执行结果。

Auto 同时给出 effort、直接/编排拓扑和分层上下文预算。路由前只读检查仓库结构，确定性排序候选路径；微型仓库没有相关候选时不注入仓库摘要，避免无效 Token。显式模型和 effort 始终优先。

### 显式验证失败升级

只有用户明确允许、且首次模型执行成功但确定性验证失败时，才会升级到下一个受信能力层，最多一次：

```powershell
& "$HOME/.codex/skills/codex-auto-router/scripts/invoke_auto_task.ps1" `
  -Task "实现修改并通过测试" `
  -Model auto `
  -Workdir "D:/path/to/project" `
  -ValidationCommand @('python', '-m', 'unittest', 'discover', '-s', 'tests') `
  -EscalateOnValidationFailure
```

验证命令按 argv 数组执行，不解释为任意 Shell 字符串。升级前会明确警告，升级后重新验证；显式模型不允许自动升级。认证、网络、provider、模型不可用、沙箱等 CLI 失败直接返回，不触发升级。升级后的成功不会被用于训练初始能力层阈值。

## 离线路由评估

离线评估不会调用任何模型：

```powershell
python "$HOME/.codex/skills/codex-auto-router/scripts/evaluate_auto_router.py" `
  --output "./auto-router-eval.json"
```

评估内容包括：

- 三种策略的代表性路由结果
- 受信模型注册表及高风险能力门槛
- `xhigh` 升级行为
- 中文约束型任务识别
- 零路由模型调用保证

## 审批式自我优化

路由器可以根据人工标注过的真实结果自动生成候选阈值，但不会自行改写 Python 代码，也不会自动发布候选策略。完整闭环为：

```text
自动记录路由结果
  -> 人工标注更合适的模型
  -> 确定性优化器生成候选阈值
  -> 独立验证集和安全门检查
  -> 人工显式批准
  -> 活动策略生效并保留回滚快照
```

查看状态：

```powershell
python "$HOME/.codex/skills/codex-auto-router/scripts/policy_learning.py" status
```

`status.efficiency` 会显示 Token 覆盖率、人工验收结果、按最终模型汇总的通过率和平均可观察 Token。只有全部已标注任务都有 Token 数据时才计算 `observedTokensPerPass`。

根据执行时显示的 `routeId` 标注结果：

```powershell
python "$HOME/.codex/skills/codex-auto-router/scripts/policy_learning.py" label `
  --route-id "<route-id>" `
  --preferred-model gpt-5.6-terra `
  --outcome pass
```

默认至少需要 20 条带人工标签的自动路由。达到门槛后，`label` 会自动在 `~/.codex/auto-router/candidates` 下生成候选；可以添加 `--no-auto-propose` 禁用。也可以手动指定候选输出位置。生成候选不会改变当前策略，也不会调用模型：

```powershell
python "$HOME/.codex/skills/codex-auto-router/scripts/policy_learning.py" propose `
  --output "./candidate-policy.json"
```

显式模型或编排变体覆盖仍会被记录，但不会进入学习样本，避免把一次人工覆盖直接当作通用策略。检查候选文件中的 `eligibleForApproval`、验证集准确率、加权损失、误降级数和 `safetyChecks`。只有满足门禁的候选才能显式批准；批准时会用当前反馈日志重新计算指标，不能只靠修改候选文件绕过门禁：

```powershell
python "$HOME/.codex/skills/codex-auto-router/scripts/policy_learning.py" approve `
  --candidate "./candidate-policy.json" `
  --approved-by "reviewer-name"
```

回滚到最近一个不同的历史版本：

```powershell
python "$HOME/.codex/skills/codex-auto-router/scripts/policy_learning.py" rollback `
  --approved-by "reviewer-name"
```

学习状态默认保存在 `~/.codex/auto-router`，与 Skill 安装目录分离，因此重新安装 Skill 不会丢失活动策略、审计日志和回滚历史。策略 schema v2 学习的是 `fast / balanced / frontier` 复杂度边界，不学习任意模型 ID。高风险任务始终要求 `frontier + high-risk-primary`；注册表、风险词表和用户显式覆盖不会被优化器修改。显式试用但尚未进入 Auto 的模型标签也不会参与阈值学习。

## 匹配开发效率评测

要判断某个模型、effort 或拓扑是否真正节省 Token，必须对同一批 `caseId` 使用同一外部验收标准。结果文件只能包含 `caseId`、`configuration`、可选的 `model`/`effort`、`accepted`、可选 `tokens`、`durationMs` 和可选 `retries`，不能包含任务正文或回复：

```powershell
python "$HOME/.codex/skills/codex-auto-router/scripts/evaluate_development_routes.py" `
  --results "./matched-results.json" `
  --output "./matched-summary.json"
```

工具先比较验收通过率，只在双方都通过的匹配用例上计算 Token 差异；Token 覆盖不完整时不计算每个通过任务的 Token。CLI 计数不是账单金额，因此不会输出虚构成本。

## 单元测试

项目使用 Python 标准库 `unittest`，没有第三方运行时依赖：

```powershell
python -m unittest discover -s tests -p "test_*.py"
```

## 多模型编排执行

正式执行入口会自动选择 A-F 编排变体。规划、调度、Luna workers 和 grader 均为只读；只有直接执行模型或最终 reviewer 可以修改工作区：

```powershell
& "$HOME/.codex/skills/codex-auto-router/scripts/invoke_orchestrated_task.ps1" `
  -Task "实现认证模块重构并补充测试" `
  -Strategy balance `
  -Workdir "D:/path/to/project" `
  -MaxWorkers 2 `
  -Explain
```

显式指定 C 变体：

```powershell
& "$HOME/.codex/skills/codex-auto-router/scripts/invoke_orchestrated_task.ps1" `
  -Task "实现认证模块重构并补充测试" `
  -Variant C `
  -Workdir "D:/path/to/project"
```

使用 `-DryRun` 只查看路由，模型调用数为零。使用 `-Sandbox read-only` 可执行完整编排但禁止最终角色写入。

正式执行默认要求 Git 工作区干净，避免编排修改与已有改动混在一起。只有明确接受该风险时才使用 `-AllowDirty`。普通短任务即使出现“API 和测试”等并行词，也不会自动进入 B/C/D；可以通过 `-Variant C` 显式覆盖。

长任务默认输出逐角色 JSON 进度事件，并受总时限和调用预算约束：

```powershell
& "$HOME/.codex/skills/codex-auto-router/scripts/invoke_orchestrated_task.ps1" `
  -Task "实现模块并补充测试" `
  -Variant D `
  -Workdir "D:/path/to/project" `
  -TotalTimeout 1800 `
  -MaxModelCalls 7 `
  -PlannerEffort high `
  -WorkerEffort medium `
  -ReviewerEffort high `
  -GraderEffort high `
  -ResultsDir "./orchestration-results"
```

正式子进程会设置 `PYTHONDONTWRITEBYTECODE=1`，减少测试过程中产生 `__pycache__`。`-Quiet` 可以关闭 stderr 进度事件。

为了减少成功任务的总 Token，正式执行默认采用风险感知验收：低风险 A/E/F 和 D 不再额外调用 grader，B/C 与高风险任务仍保留独立验收。可以使用 `-GraderPolicy always` 强制验收，或使用 `-GraderPolicy never` 明确关闭。D 最多规划两个 workers。

使用 `-MaxTotalTokens` 设置 Codex CLI 已暴露 Token 的软预算，并把并发执行中尚未完成调用的预计 Token 预留计入判断。达到预算后停止新的非写入角色，但仍允许最终 direct/reviewer 完成交付，避免已经消耗的规划 Token 失去结果。报告中的 Token 是 CLI 可观察值，不代表完整账单。

默认 `-ContextMode lean` 只对 planner、dispatcher、worker、grader 等只读角色忽略个人 Codex 配置，并始终保留仓库规则；direct/reviewer 保留用户配置，确保写入权限正常。只读角色也依赖自定义 provider 或个人配置时使用 `-ContextMode full`。低风险 Terra 默认使用 `medium` effort，并要求批量读取、单次编辑和合并验证，减少 agent 工具循环造成的累计输入 Token。

Token 报告进一步区分 `cached_input`、`uncached_input` 和 `reasoning_output`。CLI 的 input 计数可能累计一次 agent 运行中的多轮工具交互，不能解释为单个任务提示词长度。

`workspace-write` 返回成功但 Git 状态没有变化时，执行结果会标记为 `failed_no_workspace_changes` 并返回非零。只有任务允许“无需修改”时才使用 `-AllowNoChanges`。

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
- 在反馈日志中记录任务正文、模型回复或工具输出
- 未经人工审批激活候选路由策略
- 通过学习降低高风险任务的 `frontier + high-risk-primary` 安全边界
- 从任务文本、环境内容或模型输出动态注入模型 ID
- 修改当前 Desktop 对话的模型
- 在模型不可用时静默切换到其他层级
- 未经允许使用 `danger-full-access`

路由输入通过 UTF-8 标准输入传给本地规划脚本，避免暴露在命令行参数中；Desktop 计划不包含任务正文，主代理只把当前原始任务交给新建的 direct 子代理。

## 项目结构

```text
.
├── LICENSE
├── pyproject.toml
├── .github/workflows/test.yml
├── tests/
│   ├── test_routing_policy.py
│   ├── test_model_registry.py
│   ├── test_policy_learning.py
│   ├── test_orchestration_engine.py
│   ├── test_desktop_execution.py
│   └── test_orchestrated_execution.py
└── skills/codex-auto-router/
    ├── SKILL.md
    ├── agents/openai.yaml
    ├── references/
    │   ├── entrypoints.md
    │   └── router-contract.md
    └── scripts/
        ├── model_registry.json
        ├── model_registry.py
        ├── orchestration_profiles.json
        ├── orchestration_profiles.py
        ├── validate_model_registry.py
        ├── routing_policy.py
        ├── policy_learning.py
        ├── efficiency_metrics.py
        ├── evaluate_development_routes.py
        ├── desktop_execution.py
        ├── execution_plan.py
        ├── repository_context.py
        ├── single_task_runner.py
        ├── select_auto_model.py
        ├── auto_router.py
        ├── invoke_auto_task.ps1
        ├── invoke_orchestrated_task.ps1
        ├── invoke_orchestrated_task.py
        ├── codex_cli_client.py
        ├── execution_policy.py
        ├── install.ps1
        ├── evaluate_auto_router.py
        ├── orchestration_engine.py
        ├── codex_cli_orchestration_eval.py
        └── eval_cases.json
```

仓库只保留并发布 `codex-auto-router`，避免旧编排 Skill 与当前注册表、执行策略和安全边界漂移。

## 卸载

删除个人 Skill 目录即可：

```powershell
Remove-Item -LiteralPath "$HOME/.codex/skills/codex-auto-router" -Recurse -Force
```

该操作不需要恢复 Codex 配置，因为隔离版从不修改全局配置。

## License

本项目采用 MIT License，详见 [LICENSE](LICENSE)。
