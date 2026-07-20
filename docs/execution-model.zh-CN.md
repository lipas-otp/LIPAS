# LIPAS 执行模型

> 语言：[English](execution-model.md) | [中文](execution-model.zh-CN.md)

这是 LIPAS 的核心概念文档。编写第一个 Agent 时不需要先读它；请先看
[README](../README.zh-CN.md)或[循序上手教程](tutorial.zh-CN.md)。当你
需要了解持久 trace、replay 或 Team 究竟提供什么保证时，再回到这里。

## 从应用需要开始

LIPAS 有四个执行概念：

| 概念 | 含义 | 何时引入 |
|---|---|---|
| `Agent` | 一个 assistant：模型、工具和 reason/act 循环 | 通常的起点 |
| `@tool` | 带已声明副作用类别的显式 Python capability | assistant 需要读取或改变某些内容 |
| `ExecutionStore` | 持久 Task/Run 归属、checkpoint、取消与审批 Interrupt | 同一个 Agent run 必须跨越等待或进程中止 |
| `Team` | 在具名 assistant 或函数之间建立可持久化的 handoff 边界 | 工作需要独立 owner、重启边界或审计记录 |

一个 Agent 可以使用很多工具、进行很多次模型调用；仅此并不需要 Team。同一个
Agent run 需要恢复时添加 `ExecutionStore`；只有下一段工作应作为独立归属的
handoff 存活下来时才添加 Team，例如 planner 将
研究任务交给可独立重启的 researcher，或付款需要独立的审批边界。

工具不是 Agent；它们是 Agent 明确的“手”。Team 成员通常是一个 Agent，但在
不需要模型时也可以是普通 async 函数。

Skill 是一个可选、可复用的 `SKILL.md` 指导文件。它会被载入 Agent 的
instructions，但不会创建 capability 或新的执行语义：Agent 仍然只能通过已
声明的工具行动。

## 一条证据 tape，显式的控制 store

每个与可靠性有关的模型、工具、budget、replay 与 supervision event 都会成为一个
带 tag、fields、source 和稳定 `claim_id` 的 **Claim（声明）**。store 接纳 Claim
后拥有的是不可变快照：再次投递相同逻辑 id 与 payload 是 no-op，用同一 id 投递
不同内容会被拒绝，调用方之后的修改也无法重写 tape。用于准备 event 的 Python
`Claim` 对象本身并不是 frozen value。

一次 **fold（折叠）** 会追加该 Claim，并更新派生视图。这是 runtime 的中心
规则：在决定与 effect 尚未被遗忘前记录它们。merge strategy 必须是确定的；
使用 semilattice strategy 的字段也与顺序无关。history 则有意保持顺序。

标准 session 只有三行：

| Row | 回答的问题 | 职责 |
|---|---|---|
| History | 发生或决定了什么？ | observation、replay 选择、supervision、execution、mailbox 与 operation transition |
| Capability | 这笔消耗可以发生吗？ | budget、资源消耗、quota 和 rate event |
| Effect | 原本想调用什么，最后发生了什么？ | 模型/工具 intent、result、rejection 与因果链接 |

这些 row 是同一条 tape 的投影，并非独立数据库或隐藏的 workflow state。只有当
一个 concern 拥有自己的 tag、确实需要独立 invariant 或 view 时，才应添加新
row。领域 memory、搜索索引和用户资料仍是普通应用数据，不是 LIPAS row。

可变协调状态承担不同工作，并有明确的权威来源：

| 状态 | 权威 store | Claim 的角色 |
|---|---|---|
| 模型/工具 Effect、消耗、replay、supervision | Agent Claim/Effect session | 权威证据 |
| Task、Run、lease、checkpoint、Interrupt | `ExecutionStore` | 连接 `RowSet` 时形成可修复 transition 镜像 |
| external write reconciliation | `OperationJournal` | 可修复 transition 镜像 |
| Team 投递与 acknowledgement | mailbox SQLite database | 可修复 transition 镜像 |

lease 与 compare-and-swap checkpoint 不会被强行塞进 Claim merge。它们的权威
SQLite transition 会在同一 transaction 中追加 Claim-shaped 本地 outbox event。
进程中止后镜像可能暂时落后，但 `repair_audit()` 会用稳定 Claim id 恢复每个 event。
这样既保持控制状态精确，也维持统一的证据词汇。

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

当多个工具调用具有相同名称和参数时，target 会按 fold 顺序依次消费匹配的 source
recording。因此，变化中的 read-only observation 不会在每一次 replay 时都错误地复用
第一条匹配结果。

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

## 持久 ReAct run

普通 `Agent.run()` 在内存中持有 reason/act/observe 循环，同时由 session 记录
Effect。`Agent.run_durable()` 还会把这个循环接入 `ExecutionStore`：execution store
负责 Task/Run 状态、带 fencing 的 run lease、带版本的阶段 checkpoint 和审批
Interrupt；Agent 的 SQLite session 仍是模型与工具 Effect 的事实来源。
checkpoint 会记录该 session 的稳定 `store_id`；若恢复时误用另一份 claim 数据库，
runner 会在任何实时模型或工具调用之前 fail closed。
所有权威 SQLite control store 都带显式 schema version：`ExecutionStore`、
`OperationJournal` 与 Team mailbox 遇到不兼容 release 时都会 fail closed。
使用 `rowset=...` 构造 `ExecutionStore` 时，每个 Task/Run/checkpoint/Interrupt
transition 还会从其 transactional outbox 镜像至该 Claim tape；控制决策仍以
execution database 为准。

持久循环会在每次模型调用前后、每个串行工具结果或安全并行 batch 完成后、每次
observation 完成后，以及 terminal settlement 之前保存 checkpoint。模型与工具 Effect 使用 run 范围内的
确定性 identity。如果进程在 terminal Effect 已记录、对应 checkpoint 尚未写入时
中止，下一位 lease owner 会从 Effect tape 恢复结果；如果只有 intent，则恢复会抛出
`OrphanedEffectError`，不会猜测再次提交是否安全。

同一模型 reply 可以请求多个彼此独立的工具。最多
`Agent(max_parallel_tools=4)` 个连续 `pure`/`read_only` 调用可以并发执行；返回给模型的
result block 仍保持原顺序，每个调用也保留各自确定性的 Effect identity。因此，结果已经
完成、batch checkpoint 尚未写入时发生崩溃，恢复不会重复实时执行。write 始终串行；
启用硬 budget、guard、tool replay cursor 或自定义 argument/result hook 时也保持串行，
因为并发 preflight 可能依据过期的 policy/accounting state 错误放行。只有 intent 的
在途调用仍是 orphan；并行不会
削弱这条失败关闭规则。

审批 policy 可以在工具执行前以原子方式保存 checkpoint，并把 Run 转为 `waiting`。
使用 `allow=True` 解决 Interrupt 后，Run 会重新变为可 claim；
`Agent.resume_durable()` 从 checkpoint 恢复，不会把原始 prompt 追加两次。持久执行
目前要求 Agent 使用 SQLite session。协作式 cancellation 会写入 checkpoint；带取消
请求且已经过期的 lease 可以被重新 claim，但只能用于完成取消。durable run 中的
supervisor tick 使用稳定 claim identity，因此 recommendation 已写入、checkpoint 尚未
保存时发生崩溃，也能在恢复时修复而不重复 recommendation。开发主线已加入自动
lease heartbeat 和模型/工具阶段 timeout；同步工具移出 event loop，取消时因无法证明
线程已经停止而保留 orphan。更广泛的 timeout recovery policy 仍属于后续工作。恢复
契约还通过真实 subprocess 测试验证：已完成
write Effect、尚未写入其 checkpoint 时发送 `SIGKILL`，重启会恢复 Effect result，
不会第二次执行 write。

## 持久本地 Task 调度

`TaskDispatcher` 把 pending Run 变成有并发上限的本地 worker 队列，但不创建第二份
scheduler 数据库。发现 candidate 只是提示；带条件的 `ExecutionStore.claim_run()`
transition 才是原子所有权边界，因此两个 worker 不能执行同一 active lease。pending Run
按 FIFO 调度；重启后可以领取过期 running lease，包括只用于完成取消的恢复路径。

多个 Task 可以并发，但每个 Run 都拥有独立 SQLite Claim/Effect session。全局 execution
database 负责 Task/Run/lease 状态；每 Run session 负责模型/工具证据和 budget。这样不会
让无关 Task 共享 single-writer Claim seq 或同一 budget projection。旧布局产生、已经绑定
checkpoint 的 Run 会继续使用原来的 session。

Run 因审批进入 `waiting` 时会释放 lease 和 worker 槽位；使用 `allow=True` 解决 Interrupt
后，它回到 `pending`，可由 worker 重新领取并恢复。`lipas task submit` 持久化任务，
`lipas task worker` 持续调度，`--max-concurrency` 限制同时执行的 Task 数。停止 worker
会取消其本地 heartbeat；尚未结算的 lease 必须过期后才能被另一 worker 领取。

## Staged ChangeSet 与交付

第一方 CLI Task 会获得每 Run 独立 staging workspace。Agent 在其中读取、写入和运行验证；
Run 执行期间，用户选择的 source workspace 保持不变。staged write 仍保留正常的
idempotent Effect 分类，但因为它被限制在产品状态内部，不再逐项要求人工审批。command
仍保留审批与 OS 隔离边界。

Run 完成后，workbench 将 stage 与 baseline 比较，把 ChangeSet 标记为 `ready`。报告和
`lipas task diff` 展示完整文件变更；`lipas task apply` 是显式交付决定，并且只允许用于
已完成 Run。修改任何 destination 之前，它会验证每个 changed path 仍等于记录的 baseline，
或已经等于 staged desired hash；任何无关漂移都会失败关闭。

每个文件替换都是原子的。多文件 apply 不是一个 filesystem transaction，但可以在重启后
继续：已经等于 desired hash 的 path 被视为已完成，其余仍处于 baseline 的 path 继续应用。
discarded stage 不可 apply，applied stage 不可 discard。apply/discard transition 会持久化为
产品事件，并更新报告 delivery 状态。

首版 snapshot backend 对 Git workspace 复制 tracked/未 ignore 文件，对非 Git workspace
复制普通文件；在持久化前排除疑似 secret 路径/文本、symlink、超大文件和常见生成 cache，
并执行总文件数/字节上限。它是
受限安全 backend，尚不是 copy-on-write filesystem 或 Git worktree transaction。

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

`OperationJournal` 与 Team mailbox 的 SQLite 状态是 authoritative durable state；
但它们可选的 Claim audit 使用另一笔 transaction。每次 authoritative mutation 都会
在同一个 SQLite transaction 中追加一条 Claim-shaped outbox event；构造、幂等重试及
`repair_audit()` 会用稳定的 Claim id 重放 outbox。因此进程在两个数据库写入之间中止
只会让 audit 暂时落后，不会永久漏掉或重复镜像事件。这仍是可恢复镜像，不是分布式
transaction；调用方仍必须依据 journal/mailbox 数据库判断 operation 或 handoff 是否
存在，不能只依据 Claim。
