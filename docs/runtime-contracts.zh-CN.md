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
    runtime.handoffs    # legacy mailbox 兼容边界
    runtime.sessions    # 乐观并发 conversation snapshot
    runtime.artifacts   # 产品 artifact repository
    coordinator = runtime.coordinator()  # ExecutionStore-backed handoff
```

`ExecutionStore` 仍是持久 Task/Run/Interrupt 控制状态的唯一权威来源。
`workspace.db` 是 composition root 打开的唯一全局产品数据库；调用方不再自行拼装、
选择路径或关闭一组松散 store。Run evidence 继续位于 `runs/<run-id>/claims.db`，从而
保持 budget、replay 与 writer 热点隔离，而不会形成另一套 Run 状态机。所有常规连接
共享 0.63 的 SQLite WAL、超时、事务和错误策略。Claim tape 可以协调并发 connection，
但仍诚实地受限于 SQLite 的单物理 writer；详见
[SQLite 存储与并发](sqlite-storage.zh-CN.md)。

## 存储迁移与诊断

打开 legacy 工作区绝不会隐式改写它：

```bash
lipas install --home .lipas
lipas release check --home .lipas
lipas upgrade --home .lipas
lipas backup --home .lipas --destination /safe/lipas.db
lipas restore --home .lipas --source /safe/lipas.db --yes
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

installation manifest 只保存非敏感元数据。POSIX 下 workspace、database、runs、manifest
和本地 secret 文件会做权限加固；如果 group/other 可访问，installation 不会通过 readiness。
使用 `FileSecretResolver` 原子轮换 `secret://file/NAME`，使用 `TLSConfig` 为 TLS 1.2+
的 operator/worker endpoint 提供证书；非 loopback server 必须同时启用 TLS 和认证。

## 一套 invocation 契约

普通调用、对话 turn 与 durable Run 现在共享以下概念：

- `RunContext`：稳定 run id、协作式 cancellation token 和可选的绝对 monotonic
  deadline。deadline 跨越所有模型与工具阶段，不会在每个阶段重新计时。
  `current_run_context()` 让工具读取宿主上下文，而不增加模型可见的 schema 参数；
  有界且传播 context 的同步 Tool executor 中同样可见。
- `AgentEvent`：有序、provider-neutral 的 run/model/tool 事件。`Agent.stream`、
  `Session` 与 `RunHandle` 使用同一协议。durable 事件由 `ExecutionStore` 持久化并支持
  cursor 之后的 catch-up。
- `Session`：显式对话状态；`SQLiteSessionStore` 使用乐观版本检查保存命名快照。
- `RunHandle`：一次正在运行的 Session 调用，提供 `result()`、`events()` 与协作式
  `cancel()`。

`LIPASRuntime.execute_workflow()` 把 `CompiledWorkflow` 作为一个带 lease 的 Task/Run 执行，
并记录步骤生命周期事件。callback 由宿主负责；任何改变世界的动作仍必须经过标准
`EffectProposal`/Harness bridge，workflow execution 不能形成第二套 authority。宿主可以传入协作式
`cancel_check`；取消会返回独立的 `cancelled` workflow 结果，并把底层 Run 持久化结算为
`CANCELLED`，不会被误报为普通 callback 失败。

### 0.41 契约：conversation kernel

Conversation kernel 在同一个 store 增加显式的 `Conversation`、`Message` 与
`ConversationEvent`。Message 有稳定 identity；`LIPASRuntime.promote_message_to_task()` 根据
它得出唯一的确定性 Task/Run link，重复调用不会创建第二次执行。Conversation event 使用独立
sequence 和 catch-up cursor，控制 transition 仍由 ExecutionStore 权威负责。

### 0.42 契约：local Web operator

Local Web operator 在 `/api/conversations` 下投影 0.41 资源，支持 cursor-based SSE catch-up、
认证 stream/mutation 和有界 content-addressed attachment。Execution AgentEvent 与 Interrupt
按 event identity 投影，持久 event log 仍是 replay authority。

### 0.43 契约：remote Worker

`RemoteWorkerRunner` 与 HTTPS-gated `RemoteWorkerHTTPClient`/
`RemoteWorkerHTTPServer` 在 `ExecutionStore` 之上增加 capability 声明、HMAC attestation、
lease heartbeat、attempt fencing 和显式 complete/fail transition，不创建第二个 queue。
TLS 与证书/key custody 仍由部署负责。

### 0.44 契约：shared workspace policy

`WorkspaceIdentity` 与 `ApprovalDelegation` 是宿主 policy 值。policy store 可持久化、可撤销，
但不会自行 resolve Interrupt，也不会在宿主 Run 之外授予 authority。

### 0.45 契约：connector recovery

Connector descriptor、provider request identity 与 `RateLimitPolicy` 让 egress capability 和本地
限流显式化。HTTP、MCP、Email 写操作使用 OperationJournal 的 timeout → uncertain → reconcile
契约；policy 不能替代 reconciliation。

### 0.46 契约：Plan/Handoff boundary

`AgentPlan` 与 `PlanStep` 是 plan/handoff envelope。外部 graph/team state 留在 LIPAS 外部，
以一个受限 Run 托管，不创建第二个 scheduler。

### 0.47 契约：observability projection

`measure_execution()` 是从现有 store 派生的有界 metrics/SLO projection，提供 cost、latency、
replay 与 failure evidence，但不让 metrics database 成为 authority。

### 0.48 契约：signed extension registry

`ExtensionSigner` 与 `ExtensionRegistryService` 验证并发布 provenance/certification metadata，
不 import 第三方代码。package 安装和 tenancy 仍由宿主显式负责。

`AgentCoordinator` 把同一契约扩展到具名成员。每个 handoff 都是确定性的
ExecutionStore Run，具备 branch `RunContext`、lease heartbeat、持久 cancellation、
terminal replay 与 handoff 生命周期事件。成员 registry 与 policy 是应用组合，不是另一套
durable 状态机。详见[多 Agent 协调](multi-agent.zh-CN.md)。

`coordinator.event_handle(coordination_id)` 提供有界、可重连的聚合 page，合并各 Run
的 `AgentEvent` 流。`SharedBudgetPolicy` 在 handoff claim 前以原子事务预留共享硬预算，
`CapabilityPolicy` 在成员注册时检查 host 声明的 capability；二者都不会让 Skill 或
Memory 获得 authority。

0.43 worker 可以返回 `RemoteExecutionResult`：事件 identity 属于逻辑 action 而不是某次
 worker attempt，换 worker redelivery 仍然幂等；checkpoint 使用当前 lease 和版本保存，
 remote `EffectObservation` 在 Run 进入终态前写入 durable event。Reference transport 会验证
 worker attestation；worker、task 不匹配或 lease 过期时会在调用前拒绝。

共享 workspace 使用 `WorkspacePolicyStore` 在同一 SQLite authority 中保存不可变 identity、
有界 `ApprovalDelegation`、撤销和 policy audit；它不会自行 resolve Interrupt。Connector
 descriptor 与 `RateLimitPolicy` 让 egress capability 和本地限流显式化，但不替代
 `OperationJournal` reconciliation。

`ExternalRunEnvelope`/`AgentCoordinator.execute_external()` 是 LangGraph、AutoGen 或其他
 workflow host 的边界：创建一个确定性的 LIPAS Task/Run，传播 RunContext deadline/
 cancellation，续租并记录终态证据。外部 graph/team state 留在 LIPAS 外部，不能创建平行
 scheduler。

0.40 operator 继续保持这条边界：`LocalWebOperator` 只是同一组 Task、Run、Interrupt 与
event cursor 的本地 HTTP projection。它默认绑定 loopback，绝不返回 lease token，mutation
必须携带显式 bearer token（配置时 stream 也一样）。由 Runtime 创建的 operator 还可以投影有界 Workbench Task detail
（product event、artifact、ChangeSet diff 状态与 report），但这些仍是 Workbench projection，
不是第二个 authority。`FaultPlan`/`FaultCampaign` 与 `benchmark_execution_store()` 只是
有界故障/测量 helper。root browser page 使用 SSE catch-up 并在需要时回退 polling，
`/api/runs/<id>/events` 仍是同一 cursor 契约上的薄 projection；`run_fault_matrix()` 也不创建
queue、metrics database 或 retry policy。

durable 重连时，把最后确认的 `event_cursor=` 与 `event_sink=` 传给
`run_durable`/`resume_durable`。持久记录是权威来源；event sink 断开不会改变 Run 结果。
`LIPASRuntime.run_durable()`/`resume_durable()` 会创建 Run-scoped ExecutionStore
evidence attachment。composition-root Workbench 的 control store 始终稳定，因此互不相关的
durable call 可以并发运行，不会关闭或重定向彼此的 audit sink；SQLite 仍会串行提交这些
很短的控制事务。

直接嵌入 Workbench 时使用同一 ownership 边界：

```python
with workbench.execution_scope(agent.rowset, run_id=run.id) as execution:
    result = await agent.run_durable(
        task.goal, execution_store=execution, run_id=run.id,
    )
```

scope 只拥有并关闭自己的临时 connection，绝不会替换 `workbench.execution`。

## 0.49 契约：backup 与 restore

Workspace backup 在 workspace lease 保护下使用 copy-on-write SQLite snapshot，并在接受前
做 integrity check；restore 不能绕过同一 lease 或 schema-version 检查。Installer UX、兼容
策略和 rollback 演练属于发布证据，不会创建新的 authority。

## 0.50 Runtime Semantics façade

0.50 的公共 façade 是 `AgentRuntime`，它只是 `LIPASRuntime` 面向产品的一层薄命名。
`decide_effect()` 会根据宿主声明的 capability、剩余 budget 与 approval 状态评估一个
`EffectProposal`。`execute_effect()` 会把该 decision 传给匹配的现有 Harness，并从持久
Claim tape 投影返回 `EffectObservation`。Harness 仍是唯一执行 LLM/Tool call 的组件，且会
把 proposal identity/provenance 写入具体的 Effect intent。重复 proposal identity 会恢复
terminal result，不会再次调用 provider/tool；只有 intent 的 Effect 会保持 `uncertain`，
外部写操作必须先进入 reconciliation 流程，才能决定是否重试。Proposal metadata 会被
放入独立 namespace，`caused_by` 作为显式因果链接保留；reconciliation 可以接受产品
proposal id 或映射后的 claim id。
如果复用 identity 时修改 actor、risk、capability、因果或 metadata 字段，Runtime 会 fail
closed，而不会静默返回旧结果。

当已知 Run 时，产品路径应使用 `LIPASRuntime.execute_effect_for_run()`：它将 Harness 配置
克隆到该 Run 的隔离 durable Claim tape，避免把方便的内存 store 误当成 Runtime 的权威证据。
低层 `execute_effect()` 为兼容性保留，并明确由调用者负责 Harness evidence sink。

同一规则也适用于 direct Gateway：pending approval 会绑定工具、参数摘要和 causation。
HTTP connector 会明确区分 provider request identity 与 operation idempotency key；write redirect
保持为 `uncertain`，必须 reconciliation。`SLOReport` 对没有 terminal sample 的空窗口报告为
不健康，不把缺失证据当成成功。`run_design_partner_validation()` 将 local fixture 与外部
adapter 统一为包含 run identity、unsafe delivery、reconciliation time、operator acceptance
和 failure category 的证据；本地报告标记为 `local_fixture`，不能满足外部 partner 闸门。

## 0.51 有界 workflow compiler

`AutonomousWorkflowCompiler` 将 `WorkflowGoal`（目标、严格 JSON constraints、workspace
与 adaptive step 上限）及宿主声明的步骤编译为确定性的 `CompiledWorkflow`。每个步骤明确
标记为 `fixed` 或 `adaptive`；goal constraints 会复制到每个编译步骤及 handoff metadata，
确保下游收到同一不可变规划边界，同时不会获得额外 authority。adaptive 步骤不得超过上限，
依赖环会 fail closed。编译不创建 Task、Run、Effect、Tool claim 或 approval；
`LIPASRuntime.compile_workflow()` 仅在未指定时提供 Runtime workspace 默认值。

`run_provider_workflow(..., live=True)` 是针对真实 provider 的显式生产探针：创建一个确定性的
durable Task/Run，并返回有界的 provider/model/terminal evidence；`live` 防止意外发出可能计费的请求。
`run_execution_soak()` 与 `lipas soak` 则按限定次数/时间反复验证本地 transition，并将 invariant
失败与 provider 可用性分开报告。
Provider evidence 还提供面向运维的 `outcome` 分类（`succeeded`、`provider_error`、`uncertain`、
`cancelled` 或 `non_success`）；若 adapter 暴露 model-completed event，则从 durable event 聚合 usage。
该 projection 不持久化 prompt 或未脱敏的 provider 原始诊断。

## 0.63 生产化契约

0.63 的部署层保持同一套 authority 边界，同时让单工作区路径可运维：`install`/`upgrade` 维护受限
manifest，backup bundle 包含可验证的 workspace 与 per-Run evidence，`verify-bundle` 可以在不修改目标的
情况下检查它们；`lipas soak` 提供有界本地 durability 证据。`TLSConfig` 支持 TLS 1.2+，以及 Operator/
remote Worker endpoint 的证书或 trust context 热轮换；`ManagedSecretResolver` 是接入外部 KMS/HSM 或
secret manager 的边界，本身不声称已经完成密钥托管。真实 provider 运行与外部 partner signoff 仍属于部署
证据，不能由本地测试或 fixture 自动推导。

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
- `AgentCoordinator` 在现有 `ExecutionStore` 下组合确定性 handoff Run，不拥有 mailbox
  或 graph 权威；
- legacy `Team`/`Mailbox` 仍可作为兼容 orchestration 层使用，但不再被视为核心 Run 的
  第二套身份；
- `StrategyRegistry` 与 belief-adaptive calculus 继续服务高级/实验 projection；核心
  Run、Interrupt、event 与 operation 控制使用固定 reducer 和显式状态机。

`lipas audit` 默认只读并检查 storage invariant；JSON 会明确把 Claim lint 标记为
`not_run`，不会用空列表伪装成检查已经完成。`LIPASRuntime.audit(repair=True)` 与
`lipas audit --repair` 会修复可恢复的 audit outbox 并运行持久 Claim lint，但绝不会
遗漏全局 evidence 或任何已登记的 Run tape，也绝不会虚构缺失的外部操作结果。
