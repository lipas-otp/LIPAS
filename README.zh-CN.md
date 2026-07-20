# LIPAS

> 语言：[English](README.md) | [中文](README.zh-CN.md)

LIPAS 是面向个人与小团队的本地可信任务 Agent。它在用户选定的 workspace 中工作，
高风险动作先审批，中断后能够恢复，完成后验证结果并交付证据，而不只是结束一段聊天。
Python runtime 是内部可靠性基础，也保留为可选的高级嵌入能力。

```text
Agent  = 一个会思考并使用工具的 assistant
@tool  = 一项带已声明副作用的显式 capability
Team   = 在具名 assistant 或函数之间建立可持久化 handoff
```

> **0.20.0 本地任务产品 alpha。** 此版本加入第一方任务工作台与 CLI、带 heartbeat
> 和恢复能力的持久后台调度、workspace 暂存变更、审批收件箱、隔离命令执行、
> secret-safe 持久化、验证证据，以及显式 apply/discard。

## 一个系统，两层能力

```text
LIPAS 本地任务工作台（0.20.0 产品 alpha）
  Task / Workspace / Approval / Artifact / Task CLI / 未来的 Local Web
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

## 本地工作区任务（产品 alpha）

0.20.0 版本开启产品发布线，并提供第一条可运行的本地任务产品纵切。它在独立产品层复用同一个
`ExecutionStore` 与 Effect tape，不会把 Workspace、Artifact 或 Report 概念反向塞进
Agent runtime。默认状态目录是 `~/.lipas`，也可以用 `LIPAS_HOME` 或 `--home` 指定。

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
```

CLI Task 现在修改每 Run 独立的 staging workspace，不直接修改用户选择的 workspace。
staged 文件写入不再逐个打断；命令仍持久等待审批，批准后同一个 Run 从 checkpoint 恢复。首版工具
仅支持选定工作区内的受限文件操作、只读 Git status/diff，以及无 shell 展开的命令白名单。
报告会列出实际变更、验证命令、退出状态与尚未解决的风险。Python factory 可以接收
`tools`、`session_path` 和 `workspace` 关键字；不提供 factory 时使用本地 Ollama。

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
并行 Task 共享 budget 或 single-writer journal state。

Git workspace 会快照 tracked 与未被 ignore 的 untracked 文件；其他 workspace 快照普通
文件。疑似 secret 的路径和文本内容、symlink、生成式 cache 目录以及超过单文件限制的文件
会被排除，超出总文件/大小上限则失败关闭。首版 snapshot backend 有意保持受限；被 Git ignore 的依赖目录在验证时可能需要重新
安装，后续可增加 read-only mount 方案。

## 实验性互操作

LIPAS 优先发展自己的任务产品。LangGraph、MCP server、OpenCrew/OpenClaw adapter
只是实验性兼容样例，不属于核心产品界面，也不承诺长期兼容。只有现有系统确实需要入口
时，才参考[实验性 Integration 指南](docs/integrations.zh-CN.md)。

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
- [实验性 Integration](docs/integrations.zh-CN.md) —— 可选 LangGraph、MCP server、
  OpenCrew/OpenClaw 和 Action Gateway 兼容样例。
- [示例](examples/README.zh-CN.md) —— 从高层 API 到更底层 harness 的聚焦、可运行
  场景。
- [更新日志](CHANGELOG.md) —— release 历史。

## 许可证

[Apache License 2.0](LICENSE)
