# LIPAS

> 语言：[English](README.md) | [中文](README.zh-CN.md)

LIPAS 是带有 local-first control plane 的可信 Agent 执行与交付平台。它在用户选定的
workspace 中工作，高风险动作先审批，中断后能够恢复，完成后验证结果并交付证据，而不只是
结束一段聊天。模型和执行 provider 可以运行在本地，也可以是显式配置的远程 endpoint；
authority、policy 和 evidence 始终由宿主控制。

```text
Agent  = 一个会思考并使用工具的 assistant
@tool  = 一项带已声明副作用的显式 capability
AgentCoordinator = 在具名成员之间建立 ExecutionStore-backed 归属
Team   = legacy mailbox 兼容 facade
```
---

```text
Agent 提出 Effect
        ↓
Runtime 按 policy、budget、capability、approval 决定准入
        ↓
Harness / Tool / Connector / Worker 执行
        ↓
Observation → Artifact / Report → verified Delivery
```

同一套语义同时支持 deterministic workflow step 与 autonomous agentic step。Agent、graph
node 和 member 只能提出工作，不能直接拥有改变世界的 authority。`EffectProposal`、
`EffectDecision`、`EffectObservation` 是 0.50 边界的公共契约。Runtime bridge 现在会把已准入
的 proposal 传给现有 Harness，由 Harness 将 proposal identity 写入 Run 的 Effect intent，
再从持久 Claim tape 返回 observation。重复 proposal 只会恢复 terminal result；只有 intent
而没有 terminal claim 时保持 `uncertain`，绝不报告为成功。Proposal metadata 会放入独立
namespace，不能覆盖保留审计字段，`caused_by` 作为持久因果链接保留。崩溃后可使用 proposal
id 或映射后的 claim id 做 reconciliation，关闭 orphan 时不会再次提交实时请求。
Proposal identity 对应不可变证据；如果复用时改变 provenance，会 fail closed。
如果改变 causation 也会 fail closed。Gateway 还会把 pending approval 绑定到工具与参数摘要，
因此审批不能被复用到另一份 payload。

```python
observation = await runtime.execute_effect(
    proposal,
    harness=tool_harness,
    target=ToolTarget(send_email, {"to": "user@example.com"}),
    available_capabilities={"email.send"},
    approved=True,
)
```

## 一个系统，一个 local-first control plane

```text
LIPAS control 与产品层（0.63.0 local-first runtime）
  Scenario / Skill / Task / Workspace / Approval / Artifact / Local Web operator
                              │
                              ▼
LIPAS Python runtime（当前可用）
  Agent / Tool / Effect / Guard / Budget / Replay / Execution / Operation
  AgentCoordinator / legacy Team
                              │
                              ▼
执行 provider（本地 sandbox、显式模型 endpoint、未来 worker）
```

工作台是 LIPAS 执行工作区任务的第一方体验，例如检查文件、进行受控修改、阅读受限 PDF、
转换文档、运行验证并交付报告。Coding 任务已经提供读写、文本搜索、Git diff/status 以及白名单测试/
质量检查命令。对于需要自有领域模型或界面的应用，Python API 仍然可以独立嵌入。两层能力
共享同一套 Effect 与审计记录；工作台不会另建一套执行模型。

安装 `lipas[documents]`（或 `lipas[all]`）即可启用受限文档 Tool 所需的可选
PDF/DOCX/XLSX/PPTX 解析器；ZIP/TAR 检查与安全解压使用标准库，并受成员数和展开大小
限制。Coding 工作台还提供受限算术、CSV 概览和临时 Python worker。

应用现在可以通过一个 lifecycle owner 打开这些边界：

```python
from lipas import LIPASRuntime

with LIPASRuntime.open(".lipas") as runtime:
    runtime.execution
    runtime.claims
    runtime.operations
    runtime.handoffs
    runtime.sessions
    runtime.artifacts
    coordinator = runtime.coordinator()
    operator = runtime.operator(operator_token="change-me")
```

全局控制与产品表位于 `.lipas/workspace.db`。每个 Run 的 Claim/Effect tape 继续位于
`.lipas/runs/<run-id>/claims.db`，从而保持 budget 与 replay 隔离，而不会形成第二套
Task/Run 状态机。SQLite 是经过明确选择的本地内核：WAL 让 reader 不受短写事务阻塞，
默认 `synchronous=FULL` 保护持久提交，per-Run evidence 文件减少全局热点。它是有界的
单写者 control-plane 设计，不伪装成分布式数据库。远程模型不会成为 authority；未来 remote
worker 也必须通过同一套 Run、Effect、policy 和 evidence 契约返回。详见
[SQLite 存储与并发](docs/sqlite-storage.zh-CN.md)。打开旧工作区绝不会静默改写数据；请先
运行 `lipas migrate plan`，再以 `lipas migrate apply --yes` 显式迁移。
migration 与 rollback 采用 copy-on-write，保留并验证备份、处理 SQLite WAL 状态，
并拒绝活跃 Runtime 或 SQLite writer。死亡进程留下的 stale migration lock 可以恢复；
活跃 lock 绝不会被删除。

Agent 调用、对话 Session 与 durable Run 也共享 `RunContext`、`AgentEvent`、取消、
deadline 和事件 cursor。权威来源与兼容边界见
[统一 runtime 契约](docs/runtime-contracts.zh-CN.md)。

## 对话是产品入口

LIPAS 已经支持带持久 session 的对话式 REPL：

```bash
lipas chat --model phi4-mini --session runs/chat.db
```

下一步应把对话作为 workspace Task 的统一入口，而不是再创建一套 Agent 或权限系统：

```text
Conversation / chat message
          │
          ├── 只需回答 → Session / RunHandle
          ├── 需要行动 → Task / durable Run
          ├── 高风险操作 → Approval 或 Input Interrupt
          └── 完成工作 → diff / verification / report / delivery
```

---

```python
with LIPASRuntime.open(".lipas", sandbox="local") as runtime:
    chat = runtime.create_conversation(title="发布检查")
    message = runtime.append_message(
        chat.id, role="user", content="检查发布状态", message_id="msg-1",
    )
    task, run, message = runtime.promote_message_to_task(chat.id, message.id)
    page = runtime.conversation_events(chat.id, limit=100)
```

无依赖的 Web preview 通过 `runtime.operator(...)` 启动，与 CLI 和 Python 宿主共享同一套
Task/Run/Approval/Effect 契约。客户端应保存返回的 `message_id` 和 `next_cursor`；这样
重试就是幂等写入，断线后可以从 cursor 继续追赶事件。

## 底层的一个核心想法

LIPAS 不要求你编写 graph 或特殊 workflow 语言。你只需写普通 Python：一个
`Agent` 调用模型和普通 `@tool` 函数。runtime 会把这项工作中与可靠性相关的部分
接纳为不可变的 **Claim（声明）存储快照**。调用方仍可修改尚未提交的 Claim 对象，
但 store 接纳后的记录不会被这些修改重写。

一次 **fold（折叠）** 会接纳每条稳定 Claim 一次，验证它，并更新同一记录上的小型
派生视图：history 回答发生了什么，capability 约束资源消耗，effect 记录
`intent → result | rejection`。

```text
普通 Python Agent / Tool / Execution / Operation / AgentCoordinator
                 │
                 ▼
           append-only Claims
                 ├── history:    决策与 handoff
                 ├── capability: budget 与消耗
                 └── effect:     intent、result 与 lineage
```

这同一条证据 tape 使各项功能能够协作，而不是成为互不相干的特性：guard 和 budget 在
调用前作决定；replay 替换已记录的结果；supervision 记录其建议；coordinator handoff 有
稳定 causal id；外部 write 可以相对于其记录的 intent 进行 reconciliation；执行控制
store 通过可修复 outbox 镜像 transition。代码保持自然的 Python，因为 LIPAS 记录的
是工作边界，而不是取代你的控制流。

如需精确的保证与限制，请阅读简短的[执行模型](docs/execution-model.zh-CN.md)。

## 从这里开始

最稳妥的首次试用方式是先按[五分钟首次试用](docs/onboarding.zh-CN.md#五分钟首次试用)
完成 provider-free 流程，再连接模型。

```bash
pip install 'lipas[ollama]'
ollama pull phi4-mini
```

```python
from lipas import Agent, tool


@tool(side_effect="read_only")
def lookup_customer(customer_id: str) -> str:
    """Look up a customer without changing external state."""
    return f"customer={customer_id}"


with Agent.ollama(
    tools=[lookup_customer],
    instructions="Use tools when useful; answer concisely.",
    session="runs/support.db",  # omit for in-memory use
) as agent:
    result = agent.ask("Find customer C-42")
    print(result.text)
```

`agent.ask(...)` 是普通脚本使用的 API；在 async 应用中使用
`await agent.run(...)`。第一个可运行示例是
[`examples/01_first_agent.py`](examples/01_first_agent.py)。

接入 OpenAI-compatible `/chat/completions` 端点时，请不要把 credential 写入源码，
并显式提供 route：

```bash
pip install 'lipas[compatible]'
```

```python
import os

from lipas import Agent

agent = Agent.openai_compatible(
    model="deepseek-chat",
    base_url="https://api.deepseek.com",
    api_key=os.environ["DEEPSEEK_API_KEY"],
)
```

同一个 factory 可以覆盖火山引擎方舟、阿里百炼、腾讯混元、OpenAI、private gateway
和其他 compatible route。非 streaming 是兼容优先的默认值；SSE 必须显式开启，
provider/model-specific capability 在登记前保持 unknown。URL、安全 CLI 使用、
streaming、tool、错误语义和精确兼容边界见
[OpenAI-compatible 模型端点](docs/model-providers.zh-CN.md)。
运行 Agent 前，可以用 `lipas model check --base-url ... --model ...` 在不访问网络的
情况下验证配置；只有确实要执行一次可能计费的 provider probe 时才显式加入 `--live`。

第一次接触 LIPAS？请按顺序阅读[循序上手 LIPAS](docs/tutorial.zh-CN.md)：从第一个
Agent、工具、副作用、结果、session，逐步到 budget、replay、持久恢复、write、
Skill、多 Agent 协调和完整可运行项目。编号的
[示例课程](examples/README.zh-CN.md)仍是聚焦场景的参考集合。

## 何时增加更多组件

当一个连贯目标共享同一段对话、工具集、budget 和答案时，请保持一个 Agent。多个
步骤或多个工具并不意味着需要多个 Agent。

只有当工作需要独立 owner 或恢复边界时才添加 `AgentCoordinator`：例如可独立重启的任务、不同
authority/budget、需单独审计的结果，或人工/外部操作 handoff。成员通常是
Agent，也可以是普通 async 函数。普通脚本中可以这样写：

```python
from lipas import AgentCoordinator


async def researcher(prompt):
    return {"finding": f"researched: {prompt}"}


async def main():
    with AgentCoordinator.open("runs/coordination.db") as coordinator:
        coordinator.add("research", researcher)
        finding = await coordinator.handoff(
            "research", "check release risks",
            coordination_id="release-risk-v1",
        )
        print(finding.value)
```

只有维护 mailbox API 时才使用 legacy `Team`。新协调及其精确恢复边界见
[多 Agent 协调](docs/multi-agent.zh-CN.md)。

## 只在需要时引入可靠性

| 加入 | LIPAS 提供 |
|---|---|
| `@tool(side_effect="read_only")` | 显式的 replay 与 retry 安全类别 |
| `session="runs/app.db"` | intent、result、消耗与决策的持久 trace |
| `budgets={...}` | 已知限制前的 pre-flight rejection |
| `tool_guards=[...]` | 实时调用前有记录的 policy denial |
| `OperationJournal` | external write 的 idempotency-key 持久化与 reconciliation state |
| `AgentCoordinator` | 确定性 handoff Run、受限 policy、cancellation 与 terminal replay |
| legacy `Team` | 面向已有应用的 mailbox-compatible at-least-once handoff |
| `ExecutionStore` + `Agent.run_durable()` | 带 lease 的 ReAct checkpoint、审批中断、取消与崩溃恢复 |
| `CoordinationEventHandle` | 不引入第二条全局序列的可重连聚合事件 |
| `LocalWebOperator` | 带 token mutation protection 的本地 Task/Run/Interrupt projection |
| `FaultCampaign` / `run_fault_matrix()` | 无隐藏 retry 的隔离 named recovery fixture |
| `benchmark_execution_store()` | 有界 SQLite transition 与 contention 测量 |
| `ExtensionManifest` / `run_conformance()` | 离线 provenance、connector 安全与版本检查 |

这份记录不是魔法 memory system，LIPAS 也不是 graph/workflow DSL。应用仍然拥有自己
的领域数据、业务规则和面向用户的流程。

`Agent.run(...)` 返回最终结果；`Agent.stream(...)`、`Session` 与 durable event
cursor 提供规范化 run/model/tool 事件。adapter 产出的真实 delta 也通过同一套
`AgentEvent` 协议向上提供。

## 本地与混合工作区任务（0.63 产品）

历史 0.31.0 切片让本地任务产品纵切默认使用统一 runtime；当前 0.63 产品版本
在独立产品层复用同一个 `ExecutionStore` 与 Effect tape，不会把 Workspace、Artifact 或
Report 概念反向塞进 Agent runtime。workspace 与 control state 默认保留在本地，模型可以
使用本地 Ollama，也可以使用显式的 OpenAI-compatible endpoint。多机器 worker pool 是未来
执行层，不是 0.63 的隐式 fallback。默认状态目录是 `~/.lipas`，也可以用 `LIPAS_HOME` 或
`--home` 指定。

```bash
lipas task start . "修正文档中的错误并运行相关测试"
lipas task submit . "更新两份本地报告并完成验证"
lipas task worker --max-concurrency 2
lipas task list
lipas task approvals
lipas task show <task-id>
lipas task approve <approval-id>
lipas task diff <task-id>
lipas task apply <task-id>
# 或：lipas task discard <task-id>
lipas task events <task-id>
lipas task report <task-id>
lipas doctor
lipas audit
lipas tour --offline
```

`doctor` 分别报告 storage health 与完整 runtime readiness，并实际执行有界的默认 sandbox
启动探测。`audit` 默认只读：检查 storage invariant，并明确把 Claim lint 标记为
`not_run`。`audit --repair` 会打开 Runtime，修复可恢复的 audit outbox，并 lint 全局证据
以及每一个已登记的 per-Run Claim tape。

CLI Task 现在修改每 Run 独立的 staging workspace，不直接修改用户选择的 workspace。
staged 文件写入不再逐个打断；命令仍持久等待审批，批准后同一个 Run 从 checkpoint 恢复。首版工具
仅支持选定工作区内的受限文件操作、只读 Git status/diff，以及无 shell 展开的命令白名单。
报告会列出实际变更、验证命令、退出状态与尚未解决的风险。Python factory 可以接收
`tools`、`session_path` 和 `workspace` 关键字；不提供 factory 时默认使用本地
Ollama，除非显式提供 OpenAI-compatible `--base-url`、model 与 key 环境变量。

命令执行默认使用 `--sandbox auto`：通过 Bubblewrap 提供最小文件系统和无网络环境，无法
建立隔离时会失败关闭。`--sandbox local` 是仅面向可信代码的显式不安全 fallback。
`task events` 以便于流式消费的 JSONL 输出持久产品事件，包括审批、artifact、verification、
run state 与 report。
同一模型轮次中，彼此独立的 `pure`/`read_only` 工具可以并行；write 与涉及
policy/accounting 的调用仍保持串行。heartbeat 维持 run lease，稳定 Effect 则在中断后
恢复已经完成的调用。

Run 完成后，`task diff` 展示完整 staged file ChangeSet；`task apply` 是显式交付审批，
会在修改任何文件前检查原 workspace 的每个目标仍等于 snapshot baseline。检测到外部漂移
就失败关闭。单文件替换是原子的；若进程在多文件之间中止，重复 apply 可以继续。
`task discard` 丢弃尚未应用的 stage，不改变原 workspace。报告会显示
`delivery: ready|applied|discarded`。

`task submit` 持久化任务，不再要求提交任务的进程一直存活。`task worker` 是本地持久
dispatcher：以受限并发运行多个 Task，重启后领取过期 lease，并在 Run 等待审批时释放
执行槽。`task approvals` 是持久 operator inbox；使用
`task approve <id> --defer-resume` 可让批准后的 Run 回到 worker 队列。
每个 Run 使用独立 Claim/Effect session，全局 `ExecutionStore` 仍是权威队列，从而避免
并行 Task 共享 budget projection 或同一条热点 evidence sequence。

Git workspace 会快照 tracked 与未被 ignore 的 untracked 文件；其他 workspace 快照普通
文件。疑似 secret 的路径和文本内容、symlink、生成式 cache 目录以及超过单文件限制的文件
会被排除，超出总文件/大小上限则失败关闭。首版 snapshot backend 有意保持受限；被 Git ignore 的依赖目录在验证时可能需要重新
安装，后续可增加 read-only mount 方案。

当前边界保持明确：`AgentCoordinator` 是新的 ExecutionStore-backed orchestration
标准库；legacy `Team` 只为兼容保留 mailbox，不再作为新代码的第二套 Task/Run API。
普通 Agent 成员共享 context 与 causality，但不会自动变成 durable。使用 SQLite-backed
session 的 Agent 成员则直接让已 claim 的 handoff Run 承载 Agent checkpoint、Approval/Input
Interrupt 与 Effect tape，因此 resume/replay 不会 double claim。含糊的模型/工具阶段
timeout 会被标记为 recovery-required；operator 必须先完成 Effect/provider reconciliation、
记录 observation/evidence，再显式 reopen Run，之后才能 resume。`LLMHarness.reconcile_orphan()`
与 `ToolHarness.reconcile_orphan()` 可以在不发起未经验证的第二次请求的前提下关闭
intent-only Effect。

## Integration 与执行边界

LIPAS 的核心是执行与交付契约，而不是某一种模型托管拓扑。LangGraph、MCP server、
OpenCrew/OpenClaw adapter 仍是兼容边界，不是另一套 authority，也不是对这些框架的完整复制；
HTTP/MCP client 则是第一方 capability boundary。详见[Integration 指南](docs/integrations.zh-CN.md)。

## 业务 Skill 与 Scenario

Skill 是可移植的 instruction 文件；`BusinessScenario` 组合最小相关 Skill bundle、
生命周期和所需 Tool contract。两者都不授予 authority。Tool 仍是唯一可执行
capability，durable Run 负责审批、恢复和证据。LIPAS 内置 17 个 Skill 与 18 个
Scenario，覆盖文件、工程、办公、个人写作与受限 connector workflow：

```python
from lipas import Agent, ScenarioRegistry

scenarios = ScenarioRegistry.from_names([
    "coding-change",
    "release-readiness",
])
skills = scenarios.skill_registry(
    paths=["skills/repository-conventions"],
)

agent = Agent.ollama(
    skills=skills,
)
```

系统不会自动选择任何内容，因此目录增长不会膨胀无关任务的 prompt。无需运行模型即可
查看配方与 capability boundary：

```bash
lipas skill list
lipas scenario list
lipas scenario show email-delivery
lipas scenario check email-delivery --factory connectors:email_tools
lipas chat --scenario office-report --once "起草项目进展报告"
lipas task start . "修复 parser" --scenario coding-change
```

Connector Scenario 是契约，不是内置账号访问。Email delivery 仍需要应用提供
`send_email` Tool、显式 scope、preview approval、幂等、provider evidence 与
uncertain-result reconciliation。详见[业务 Skill、Scenario 与 Capability](docs/business-skills.zh-CN.md)。

## 尝试并检查

可选 CLI 用于尝试普通 Python Agent、检查其 session；它不是第二种配置语言。

```bash
pip install 'lipas[ollama,cli]'
lipas init support-demo --model phi4-mini
cd support-demo
lipas chat --factory agent:build_agent
lipas trace runs/chat.db
lipas effects runs/chat.db
```

从源码 checkout、但尚未安装时，请改用 `python -m lipas.cli`。session 文件会自动
创建。Ollama 是本地的，但通过本地 HTTP service 访问；LIPAS 也支持显式的远程兼容
endpoint，但不会因此把 task authority 或 evidence 移出宿主 workspace。timeout 表示本地
daemon/model 未及时回答，并不表示 LIPAS 联系了互联网。

## 按需阅读

- [架构导览](docs/architecture.zh-CN.md) —— 请求路径、模块职责、权威存储和入口选择。
- [循序上手 LIPAS](docs/tutorial.zh-CN.md) —— 推荐的线性教程，从一个 Agent 到
  完整项目。
- [执行模型](docs/execution-model.zh-CN.md) —— Claim、effect、持久 run、replay、
  external operation 的精确语义和限制。
- [多 Agent 协调](docs/multi-agent.zh-CN.md) —— 确定性 handoff、协调 policy、
  cancellation、replay 与剩余边界。
- [SQLite 存储与并发](docs/sqlite-storage.zh-CN.md) —— WAL 策略、并发 Run 边界、
  evidence 分页/snapshot 与诚实的规模限制。
- [路线图](docs/roadmap.zh-CN.md) —— local-first control plane 如何从单用户产品发展到
  共享与混合执行。
- [战略](docs/strategy.zh-CN.md) —— LIPAS 与 LangGraph、AutoGen 的定位、当前缺口、
  架构护栏以及通往 0.50 的路径。
- [OpenAI-compatible 模型端点](docs/model-providers.zh-CN.md) —— 通过显式 Chat
  Completions URL、model 与 API key 接入，不使用隐藏 fallback。
- [实验性 Integration](docs/integrations.zh-CN.md) —— 可选 LangGraph、MCP server、
  OpenCrew/OpenClaw 和 Action Gateway 兼容样例。
- [安装、onboarding 与 Design partner 验证](docs/onboarding.zh-CN.md) —— doctor、离线 tour、
  migration、备份、external capability readiness，以及可重复的恢复/reconciliation 试点协议。
- [0.39/0.40 协调与 operator 契约](docs/multi-agent.zh-CN.md) —— 聚合 event handle、
  shared policy、框架 boundary、本地 operator 与故障演练。
- [示例](examples/README.zh-CN.md) —— 从高层 API 到更底层 harness 的聚焦、可运行
  场景。
- [更新日志](CHANGELOG.md) —— release 历史。

## 许可证

[Apache License 2.0](LICENSE)
