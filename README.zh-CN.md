# LIPAS

> 语言：[English](README.md) | [中文](README.zh-CN.md)

LIPAS 是可信 AI 执行系统。
它从一个 assistant 开始。只有在应用确实需要时，才添加相应的可靠性边界。

```text
Agent  = 一个会思考并使用工具的 assistant
@tool  = 一项带已声明副作用的显式 capability
Team   = 在具名 assistant 或函数之间建立可持久化 handoff
```

> **0.10.0 public beta。** 此版本在已有 Ollama、注入 client 的 Anthropic、
> OpenAI Responses、持久 SQLite session、安全 replay、supervision 与
> at-least-once Team 基础上加入 checkpointed ReAct execution、持久审批、取消与
> 崩溃恢复。

## 一个系统，两层能力

```text
LIPAS 本地任务工作台（开发中）
  Task / Workspace / Approval / Artifact / Task CLI / Local Web
                              │
                              ▼
LIPAS Python runtime（当前可用）
  Agent / Tool / Effect / Guard / Budget / Replay / Execution / Operation / Team
```

工作台是 LIPAS 执行工作区任务的第一方体验，例如检查文件、进行受控修改、运行验证并
交付报告。对于需要自有领域模型或界面的应用，Python API 仍然可以独立嵌入。两层能力
共享同一套 Effect 与审计记录；工作台不会另建一套执行模型。

## 底层的一个核心想法

LIPAS 不要求你编写 graph 或特殊 workflow 语言。你只需写普通 Python：一个
`Agent` 调用模型和普通 `@tool` 函数。runtime 会把这项工作中与可靠性相关的部分
接纳为不可变的 **Claim（声明）存储快照**。调用方仍可修改尚未提交的 Claim 对象，
但 store 接纳后的记录不会被这些修改重写。

一次 **fold（折叠）** 会接纳每条稳定 Claim 一次，验证它，并更新同一记录上的小型
派生视图：history 回答发生了什么，capability 约束资源消耗，effect 记录
`intent → result | rejection`。

```text
普通 Python Agent / Tool / Execution / Operation / Team
                 │
                 ▼
           append-only Claims
                 ├── history:    决策与 handoff
                 ├── capability: budget 与消耗
                 └── effect:     intent、result 与 lineage
```

这同一条证据 tape 使各项功能能够协作，而不是成为互不相干的特性：guard 和 budget 在
调用前作决定；replay 替换已记录的结果；supervision 记录其建议；Team handoff 有
稳定 causal id；外部 write 可以相对于其记录的 intent 进行 reconciliation；执行控制
store 通过可修复 outbox 镜像 transition。代码保持自然的 Python，因为 LIPAS 记录的
是工作边界，而不是取代你的控制流。

如需精确的保证与限制，请阅读简短的[执行模型](docs/execution-model.zh-CN.md)。

## 从这里开始

```bash
pip install 'lipas[ollama]'
ollama pull gemma4:12b
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

第一次接触 LIPAS？请按顺序阅读[循序上手 LIPAS](docs/tutorial.zh-CN.md)：从第一个
Agent、工具、副作用、结果、session，逐步到 budget、replay、持久恢复、write、
Skill、Team 和完整可运行项目。编号的
[示例课程](examples/README.zh-CN.md)仍是聚焦场景的参考集合。

## 何时增加更多组件

当一个连贯目标共享同一段对话、工具集、budget 和答案时，请保持一个 Agent。多个
步骤或多个工具并不意味着需要 Team。

只有当工作需要独立 owner 或恢复边界时才添加 `Team`：例如可独立重启的任务、不同
authority/budget、需单独审计的结果，或人工/外部操作 handoff。Team 成员通常是
Agent，也可以是普通 async 函数。普通脚本中可以这样写：

```python
from lipas import Team


async def researcher(prompt):
    return {"finding": f"researched: {prompt}"}


with Team.open("runs/team.db") as team:
    team.add("research", researcher)
    finding = team.ask_sync("research", "check release risks")
```

## 只在需要时引入可靠性

| 加入 | LIPAS 提供 |
|---|---|
| `@tool(side_effect="read_only")` | 显式的 replay 与 retry 安全类别 |
| `session="runs/app.db"` | intent、result、消耗与决策的持久 trace |
| `budgets={...}` | 已知限制前的 pre-flight rejection |
| `tool_guards=[...]` | 实时调用前有记录的 policy denial |
| `OperationJournal` | external write 的 idempotency-key 持久化与 reconciliation state |
| `Team` | 带 lease 和 acknowledgement 的持久、at-least-once handoff |
| `ExecutionStore` + `Agent.run_durable()` | 带 lease 的 ReAct checkpoint、审批中断、取消与崩溃恢复 |

这份记录不是魔法 memory system，LIPAS 也不是 graph/workflow DSL。应用仍然拥有自己
的领域数据、业务规则和面向用户的流程。

高层 `Agent` API 返回最终结果。需要对接底层流事件时，`LLMHarness.stream(...)`
提供规范化 stream event；但 LIPAS 尚未从 `Agent` 提供 token streaming。

## 可复用 Skill

Skill 是可移植的 `SKILL.md` instruction 文件：它捕获 Agent 应如何处理重复工作，
但不会授予任何新 authority。工具仍然是唯一的可执行 capability。先复制一个现成的
[示例 Skill](examples/skills)，然后将其目录传给 Agent：

```python
from lipas import Agent
from my_app.tools import search_papers

agent = Agent.ollama(
    tools=[search_papers],
    skills="skills/research-brief",
)
```

research、support-triage、daily-brief 与 safe-external-actions Skill 有意保持为小
模板：应当按你的标准编辑它们，不要把 prompt 文本当作 permission system。

## 尝试并检查

可选 CLI 用于尝试普通 Python Agent、检查其 session；它不是第二种配置语言。

```bash
pip install 'lipas[ollama,cli]'
lipas init support-demo --model gemma4:12b
cd support-demo
lipas chat --factory agent:build_agent
lipas trace runs/chat.db
lipas effects runs/chat.db
```

从源码 checkout、但尚未安装时，请改用 `python -m lipas.cli`。session 文件会自动
创建。Ollama 是本地的，但通过本地 HTTP service 访问；timeout 表示本地 daemon/model
未及时回答，并不表示 LIPAS 联系了互联网。

## 按需阅读

- [循序上手 LIPAS](docs/tutorial.zh-CN.md) —— 推荐的线性教程，从一个 Agent 到
  完整项目。
- [执行模型](docs/execution-model.zh-CN.md) —— Claim、effect、持久 run、replay、
  external operation 与 Team 的精确语义和限制。
- [路线图](docs/roadmap.zh-CN.md) —— runtime 与本地任务工作台如何作为同一个
  LIPAS 项目推进。
- [示例](examples/README.zh-CN.md) —— 从高层 API 到更底层 harness 的聚焦、可运行
  场景。
- [更新日志](CHANGELOG.md) —— release 历史。

## 许可证

[Apache License 2.0](LICENSE)
