# 统一 runtime 契约

> 语言：[English](runtime-contracts.md) | [中文](runtime-contracts.zh-CN.md)

LIPAS 向应用提供一个 composition root、一套公共生命周期词汇和一个有版本的全局数据库。
schema v2 把兼容的控制、产品、operation、handoff、conversation 与 evidence 表物理合库，
同时保持每个 Run 的 Claim/Effect tape 独立。

```python
from lipas import LIPASRuntime

with LIPASRuntime.open(".lipas") as runtime:
    runtime.execution   # Task / Run 的权威状态机
    runtime.claims      # 审计证据与 projection
    runtime.operations  # 幂等外部操作边界
    runtime.handoffs    # 可选的 at-least-once 协调边界
    runtime.sessions    # 乐观并发 conversation snapshot
    runtime.artifacts   # 产品 artifact repository
```

`ExecutionStore` 仍是持久 Task/Run/Interrupt 控制状态的唯一权威来源。
`workspace.db` 是 composition root 打开的唯一全局产品数据库；调用方不再自行拼装、
选择路径或关闭一组松散 store。Run evidence 继续位于 `runs/<run-id>/claims.db`，从而
保持 budget、replay 与 single-writer 隔离，而不会形成另一套 Run 状态机。

## 存储迁移与诊断

打开 legacy 工作区绝不会隐式改写它：

```bash
lipas migrate plan --home .lipas
lipas migrate apply --home .lipas --yes
lipas migrate verify --home .lipas
lipas doctor --home .lipas
lipas audit --home .lipas
```

迁移先生成 SQLite 一致性备份，再装配临时目标库，核对源/目标行数、SQLite integrity、
外键、event cursor、interrupt 状态与 evidence 路径 containment，最后原子启用
`workspace.db`。原始 v1 文件保持不变。`rollback --yes` 会先把 v2 数据库完整保存到备份，
再回到保留的 v1 文件；它不会假装 v2-only write 可以表达在 v1 中。Runtime 实例持有共享
workspace lease，migration/rollback 必须取得独占 lease，因此会拒绝活跃 worker、Runtime
或 SQLite writer。rollback 会先 checkpoint WAL 并验证备份，再停用 schema v2。死亡 PID
留下的 migration lock 会被诊断并安全恢复，活跃 lock 则绝不删除。

`doctor` 会实际执行有界的默认 OS sandbox 启动探测，并分别报告 storage health 与完整
runtime readiness；仅在 `PATH` 中发现可执行文件不会被当成隔离能力已经可用。

## 一套 invocation 契约

普通调用、对话 turn 与 durable Run 现在共享以下概念：

- `RunContext`：稳定 run id、协作式 cancellation token 和可选的绝对 monotonic
  deadline。deadline 跨越所有模型与工具阶段，不会在每个阶段重新计时。
  `current_run_context()` 让工具读取宿主上下文，而不增加模型可见的 schema 参数；
  `asyncio.to_thread` 中的同步工具同样可见。
- `AgentEvent`：有序、provider-neutral 的 run/model/tool 事件。`Agent.stream`、
  `Session` 与 `RunHandle` 使用同一协议。durable 事件由 `ExecutionStore` 持久化并支持
  cursor 之后的 catch-up。
- `Session`：显式对话状态；`SQLiteSessionStore` 使用乐观版本检查保存命名快照。
- `RunHandle`：一次正在运行的 Session 调用，提供 `result()`、`events()` 与协作式
  `cancel()`。

durable 重连时，把最后确认的 `event_cursor=` 与 `event_sink=` 传给
`run_durable`/`resume_durable`。持久记录是权威来源；event sink 断开不会改变 Run 结果。
`LIPASRuntime.run_durable()`/`resume_durable()` 会串行执行 convenience call，因为一个
composition-root Workbench 只有一个可变 audit attachment。并发 worker 像内置 dispatcher
一样，在同一权威数据库上使用独立 Workbench view。

## Input 不是 Approval

`InputPolicy` 与 `ApprovalPolicy` 都能暂停 durable Run，但回答不同问题。input interrupt
补充缺失信息，其 response 只成为当前一个工具结果，工具函数不会执行。approval 只允许
当前一个待执行 capability call。解决 input 绝不会授权当前或后续 write。

## 诚实的模型能力

`ModelCapabilities` 用 `None` 表示未知；`ModelRequirements` 把指定能力变成显式启动
检查；`ModelCapabilityReport` 解释每项不匹配。当前 Anthropic 与 Ollama adapter 实际是
single-shot，因此如实标记 `streaming=False`，即使 provider 的其他集成支持 streaming。
校验过程不会静默换模型，也不会偷偷降级所需能力。

generic Chat Completions adapter 使用 `openai-compatible`（单个 terminal response）
和 `openai-compatible-stream`（真实 SSE）两个 provider name。只有当前配置的 streaming
mode 会被声明为 true/false；tool calling、structured output、reasoning、context length
与 locality 在应用登记测试过的精确 provider/model route 前保持 unknown。vision 明确为
false，因为当前 adapter 只接收 text/tool message block。

## Observer 边界

`RunObserver` 接收冻结的 `RunSnapshot` 与 `RunContext`，可以返回
`Recommendation`。recommendation 会作为证据记录并发出事件，但默认只有建议性。
只有宿主明确设置 `honor_observer_recommendations=True` 时，ReAct behaviour 才会把
`terminate`/`escalate` 建议映射为 terminal result。原有 Supervisor policy 保持兼容，
应用可以逐步迁移出 ReAct 专用 supervision。

## 权威边界

- Skill 是指导文本，不是 capability；
- 对话状态和未来 memory 是上下文，不是 replay 或 approval authority；
- Claim/Effect 是审计证据；
- Tool 是唯一可执行 capability；
- legacy `Team`/`Mailbox` 仍可作为兼容 orchestration 层使用，但不再被视为核心 Run 的
  第二套身份；
- `StrategyRegistry` 与 belief-adaptive calculus 继续服务高级/实验 projection；核心
  Run、Interrupt、event 与 operation 控制使用固定 reducer 和显式状态机。

`lipas audit` 默认只读并检查 storage invariant；JSON 会明确把 Claim lint 标记为
`not_run`，不会用空列表伪装成检查已经完成。`LIPASRuntime.audit(repair=True)` 与
`lipas audit --repair` 会修复可恢复的 audit outbox 并运行持久 Claim lint，但绝不会
遗漏全局 evidence 或任何已登记的 Run tape，也绝不会虚构缺失的外部操作结果。
