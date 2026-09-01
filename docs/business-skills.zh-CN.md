# 业务 Skill、Scenario 与 Capability

> 语言：[English](business-skills.md) | [中文](business-skills.zh-CN.md)

LIPAS 0.35 在执行核心之外扩展业务广度：

| 层次 | 负责内容 | 绝不负责 |
| --- | --- | --- |
| Skill | 只含 instruction 的领域方法 | 文件、网络、账号或交付 authority |
| BusinessScenario | 选定的 Skill、生命周期和 Tool 契约 | 第二套 Run 状态机或隐式授权 |
| Tool / Capability | 一项带诚实副作用分类的真实动作 | 持久业务编排 |
| Runtime / Workflow | Run、Effect、审批、恢复、证据与交付 | 隐藏的领域推导 |

因此应用可以获得实用配方，而不会混淆知识与权限。Scenario 可以声明 email delivery
需要 `send_email` external-write Tool、审批、幂等和 reconciliation；选择 Scenario
本身不会创建 Tool，也不会让任何账号变得可用。

## 内置目录

17 个内置 Skill 覆盖第一批完整业务面：

| 领域 | Skills |
| --- | --- |
| 文件 | `workspace-files`、`document-processing`（受限 PDF 提取与文本/Office 转换 Tool） |
| 工程 | `coding-task`、`code-review`、`release-readiness` |
| 办公 | `email-drafting`、`business-report`、`meeting-notes`、`business-notice`、`proposal-writing`、`calendar-planning` |
| 个人写作 | `personal-letter`、`speech-writing`、`celebration-message` |
| Connector 方法 | `email-operations`、`cloud-drive-operations`、`ticket-triage` |

18 个内置 Scenario 把这些单元组成显式配方：

| 模式 | Scenarios | 执行边界 |
| --- | --- | --- |
| Draft | `email-draft`、`office-report`、`meeting-notes`、`business-notice`、`proposal-draft`、`calendar-planning`、`personal-letter`、`speech-draft`、`celebration-message` | 不需要 Tool；返回可复核文本 |
| Workspace | `file-management`、`document-processing`、`coding-change`、`code-review`、`release-readiness` | 受限 Workbench Tool 与 staged delivery |
| Connector | `email-delivery`、`calendar-update`、`cloud-drive-organization`、`ticket-triage` | 应用提供受限 Tool；外部写始终需要审批 |

系统不会自动选择任何内容，因此目录增长不会增加无关任务的 prompt、token 成本或模型
注意力负担。内置项按需加载，并在当前进程中缓存。

## 查看、选择与校验

查看目录不需要模型或账号：

```bash
lipas skill list
lipas skill show code-review
lipas scenario list
lipas scenario show email-delivery
lipas scenario check email-draft
```

`scenario show` 会展示生命周期、Skill bundle、精确 Tool 名称、必需输入字段、副作用分类、审批位置以及
幂等/reconciliation 要求。可以在执行前检查应用的 Tool factory：

```bash
lipas scenario check email-delivery \
  --factory connectors:email_tools \
  --json
```

检查只能证明所需 Tool 名称、输入字段和副作用声明一致，无法证明 provider 账号范围、收件人策略、
secret 处理、人工审批或 reconciliation 实现；connector Scenario 会把这些宿主义务
单独列出。

选择一个完整配方或组合多个配方：

```bash
lipas chat --scenario office-report --once "根据所给信息起草周报"

lipas task start . "修复 parser 并检查发布准备度" \
  --scenario coding-change \
  --scenario release-readiness
```

无 Tool 的内置 chat 只接受 Draft Scenario；对缺少能力的 Workspace/Connector Scenario
会在启动前失败。Task Workbench 也会先校验默认 Tool。自定义 chat/task factory 可以接收
`skills=`，并可选接收 `scenarios=`；额外 Tool 仍由 factory 明确组合。

工程 Task 的默认 Workbench Tool 还包括纯算术 `calculate`、受限 CSV 概览
`analyze_csv`，以及需要审批的 `python_exec`。Python 在临时 worker 中运行，具有时间、
内存、源码和输出上限，不会隐式获得项目文件。生产环境应选择 Bubblewrap 作为 OS
隔离边界；显式选择的 `local` 只是受信任的兼容模式，非隔离结果会记录到 evidence。

文档 workflow 还提供受限 ZIP/TAR `inspect_archive` 和需要审批的
`extract_archive` Tool。提取前会检查路径穿越、链接/设备成员、成员数量和展开后大小。
如果 PDF 是纯图片且没有可提取文字，结果会标记 `needs_ocr`；系统不会隐式启动 OCR，
宿主应在独立 sandbox 与数据出境 policy 下另行提供 OCR capability。

需要本地 RAG 的应用可以使用 `KnowledgeStore`，把已经获授权的文本写入可持久化、按
scope 过滤的 lexical index。检索结果带有来源、chunk 和文档 digest 引用。它只是普通
应用上下文，不是对话记忆，也不是 Claim authority；以后可以在同一边界后接 embedding/
vector provider。

Provider integration 可以使用 `fetch_url_tool(HttpClient(...))` 获得只读
`fetch_url` Tool。它复用 `HttpClient` 的 HTTPS/host allowlist、重定向、timeout 和响应
策略，再提取有大小上限的可见 HTML 或 UTF-8 文本并返回 SHA-256 digest。Tavily、Exa、
ArXiv 等搜索服务应实现为独立 adapter，返回来源 URL 与引用 evidence，而不是放宽这个
通用 fetch 边界。

## Python API

```python
from lipas import Agent, ScenarioRegistry

scenarios = ScenarioRegistry.from_names([
    "coding-change",
    "release-readiness",
])
skills = scenarios.skill_registry(
    paths=["skills/repository-conventions"],
)

agent = Agent.ollama(model="gemma4:12b", skills=skills)
```

构造应用 Agent 前可以校验 Tool 集：

```python
assessment = scenarios.require_compatible(application_tools)
```

`BusinessScenario`、`CapabilityRequirement`、`ScenarioAssessment` 与
`ScenarioRegistry` 都是不可变、provider-neutral 的公共值。Scenario 不依赖 ReAct，
自定义 behaviour 或外部 LangGraph/AutoGen 宿主也可通过普通 LIPAS action 边界使用。

## 外部 Connector 边界

内置 connector Scenario 是契约，不是 provider integration。生产级 external write
必须同时具备：

- 显式 provider account、tenant、recipient、folder、queue 与 object scope；
- 在 prompt 和持久证据之外解析 secret；
- delivery 前的完整 preview 与人工审批；
- 稳定的逻辑 operation idempotency key；
- 作为交付证据保存的 provider id；
- 数据出境和附件 policy；
- `uncertain` 状态，以及 provider reconciliation，而不是盲目 retry。

`ActionGateway`、`OperationJournal`、durable Run 和 Effect 提供 Runtime 部件。
Provider package 仍必须诚实实现并测试自己的 API、scope、幂等、查询与 reconciliation。

## 增加业务包

1. 任务只需要方法、结构、语气或检查时，增加 Skill。
2. 必须获得当前事实时，增加 read-only Tool。
3. 增加 write Tool 时，必须声明审批点和诚实的 effect class。
4. Provider 结果可能不确定时，增加 operation journal 与 reconciliation route。
5. 增加 Scenario，发布最小 Skill bundle、生命周期与 capability contract。
6. 完成 crash、redelivery、scope、secret 与 conformance 测试后，才能称 connector 已达生产级。

这样 LIPAS 可以从文件和 Coding 扩展到办公、个人与 provider 工作，而不必把每个业务
领域都变成新的 Agent class 或彼此竞争的 authority store。
