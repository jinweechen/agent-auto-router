# Paperclip 调研备忘

> 调研日期:2026-08-08
> 调研背景:评估 agent-auto-router 是否可参考 Paperclip(paperclipai/paperclip)的设计
> 调研方式:GitHub API + raw.githubusercontent + codeload 拉取仓库源码(commit 主分支,2026-08-08)
> 结论摘要:**只参考适配层(adapter registry / 模型清单 / model profile),不参考架构层(server/DB/UI 编排)**

---

## 1. Paperclip 是什么

- **仓库**:paperclipai/paperclip,~75.8k★,TypeScript,MIT,Node.js server + React UI
- **定位**:"AI 公司"控制平面——管理一组 AI agent 像管理一家公司。口号:"If OpenClaw is an employee, Paperclip is the company"
- **四大支柱**:
  | 支柱 | 内容 |
  | --- | --- |
  | Agentic Task Manager | 任务、审批与 review 门、可审计流程、diff/screenshot/test 验证 |
  | Org Chart for Agents | 人+agent 混合组织架构、职责/委派、治理权限、scoped secrets |
  | Agent Employee Training | Skill Studio、组织级共享 skills、evals、性能评审 |
  | Agentic OS | 跨提供商运行时、沙箱、MCP 集成、SSO/RBAC/成本控制 |
- **Adapter 生态**(packages/adapters/):claude-local、codex-local、cursor-cloud/local、gemini-local、hermes、hermes-gateway、openclaw-gateway、opencode-local、pi-local、grok-local + bash/http 内置
- **模型选择模型**:每个 agent 配置一个主模型 + cheap profile,并**不做任务级模型路由**——这是与 auto-router 的根本差异

## 2. 与 agent-auto-router 的定位对比

| 维度 | Paperclip | agent-auto-router |
| --- | --- | --- |
| 定位 | 多 agent 组织编排平台(server+UI+DB) | 单机执行前模型路由决策器(前置选择层) |
| 模型选择 | 不做任务→模型路由;agent 配固定模型 | 核心能力:能力层+策略+effort+拓扑联合选择 |
| 架构 | TS monorepo:Express/React/Drizzle/PGlite | 零依赖 Python 脚本(skill 形式) |
| 执行方式 | 心跳驱动、常驻服务、沙箱进程管理 | 一次性决策,交给 Codex Desktop/CLI 执行 |
| 评测 | promptfoo 通用框架 | 自家 eval(真实 CLI 调用,按验收结果衡量) |
| 信任模型 | server 持有凭据、预算、审计 | 不读取/转发凭据,不修改全局配置 |

**关键判断**:Paperclip 不做"任务复杂度→模型"的路由决策(它选 agent 不选模型)。auto-router 的核心机制(能力层分类、intelligence/balance/cost 策略、effort 联合选择、A-F 编排角色)在 Paperclip 中**不存在**,不是参考来源,是差异化优势。

## 3. 值得参考的点(按价值排序)

### 3.1 Adapter 注册表可插拔化 + 运行时校验 ★★★★★
- **出处**:`server/src/adapters/registry.ts`、`adapter-plugin.md`(feat/external-adapter-phase1 分支记录)
- **机制**:
  - `registerServerAdapter(adapter)` / `unregisterServerAdapter(type)` / `requireServerAdapter(type)`:内置 adapter 启动时注册进 mutable map
  - 输入校验开放:`z.enum(AGENT_ADAPTER_TYPES)` 改为接受任意非空字符串,server 注册表是"是否真的注册了"的运行时真相来源(`assertKnownAdapterType`)
  - UI 端同构:`ui/src/adapters/registry.ts` 同样可插拔
  - 刻意保留 `AGENT_ADAPTER_TYPES` 常量不删,控制爆炸半径
- **对 auto-router 的落地**:ExecutionAdapter(Protocol, codex/claude 双后端)已是接口抽象,缺**注册表 + 运行时校验层**。新增第三后端时:声明→注册→校验三步,而不是改死代码。可同步解决 Desktop 后端的 `desktop_backend_unsupported` 阻断逻辑统一化。

### 3.2 模型清单:静态 fallback + 动态探测 + 缓存 ★★★★
- **出处**:`server/src/adapters/codex-models.ts`、`packages/adapters/codex-local/src/index.ts`
- **机制**:
  - 内置 fallback 模型列表(如 GPT-5.6 系列 sol/terra/luna)
  - 动态探测 `https://api.openai.com/v1/models`(5s 超时、60s TTL 缓存、API key fingerprint 变化即失效)
  - 探测结果与 fallback 去重合并、numeric 排序
  - `normalizeCodexModel` / `isCodexLocalKnownModel` / `isCodexLocalFastModeSupported` 归一化与能力判断
- **对 auto-router 的落地**:`model_registry.json` 目前纯静态,README 承诺"provider 不支持的模型明确失败,不静默回退"。可增加**探测+fallback 合并**能力:探测失败回退静态清单,探测成功合并去重——把"明确失败"升级为"优雅降级",仍不违反不静默切换原则(降级需显式标注)。

### 3.3 Model Profile 契约化 ★★★★
- **出处**:`packages/adapter-utils/src/types.ts`(`AdapterModelProfileKey = "cheap"`、`AdapterModelProfileDefinition`、adapter 接口 `modelProfiles?` / `listModelProfiles?`)
- **机制**:低成本模型 profile 是 **adapter 自己声明的能力**(接口契约),不是路由层硬编码;新 agent 可指定 cheap profile
- **对 auto-router 的落地**:能力层(frontier/balanced/fast)目前写在注册表 JSON。可演进为"adapter 声明能力层 + 注册表解析"双源:claude 后端声明"提供 sonnet/haiku 两级",codex 后端声明三级,路由层按声明聚合——与现有 `priority` 机制兼容,不破坏现有注册表格式。

### 3.4 沙箱/执行隔离细节 ★★★
- **出处**:`packages/adapter-utils/sandbox-*.ts`、`execution-target.ts`、`git-workspace-sync.ts`、`workspace-restore-merge.ts`、codex-local 的 `workspaceStrategy: { type: "git_worktree", ... }`、`networkAllowlist`(bwrap + HTTP_PROXY/HTTPS_PROXY 注入)
- **对 auto-router 的落地**:当前"选完模交给 CLI 自己执行"无需沙箱。若未来走向受控执行(worktree 隔离、网络白名单、执行后恢复合并),这套是现成参考。**现阶段不引入**。

### 3.5 eval 的"同用例多模型对比"思想 ★★
- **出处**:`evals/`(promptfoo 框架,按 provider/模型对比同一批行为用例)
- **对 auto-router 的落地**:自家 eval 体系(eval_cases.json + 真实 CLI 调用 + 验收结果衡量)已更贴近场景,只借鉴"同一用例集跑多模型横向对比"的呈现方式,不换工具。

## 4. 不建议参考的点

1. **整体架构**(TS monorepo / Express / React / Drizzle / PGlite)——与 README 设计目标"路由过程不调用额外模型、不修改全局配置、隔离式"直接冲突
2. **模型路由机制本身**——Paperclip 没有任务级路由,无参考价值
3. **网关/代理模式**(hermes-gateway / openclaw-gateway)——面向"agent 公司"常驻服务,与本地隔离式路由定位冲突

## 5. 关联发现

- **NousResearch/hermes-paperclip-adapter**(~1.8k★):官方把 Hermes 作为"employee"接入 Paperclip 的适配器;Paperclip 仓库内置 `packages/adapters/hermes` + `hermes-gateway`
- 方向说明:这是"Hermes → Paperclip"生态接入,与 auto-router 无关;若未来想把 auto-router 的路由结果喂给 Paperclip 式编排,它是现成桥梁
- Paperclip 生态已有大量衍生项目(桥接、路由、balance 器),说明该平台 2026 年处于快速扩张期,版本演进快,参考其代码时需锁定 commit(本次基于 master,README 引用的 assets 是固定 commit 1ec33ff)

## 6. 落地建议(增量,不重构)

优先级排序:

| # | 增量 | 来源 | 工作量估计 | 验证方式 |
| --- | --- | --- | --- | --- |
| 1 | adapter 注册表 + 运行时校验(注册/反注册/require),第三后端可插拔 | 3.1 | 中 | 新增 adapter 的注册测试 + 现有 139 tests 回归 |
| 2 | 模型清单 fallback + 动态探测 + 缓存合并 | 3.2 | 小-中 | probe 脚本对比静态/动态结果;Dry Run 验证降级标注 |
| 3 | 能力层声明进 adapter 协议(与注册表双源,兼容 priority) | 3.3 | 中 | validate_model_registry.py 扩展 + 评测回归 |

不建议做:引入 server/DB/UI、常驻服务、网关代理。

---

*归档说明:本文档为调研证据类,记录于 docs/ 不入 git;如后续按建议立项,开发任务书参照 AICreationStudio 的 AI 开发执行计划模式另行编写。*
