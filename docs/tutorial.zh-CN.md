# 循序上手 LIPAS

> 语言：[English](tutorial.md) | [中文](tutorial.zh-CN.md)

这是一本用来构建一个有用 LIPAS assistant 的小教程。第一次阅读时请按章节顺序来：
每章只引入一个需求、一段代码和一个边界；不会要求你一次性采用所有可靠性功能。

这些示例使用本地 Ollama 模型，因此第一次运行不需要 API key 或云端账户。当应用
需要其他 provider 时，同一个 `Agent` API 仍可搭配别的 adapter。

## 学习路线

1. 向一个 assistant 提问。
2. 给它一项显式 capability。
3. 为这项 capability 分类它能做什么。
4. 在普通 Python 程序中处理结果。
5. 当工作变得重要时，保留一份持久记录。
6. 只在问题需要时，再添加 limit、replay 和 write safety。
7. 只有 run 必须跨越等待或中断时，才增加 checkpoint。
8. 最后学习可复用指导、handoff 和完整项目。

## 第 1 章前的准备

安装 LIPAS 和一个本地模型：

```bash
pip install 'lipas[ollama]'
ollama pull gemma4:12b
```

确认 Ollama 正在运行。LIPAS 默认连接 `http://localhost:11434`；服务在其他位置时
设置 `OLLAMA_HOST`。

下面所有代码都可放在一份普通 Python 文件中，并以 `python your_file.py` 运行。

## 1. 从一个 assistant 开始

`Agent` 是一个模型、一组 instructions，以及一个让模型使用你提供 capability 的
循环。先完全不提供 capability：

```python
from lipas import Agent


with Agent.ollama(
    instructions="Answer concisely and say when you are uncertain.",
) as agent:
    result = agent.ask("What is a good name for a weekly engineering update?")
    print(result.text)
```

`Agent.ollama()` 会创建 Ollama-backed Agent。它的第一个位置参数是 `model`，但可
以省略：文档化默认值为 `gemma4:12b`。只有环境使用另一模型时才需要指定：

```python
agent = Agent.ollama("qwen2.5:7b", instructions="Be concise.")
```

对大多数脚本，`ask()` 是唯一需要的方法。它会运行 async Agent loop，返回一个
`FinalResult`。async host 可以使用 `Agent.stream()` 或 `Session`/`RunHandle`
接收 provider-neutral 生命周期与模型/工具事件。

## 2. 给 assistant 一项 capability

模型能写文本，但只有把 Python 函数公开为工具后，它才能检查应用数据。decorator 会
将函数变为显式 capability：

```python
from lipas import Agent, tool


@tool(side_effect="read_only")
def lookup_customer(customer_id: str) -> str:
    """Look up a customer's display name without changing their record."""
    customers = {"C-42": "Ada Lovelace"}
    return customers.get(customer_id, "customer not found")


with Agent.ollama(
    tools=[lookup_customer],
    instructions="Use lookup_customer when a customer id is given.",
) as agent:
    result = agent.ask("Who is customer C-42?")
    print(result.text)
```

函数名、参数类型和 docstring 会成为面向模型的工具描述。三者都应具体；
`lookup_customer(customer_id)` 比含糊的 `query(value)` 更容易被模型安全使用。

模型自行决定是否调用工具；LIPAS 不会把用户消息直接分派为 Python 函数调用。若
调用工具很重要，应让 instruction 和请求都没有歧义。还应选择支持 tool calling 的
模型：本地的 `gemma4`、`qwen2.5` 和 `llama3.1` family 适合；一些小模型或旧模型
可能忽略工具。

## 3. 如实声明副作用

每个 `@tool` 都要求 `side_effect=`。它不是装饰性 metadata：LIPAS 用它来记录
effect，并决定 replay 时什么可以安全运行。

| 值 | 适用情况 | 典型例子 |
| --- | --- | --- |
| `"pure"` | 输出只取决于输入，且不读写外部 state。 | 格式化文本、计算总额。 |
| `"read_only"` | 可以读取数据库、文件或 API，但不改变它们。 | 查询客户、搜索文档。 |
| `"idempotent_write"` | 会改变 state，但重复同一操作后 final effect 相同。应用或 provider 必须真正保证这个属性。 | 使用 idempotency key upsert 偏好设置。 |
| `"external_write"` | 重复操作可能制造又一个有实际意义的外部 effect。 | 扣款、提交订单、发送消息。 |

选择最窄且真实的类别。数据库查询是 `read_only` 而不是 `pure`，因为即使输入不变，
答案也可能变化。付款即使通常会成功，仍然是 `external_write`。不要仅因希望 retry
无害就把 write 叫作 idempotent。

还有一个额外 flag，但不是第五种类别：

```python
@tool(side_effect="external_write", observability_only=True)
def emit_metric(name: str, value: float) -> None:
    """Send a metric to the monitoring system."""
```

`observability_only=True` 用于对应用业务没有语义影响的 logging、metric 或 trace
export。它不会使普通业务 write 在 replay 时变得安全。

## 4. 调用 Agent 并检查结果

请选择适合应用的调用形式：

| 代码 | 适用位置 |
| --- | --- |
| `result = agent.ask(prompt)` | 普通同步 Python 脚本。 |
| `result = await agent.run(prompt)` | async web service、worker 或 notebook。 |
| `result = await agent(prompt)` | 更短的 async 写法，是 `run` 的别名。 |

返回值始终是 `FinalResult`。好的应用应处理终止原因，而不要假定每次运行都会产生
文本：

```python
result = agent.ask("Who is customer C-42?")

if result.is_error:
    print("The agent could not finish:", result.error)
elif result.is_natural:
    print(result.text)
else:
    print("The agent stopped:", result.stop_reason)
```

最常用字段是 `text`、`is_error`、`error` 和 `stop_reason`。`result.state` 包含最终
消息 history。想有意识地续接同一段对话时，将它传回
`run(..., state=result.state)`；否则各次 `ask()` 调用都从独立 prompt state 开始。
不使用 `with Agent.ollama(...) as agent:` 时，请调用 `close()`。

## 5. 让一次运行可检查

需要模型决策、工具 intent、工具 result 和 budget accounting 的持久记录时，添加
`session`：

```python
with Agent.ollama(
    tools=[lookup_customer],
    instructions="Use lookup_customer for customer ids.",
    session="runs/support.db",
) as agent:
    result = agent.ask("Who is customer C-42?")
```

程序仍然是普通 Python；区别在于 SQLite 文件会在进程结束后保留。运行后检查它：

```bash
python -m lipas.cli trace runs/support.db
python -m lipas.cli effects runs/support.db
```

实验时可省略 `session` 使用内存。工具一旦接触你以后需要解释或复现的数据，就应
添加它。

## 6. 在工作发生前限制它

Budget 会在预估限制即将超出前拒绝请求。它不是应用 authorization 的替代品，但会使
运行限制明确：

```python
agent = Agent.ollama(
    tools=[lookup_customer],
    instructions="Use the lookup when useful.",
    max_tokens=400,
    max_iterations=3,
    budgets={"tool_calls": 3, "tokens_out": 1_200},
)
```

`max_tokens` 是单次模型请求的最大输出；`max_iterations` 是模型/工具 loop 的最大
长度；`budgets` 为整次运行应用硬限制。当某项特定调用需要有记录的允许/拒绝决定时
添加 `tool_guards`。参见
[`examples/05_budget_limit.py`](../examples/05_budget_limit.py)：它展示了不需要模型
服务也能运行的完整 pre-flight rejection。

## 7. 安全地 replay 已记录的决策

Replay 用于检查和受控复现，不是为了悄悄重复现实世界。默认 strict replay 会替换
已记录的模型 reply 和工具 result；它不会联系实时模型、数据库或 API。

这正是副作用类别重要的原因。live reroute 可以重新执行 pure 或 read-only 工具，
但 external write 会被拒绝，除非调用者显式 opt in。启用任何 live replay mode 前，
请先阅读并运行 [`examples/06_strict_replay.py`](../examples/06_strict_replay.py)。精确
矩阵见[执行模型](execution-model.zh-CN.md)。

## 8. 将 external write 视为另一类问题

Agent 可以请求 external write，但模型和工具声明不能让一次付款或下单变为
exactly-once。网络可能在 provider 收到请求后、进程知道结果前失败。

对于支持 idempotency key 的 provider，请在提交周围使用 `OperationJournal`。它会
在发送前记录 key，保留 uncertain state，并要求 reconciliation，而不是盲目再次
提交。请从 [`examples/09_external_operation.py`](../examples/09_external_operation.py)
开始。

这一章故意放在后面。大多数第一版 assistant 只应阅读事实、准备人工决策；不要仅为
了让示例显得高级就引入 external write。

## 9. 只在需要时复用指导、分离归属

Skill 是可移植的 `SKILL.md` instruction 文件。它改变 Agent 处理任务的方式，但绝不
授予工具 capability：

```python
agent = Agent.ollama(
    tools=[lookup_customer],
    skills="skills/support-triage",
    instructions="Resolve the request using the available tools.",
)
```

即使一个 Agent 使用多个工具、经历多步，面对一个连贯目标时仍应使用一个 Agent。只在
工作需要独立 owner、重启边界、authority 或 audit trail 时才添加 `AgentCoordinator`。成员
可以是 Agent 或普通 async 函数：

```python
from lipas import AgentCoordinator


async def researcher(prompt: str) -> dict[str, str]:
    return {"finding": f"research complete: {prompt}"}


async def coordinate() -> None:
    with AgentCoordinator.open("runs/coordination.db") as coordinator:
        coordinator.add("research", researcher)
        finding = await coordinator.handoff(
            "research", "check release risks",
            coordination_id="release-risk-v1",
        )
        print(finding.value)
```

该 handoff 是一个确定性的 ExecutionStore Run；已完成工作直接 replay。过期工作默认
失败关闭，只有整个成员以 `redelivery_safe=True` 注册时才允许重领。legacy `Team` 只为
mailbox 示例和已有应用保留。协调 external write 前请先阅读
[多 Agent 协调](multi-agent.zh-CN.md)。

## 10. 在审批或中断后恢复同一个 Agent run

持久 session 记录 Agent 做了什么；持久 execution 还记录 ReAct loop 可以从哪里恢复。
只有一个逻辑 run 必须等待审批、跨越进程中断，或接受协作式取消，同时又不能重复追加
prompt 或重复已完成 effect 时，才需要这个边界。

持久 execution 有意使用两份 SQLite 记录：

- Agent `session` 保存 Claim、Effect、消耗与稳定 effect identity；
- `ExecutionStore` 保存 Task、Run、lease、Checkpoint 与 Interrupt state。

把 Agent 的 `rowset` 传给 `ExecutionStore` 不会改变上述权威关系：它只会通过本地、
可修复 crash window 的 outbox，把控制 transition 镜像进 Claim 证据 tape。

调用 `run_durable()` 前先创建 Task 和 Run。write approval policy 只会在 checkpoint
和 Interrupt 都已持久化后抛出 `RunSuspended`：

```python
from pathlib import Path

from lipas import (
    Agent,
    ExecutionStore,
    RunSuspended,
    writes_require_approval,
)


async def execute(agent: Agent) -> None:
    with ExecutionStore("runs/execution.db", rowset=agent.rowset) as executions:
        task = executions.create_task("prepare one approved change", Path.cwd())
        run = executions.create_run(task.id)
        try:
            result = await agent.run_durable(
                "Prepare and apply the change.",
                execution_store=executions,
                run_id=run.id,
                approval_policy=writes_require_approval,
            )
        except RunSuspended as suspended:
            # 真实应用应先把 suspended.interrupt.request 展示给用户。
            executions.resolve_interrupt(
                suspended.interrupt.id,
                allow=True,
                response={"approved_by": "operator"},
            )
            result = await agent.resume_durable(
                execution_store=executions,
                run_id=run.id,
                approval_policy=writes_require_approval,
            )
        print(result.stop_reason, result.text)
```

Agent 必须使用 `session=` 或 `session_path=`；内存 Claim tape 会被拒绝，因为只有
checkpoint 无法证明某项 effect 是否已经完成。应通过 `resume_durable()` 恢复——原始
输入已经写入 checkpoint。已完成的 terminal run 会直接恢复结果，不重新 claim lease，
也不再次调用 provider。execution schema 不匹配会在打开时失败，不会错误解释不兼容
checkpoint。

运行 [`examples/11_durable_execution.py`](../examples/11_durable_execution.py) 可查看
完全不依赖 provider 的审批/恢复流程。自动 lease heartbeat 与有类型的模型/工具阶段
timeout 已经提供；跨阶段绝对 deadline 与 durable event catch-up 使用同一公共契约。精确失败语义见
[执行模型](execution-model.zh-CN.md#持久-react-run)。

## 11. 引导式项目

这些是在前面章节之后阅读的较长、可运行示例。它们有意使用本地数据或离线函数，
使你可以在用真实 client 替换工具体前，先检查 LIPAS boundary。

| 项目 | 建议完成章节后阅读 | 组合了什么 |
| --- | --- | --- |
| [Research brief](../examples/02_research_brief.py) | 第 1–5 章 | 一个只读搜索工具、写作 Skill、session、budget 和简洁的 evidence-based answer。 |
| [Support triage](../examples/03_support_triage.py) | 第 1–6 章 | 两个范围狭窄的客户支持工具、安全指导、Skill、budget 与持久 trace。 |
| [Daily brief](../examples/04_daily_brief.py) | 第 1–6 章 | 将多个只读来源变成一项运营建议。 |
| [Safe external operation](../examples/09_external_operation.py) | 第 7–8 章 | idempotency key、失败后的不确定性、reconciliation 和 audit record。 |
| [Research review Team](../examples/10_research_review_team.py) | 第 9 章 | 两个独立 owner 的 handoff，以及稳定 message identity。 |
| [多 Agent 协调](../examples/13_multi_agent_coordination.py) | 第 9 章 | ExecutionStore-backed sequential 与 map/reduce 归属，以及 terminal replay。 |
| [Durable execution](../examples/11_durable_execution.py) | 第 10 章 | 分离的 execution/effect store、持久审批 Interrupt，以及恢复同一个 run。 |
| [Local task product](../examples/12_local_task_product.py) | 第 10 章 | 隔离的 ChangeSet、命令审批、重启恢复、验证证据、review 与显式 apply。 |
| [Operator beta](../examples/14_operator_beta.py) | 第 10 章 | 本地 Task/Run projection、确定性故障演练和有界 SQLite transition benchmark。 |

例如，使用本地 Ollama 模型运行前三个：

```bash
python -m examples.02_research_brief
python -m examples.03_support_triage
python -m examples.04_daily_brief
```

后四个不依赖 provider。包括 replay 和 supervision 的完整示例目录见
[examples/README.zh-CN.md](../examples/README.zh-CN.md)。

## API 卡片

学习本书时，将这份参考放在手边：

| Surface | 日常用途 |
| --- | --- |
| `Agent.ollama(model="gemma4:12b", ...)` | 构建本地 Agent；`model` 参数可省略。 |
| `Agent.openai_compatible(model=..., base_url=..., api_key=...)` | 使用显式 Chat Completions endpoint 构建，不做 provider fallback。 |
| `Agent(adapter=..., model=..., ...)` | 使用 provider-specific adapter 构建；此处 `adapter` 必填。 |
| `agent.ask(prompt)` | 同步运行，收到 `FinalResult`。 |
| `await agent.run(prompt, state=None)` | async 运行；只有刻意续接时才传入之前 state。 |
| `await agent(prompt)` | `run` 的 async 别名。 |
| `await agent.run_durable(..., execution_store=..., run_id=...)` | 启动或继续 checkpointed ReAct run。 |
| `await agent.resume_durable(...)` | 恢复已保存输入，不再次追加 prompt。 |
| `ExecutionStore` | 持久化 Task、Run、lease、Checkpoint、取消与 Interrupt state。 |
| `ExecutionStore.cancel_task(...)` | 取消 Task，并协作式停止其 active Run。 |
| `ApprovalPolicy` / `writes_require_approval` | 标注或直接使用在执行前挂起指定工具调用的 policy。 |
| `agent.close()` / `with agent:` | 关闭持久 session。 |
| `@tool(side_effect=...)` | 将有类型、带说明的 Python 函数变为 capability。 |

当 API 卡片不再足够时，应优先阅读最接近的可运行项目，而不是直接阅读底层类。
[执行模型](execution-model.zh-CN.md)适合查阅正式行为和限制，而不是入门起点。
