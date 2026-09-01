# LIPAS 架构导览

> 语言：[English](architecture.md) | [中文](architecture.zh-CN.md)

这是一张代码库的短地图：说明一次请求如何流转，以及每类状态由哪个组件负责。
精确的恢复和 replay 保证请继续阅读[执行模型](execution-model.zh-CN.md)。

## 请求路径

```text
用户 / CLI / Web / Python 宿主
             │
             ▼
Conversation 或直接 prompt        （可选产品入口）
             │
             ▼
Agent ── adapter + instructions + Skill + Tool
             │
             ▼
准入：guard → budget → 副作用策略 → approval
             │
             ▼
Effect intent（实时动作前先持久化）
             │
             ├── model adapter
             ├── Tool / Harness
             ├── sandbox / workspace capability
             └── connector / 外部 provider
             │
             ▼
Observation 或带类型的 rejection
             │
             ▼
Artifact / report / replay / delivery
```

运行时会在调用模型或 Tool 前记录 intent。只有 intent 而没有 observation 的记录是
orphan，必须保持可恢复/不确定，绝不能默认为成功。Skill 只改变 instructions；Tool
才是可执行 capability，`side_effect` 声明参与准入策略。

## 模块职责

| 层 | 主要模块 | 负责内容 |
| --- | --- | --- |
| 入口 | `cli.py`、`conversation.py`、`operator.py` | 命令、聊天和本地 Web projection |
| Agent loop | `agent.py`、`behaviour.py`、`react.py`、`adapter/` | reason/act/observe 与 provider-neutral 消息 |
| 准入与 evidence | `tools.py`、`guard.py`、`effect.py`、`harness.py`、`tool_harness.py` | 副作用、preflight、intent/result claim |
| 持久控制 | `execution.py`、`durable.py`、`dispatcher.py` | Task/Run lease、checkpoint、取消、Interrupt |
| 产品工作台 | `workbench.py`、`workspace_storage.py` | workspace 策略、ChangeSet、Artifact、Verification、Report |
| 外部边界 | `operations.py`、`http_client.py`、`email.py`、`gateway.py` | 幂等、不确定结果与 reconciliation |
| 协作 | `coordination.py`、`coordination_policy.py`、`orchestration.py` | owner、handoff、受限并发与 legacy mailbox |
| 领域指导 | `skills.py`、`scenarios.py`、`builtin_skills/` | 可移植 instructions 与 capability 要求 |
| 持久化 | `serialization/`、`sqlite_storage.py`、`conversation_store.py` | Claim tape、projection、conversation 与 SQLite 策略 |
| 有界 capability | `document_tools.py`、`code_tools.py`、`archive_tools.py`、`web_tools.py`、`knowledge.py` | 解析、计算、检索；不拥有 authority 或 approval |

底层 capability 模块保持小型、可选依赖，并只接受已经获得授权的输入。`Workbench`
统一提供路径策略、staging、approval 和 evidence，因此解析器与计算器不各自复制这些逻辑。

CLI 有意提供两套不同的 Tool bundle：`chat --workspace` 仅用于轻量只读对话；`task` 才取得
包含写入、验证、staging 和 artifact 的完整 Workbench bundle。显式保留这条差异，避免聊天
prompt 意外获得任务 authority。

## 哪个存储是权威？

| 状态 | 权威 | Claim tape 的作用 |
| --- | --- | --- |
| 模型/Tool intent、result、spend、replay | Agent session（`Claim`/`Effect`） | 持久 evidence 与 projection |
| Task、Run、lease、checkpoint、approval | `ExecutionStore` | 附着 RowSet 时输出可修复的 audit mirror |
| 外部写入状态 | `OperationJournal` | 记录因果 evidence 与 reconciliation 历史 |
| workspace 文件与 staged delivery | 文件系统 + `Workbench` 表 | Artifact/Report 指向精确路径与摘要 |
| Conversation/message identity | `SessionStore` / `conversation_store.py` | 用户可见历史与 promotion event |

Integration 不应建立第二套 Task/Run authority。LangGraph、AutoGen、MCP bridge 应转换为同一套
Agent、Tool、Effect 或 coordinator 契约。legacy `Team` 只为已有 mailbox 应用保留。

## 如何选择入口

1. 一个连贯目标使用 `Agent`，即使它会调用多个 Tool。
2. 需要 replay 或 budget 时加持久 session。
3. 同一次运行要跨 approval、取消或进程丢失恢复时，加 `ExecutionStore` 与 `run_durable()`。
4. 处理本地文件、staged 修改、验证和报告时使用 `Workbench`。
5. 外部写入具备幂等 key 和 provider 查询接口时使用 `OperationJournal`。
6. 只有需要独立 owner/恢复边界时才使用 `AgentCoordinator`；`Team` 仅用于 legacy mailbox。

[示例课程](../examples/README.zh-CN.md)也按这个顺序组织，从单 Agent 到本地任务和 connector。
