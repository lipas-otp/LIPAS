# 多 Agent 协调

> 语言：[English](multi-agent.md) | [中文](multi-agent.zh-CN.md)
>
> 状态：基于 ExecutionStore 的协调标准库

LIPAS 在不增加 Team 数据库或第二套 workflow 状态机的前提下协调多个 owner。
`AgentCoordinator` 是可选 policy 层；每个被接纳的 handoff 都映射为现有
`ExecutionStore` 中一组确定性的 Task/Run。

```text
sequential / parallel / map_reduce / selector / round_robin / swarm
                              │
                              ▼
                       HandoffEnvelope
                              │
                              ▼
                    确定性 Task + Run
                              │
                              ▼
                  ExecutionStore 权威状态
```

成员 registry 是应用配置，不是持久权威。重启后，应用以相同名字重新注册 handler，
terminal handoff 直接从 Store replay。CLI、Web 与 Python 可以投影相同的 Run 和
`handoff_started/completed/failed/cancelled` 事件，无需再发明一种 status。

## 选择最小的归属边界

- 一个目标共享 conversation、tool、authority、budget 与结果时，保持一个 `Agent`。
- 一段工作需要具名 owner、独立可见的 Run、受限并发或持久 handoff replay 时，加入
  `AgentCoordinator`。
- 只有已有 mailbox 应用继续使用 legacy `Team`。它仍作为兼容 facade 保留；新协调
  不应再让 mailbox 成为第二套 Task/Run 权威。

多次模型调用或多个工具本身不构成多 Agent 的理由。成员之间应有真实不同的 authority、
上下文、review 职责或恢复归属。

## 从普通 async Python 开始

```python
from lipas import LIPASRuntime


async def research(topic):
    return {"topic": topic, "facts": ["事实 A", "事实 B"]}


async def write_brief(finding):
    return f"{finding['topic']}: {', '.join(finding['facts'])}"


with LIPASRuntime.open(".lipas") as runtime:
    coordinator = runtime.coordinator(max_concurrency=4)
    coordinator.add("researcher", research)
    coordinator.add("writer", write_brief)

    result = await coordinator.sequential(
        ["researcher", "writer"],
        "release risk",
        coordination_id="release-review-2026-08-23",
    )
    print(result.value)
```

`AgentCoordinator.open("coordination.db")` 提供独立 lifecycle。Runtime 创建的
coordinator 借用 Runtime 的 `ExecutionStore`；关闭 coordinator 不会关闭 Runtime。

成员可以是普通 `Agent` 或 async callable。callable 默认接收 payload；handler 需要
sender、recipient、sequence、parent 或 metadata 时，注册
`receives_envelope=True`：

```python
async def review(envelope):
    return {"from": envelope.sender, "reviewed": envelope.payload}


coordinator.add("reviewer", review, receives_envelope=True)
```

请在 `add()` 中用 `version="..."` 显式标识成员 implementation contract。该 version
参与 durable request fingerprint，因此 pending 或 completed handoff 不会在部署后静默
改变语义。有意改变契约时，应同时使用新的 member version 与新的 handoff identity。

输入和结果必须兼容 JSON，并位于配置的字节上限内。LIPAS 在执行前快照 envelope，
给 handler 的又是另一份副本，因此成员修改数据不会破坏 durable request fingerprint。
`Agent` 成员会获得 `caused_by`、coordination/sender/recipient metadata 和 branch 专属
`RunContext`。

## Policy 只是组合，不产生第二种执行语义

| API | 形态 | 终止方式 |
| --- | --- | --- |
| `handoff` / `dispatch` | 一个具名 owner | terminal value、持久失败、取消或可见 recovery requirement |
| `sequential` | 成员 N 的输出成为 N+1 的输入 | 第一次失败停止链路 |
| `round_robin` | 按固定成员顺序依次轮转 | 配置的轮数或失败 |
| `parallel` | 受限 fan-out | 所有 branch settle；`require_all=False` 暴露保序的部分成功和失败 |
| `map_reduce` | 受限 fan-out 后执行一个 durable reducer handoff | 所有 map branch 与 reducer 都必须成功 |
| `select` | 一个 durable selector 从显式候选中选择 | 选择非候选成员时失败关闭 |
| `swarm` | 成员返回 `Transfer(recipient, payload)` | 第一个普通结果或达到 `max_hops` |

Selector 是有记录的成员，不是隐藏 router；Swarm transfer 有明确上限。Parallel 输出
保持 branch 顺序，不受完成顺序影响。`map_reduce` 给 reducer 传入保序的 `results`
列表，每项包含 recipient、handoff id 与 value。应用始终可以用普通 Python 编写其他
reducer，无需采用新 DSL。

## Identity、lease 与 replay

除非调用方显式提供 `handoff_id`，`HandoffEnvelope.create()` 会根据 coordination id、
sequence、sender、recipient 与 parent 生成稳定 id，并为完整 request 计算 canonical
fingerprint。同一 id 被用于不同输入时，会在成员代码运行前抛出
`CoordinationIdentityConflict`。

对于一个 envelope：

1. LIPAS 创建或找到确定性的 Task 与 Run；
2. 原子 Run lease 只接纳一个 live owner；
3. heartbeat 续租，并观察持久 cancellation；
4. 兼容 JSON 的 terminal result 完成 Run；
5. 重复相同 request 直接返回已存结果，不再次调用成员。

普通成员的持久失败是 terminal，不会静默 retry。另一个 live owner 会得到
`CoordinationBusy`。lease 过期默认产生 `CoordinationRecoveryRequired`，因为前一个
成员可能已经执行了尚未记录结果的 effect；下面的 SQLite durable Agent bridge 是明确的
checkpoint/Effect recovery 例外。

只有整个成员 invocation 是 pure、read-only、provider-idempotent 或已经显式完成
reconciliation 时，才设置 `redelivery_safe=True`：

```python
coordinator.add("catalog-reader", read_catalog, redelivery_safe=True)
```

该声明只允许 lease 过期后重新领取，不证明 exactly-once。普通 `Agent.run()` 成员可以
记录自己的 Effect，但其内部 reason/act loop 尚未由 coordination Run checkpoint。不能仅
因为 handler 是 `Agent` 就把它声明为 redelivery-safe。

### Durable Agent 成员只使用一次 claim

当 `Agent` 使用 SQLite-backed session 时，coordinator 会把已经 claim 的 handoff Run
直接传给 `Agent.run_durable(_claimed_run=...)`。因此 coordination Run 同时就是 Agent
的 execution Run：没有第二次 claim、队列或 completion 记录。Agent 自己负责 phase
checkpoint、heartbeat、Effect recovery 和 Approval/Input Interrupt；coordinator 只记录
handoff 边界并转换 terminal `FinalResult`。

```python
with LIPASRuntime.open(".lipas") as runtime:
    agent = Agent(
        adapter=adapter,
        model="provider-model",
        tools=[write_file],
        session_path=".lipas/agent-claims.db",
    )
    runtime.coordinator().add(
        "writer",
        agent,
        approval_policy=writes_require_approval,
    )
```

Approval 或 input 请求会原子地保存 checkpoint，并把同一个 Run 置为 `waiting`。通过
`ExecutionStore.resolve_interrupt(...)` 处理后，再 dispatch 同一个 envelope 即可从原
checkpoint 恢复。已经完成的 model/tool Effect 从 Agent claim tape replay；不确定的外部
Effect 会以 `OrphanedEffectError` 失败关闭，而不是静默再次提交。durable Agent 路径的过期
lease 可按该 recovery 协议重新领取；普通 async 成员仍受显式 `redelivery_safe` 约束。

## Cancellation 与 deadline

每个 branch 继承 parent `RunContext` 的 cancellation token 和绝对 monotonic deadline；
coordinator 在 terminal settlement 前再次检查二者。也可以持久地执行 operator 取消：

```python
run = coordinator.get_handoff_run(envelope)
coordinator.cancel_handoff(envelope.id)
```

heartbeat 会观察 `cancel_requested`，协作式停止成员，并把权威 Run settle 为 cancelled。
lease loss 的语义不同：它会报告 recovery uncertainty，绝不会被当成取消可能已经接管该
Run 的新 owner 的权限。

## 失败与数据边界

- 普通成员异常详情不会写入公共结果；Run 只保存稳定 error type 与通用 message。
- 无法序列化或超过字节限制的结果会让 handoff 失败，不会产生无法 replay 的成功。
- `__lipas_coordination__` 是保留的顶层 result 字段。handler 必须返回类型化 `Transfer`，
  不能伪造内部 replay marker。
- failed handoff 不自动 retry。只有 operator 或应用确定新尝试安全后，才能使用新的
  handoff identity。
- Parallel 不会因为一个普通 branch 失败，就取消其他已经提交的 sibling。返回的失败
  保留 envelope，由 host 显式决策。
- Skill 与 Memory 仍不授予 authority。每个 Agent 的 Tool 才是可执行 capability；
  handoff 不会秘密委托权限。

## 嵌套与 host-owned routing

async 成员可以调用另一个 coordinator，因此嵌套 Team 不需要核心 graph 类型。当嵌套
工作必须共享 cancellation 与 deadline 时，应显式传递当前 `RunContext`。host 仍负责
成员 discovery、tenancy、组织 policy，以及嵌套工作使用哪一个 coordinator/store。

LIPAS 有意不持久化 Python callable、不自动发现成员、不静默替换不可用成员，也不解释
任意 graph state。

## 0.39 已完成与 0.40 已发布边界

当前切片完成了小型协调标准库：

- 稳定 envelope 与确定性 Task/Run 映射；
- lease、heartbeat、durable cancellation、terminal replay 和失败关闭的 identity reuse；
- sequential、RoundRobin、受限 parallel、map/reduce、durable Selector 和受限 Swarm；
- Runtime composition、Agent causality、公共事件、字节上限与跨连接并发测试；
- 只 claim 一次的 durable Agent 成员、checkpointed Approval/Input suspension、Effect
  recovery，以及同一 envelope 的 resume/replay；
- 可重连的聚合 event handle、原子 shared budget reservation、显式 capability delegation，
  以及无第三方依赖的 LangGraph/AutoGen handoff boundary；
- `ExtensionManifest`、scaffold、离线 conformance SDK，以及 0.40 的
  `LocalWebOperator`、故障演练和 ExecutionStore transition benchmark 基础。

已经发布的 0.40 契约继续加深边界，而不是增加更多角色名称：

1. 通过 local Web UI 暴露 Task/Run/Interrupt/event projection，同时让 mutation 仍由
   token 保护并委托给 store；
2. 在显式 named boundary 演练 process-kill、database busy/corruption、取消 race，以及
   redelivery-safe/uncertain member fixture；
3. 把 connector scope、approval、reconciliation、provenance 与 version compatibility
   fixture 加入 extension conformance；
4. 完成这些基础后，才考虑带版本 migration 与 subgraph 语义的声明式 graph package，
   并保持可选。

SQLite 适合当前本地与中等并发设计。remote worker、multi-host fencing、tenancy 与
distributed queue 属于另一层部署工作，不能由 `AgentCoordinator` 的存在推断出来。
