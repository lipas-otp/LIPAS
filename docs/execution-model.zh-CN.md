# LIPAS 执行模型

> 语言：[English](execution-model.md) | [中文](execution-model.zh-CN.md)

这是 LIPAS 的核心概念文档。编写第一个 Agent 时不需要先读它；请先看
[快速开始](getting-started.zh-CN.md)或[循序上手教程](tutorial.zh-CN.md)。当你
需要了解持久 trace、replay 或 Team 究竟提供什么保证时，再回到这里。

## 从应用需要开始

LIPAS 有三个执行概念：

| 概念 | 含义 | 何时引入 |
|---|---|---|
| `Agent` | 一个 assistant：模型、工具和 reason/act 循环 | 通常的起点 |
| `@tool` | 带已声明副作用类别的显式 Python capability | assistant 需要读取或改变某些内容 |
| `Team` | 在具名 assistant 或函数之间建立可持久化的 handoff 边界 | 工作需要独立 owner、重启边界或审计记录 |

一个 Agent 可以使用很多工具、进行很多次模型调用；仅此并不需要 Team。只有
下一段工作应作为独立归属的 handoff 存活下来时才添加 Team，例如 planner 将
研究任务交给可独立重启的 researcher，或付款需要独立的审批边界。

工具不是 Agent；它们是 Agent 明确的“手”。Team 成员通常是一个 Agent，但在
不需要模型时也可以是普通 async 函数。

Skill 是一个可选、可复用的 `SKILL.md` 指导文件。它会被载入 Agent 的
instructions，但不会创建 capability 或新的执行语义：Agent 仍然只能通过已
声明的工具行动。

## 一份记录，三个视图

每个与可靠性有关的事件都会成为一个 **Claim（声明）**：它是带 tag、fields、
source 和稳定 `claim_id` 的不可变记录。一个 store 对同一个逻辑 claim id 只
接纳一次；再次投递相同 payload 是 no-op，用同一 id 投递不同内容则会被拒绝。

一次 **fold（折叠）** 会追加该 Claim，并更新派生视图。这是 runtime 的中心
规则：在决定与 effect 尚未被遗忘前记录它们。merge strategy 必须是确定的；
使用 semilattice strategy 的字段也与顺序无关。history 则有意保持顺序。

标准 session 只有三行：

| Row | 回答的问题 | 职责 |
|---|---|---|
| History | 发生或决定了什么？ | observation、replay 选择、supervision、mailbox 与 operation transition |
| Capability | 这笔消耗可以发生吗？ | budget、资源消耗、quota 和 rate event |
| Effect | 原本想调用什么，最后发生了什么？ | 模型/工具 intent、result、rejection 与因果链接 |

这些 row 是同一条 tape 的投影，并非独立数据库或隐藏的 workflow state。只有当
一个 concern 拥有自己的 tag、确实需要独立 invariant 或 view 时，才应添加新
row。领域 memory、搜索索引和用户资料仍是普通应用数据，不是 LIPAS row。

## Effect：让实时边界可见

每次模型或工具调用都有如下生命周期：

```text
effect_intent  →  effect_result | effect_rejected
```

实时调用前会记录 intent；result 或 rejection 使结果明确。只有 intent 而没有
二者的记录称为 **orphan**：这是中断或结果未知的操作，必须调查，不能悄悄当成
成功。

guard 和 budget 在实时 effect 之前运行。拒绝也会记录为 intent 加上类型化
rejection。使用 `estimate=` 的工具必须先给出有限且非负的估计；估计无效或抛错
会导致 `estimate_invalid` rejection，绝不是绕过硬 budget 的理由。已接受的
intent 会快照提交的输入。`caused_by` 可将 Agent effect 链至其 Team message；
`compensates` 可将补偿 effect 链至较早 effect。

## Replay：复现决策，而不意外重复 effect

LLM replay 会替换为已记录的 reply。工具 replay 默认严格：已记录的工具结果会
被替换，任何实时工具都不会执行。`BEST_EFFORT` 可以执行缺失调用；
`LIVE_REROUTE` 则会拒绝 external write，除非调用者显式 opt in。

Replay 能证明使用了哪一个已记录决策；它**不能**证明原始外部操作恰好投递一次。

## 外部边界

`OperationJournal` 是支持 idempotency key 的 external write 边界。它在提交前
持久化调用方的 key，并记录 `prepared`、`uncertain`、`succeeded` 与 `failed`。
其 transition 可以链接到原始 effect。已知 terminal outcome 不可变：重复
reconciliation 只会返回该结果，不会允许过期信息改写它。

崩溃或 provider 返回含糊错误后，状态为 `uncertain`。LIPAS 会拒绝盲目再次提交；
应用代码必须与 provider reconciliation。只有 provider 遵守同一 key、并提供确定
结果的方法时，exactly-once 才可能实现。

如果 LIPAS 无法持久化记录 provider return（例如结果无法序列化），也会将仍在
等待的 submission 标为 `uncertain`。记录失败绝不是外部 write 没有发生的证据。

## Team：可靠 handoff，不是图 DSL

`Team` 在自己的持久 session 中记录 handoff、lease、acknowledgement、release 和
recovery。投递是 at-least-once：崩溃成员的 lease 可到期，同一稳定 message 可被
再次投递。接收者将 message id 当作 idempotency/replay key。acknowledgement 仅在
其 lease 仍有效时成立；已过期 worker 不能确认迟到的工作。

这有意比 distributed ownership 或 workflow graph 更小。如今每个 Agent 都保留
自己的 authority 和 budget；跨 Team budget sharing、capability delegation 与
mailbox replay 都是明确的应用工作。

## 当前的 streaming 边界在更底层

`LLMHarness.stream(...)` 可以产出规范化的 `Delta`、`ToolUseDelta` 和终止的
`Done` event，同时保存相同 effect record。一旦 event 对外可见，该 attempt 不会
重试：已经输出的内容无法收回。高层 `Agent` API 有意只返回最终 `FinalResult`，
目前尚不提供面向调用者的 token streaming。

## 有意不做的边界

LIPAS 不提供 graph DSL、托管 control plane、魔法般的长期 memory、全局分布式
transaction 或 provider 无关的 exactly-once delivery。它的职责更窄：使 Agent 的
决策、成本、effect、失败和恢复状态足够明确，能够安全地检查与 replay。

provider-neutral 的 `Request`、`Reply`、content、usage 和 stream-event shape 位于
`lipas.adapter`。Ollama、注入 client 的 Anthropic，以及 optional-SDK 的 OpenAI
Responses adapter 都实现这些 shape。
