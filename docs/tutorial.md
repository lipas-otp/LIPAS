# LIPAS, step by step

> Language: [English](tutorial.md) | [中文](tutorial.zh-CN.md)

This is a small book for building one useful LIPAS assistant. Read the chapters
in order on a first pass. Each chapter introduces one need, one piece of code,
and one boundary; it does not ask you to adopt every reliability feature at
once.

The examples use a local Ollama model so that the first run needs neither an
API key nor a cloud account. The same `Agent` API can use another adapter when
your application needs one.

## The route

1. Ask one assistant a question.
2. Give it one explicit capability.
3. Classify what that capability can do.
4. Handle results in a normal Python program.
5. Keep a durable record when the work matters.
6. Add limits, replay, and write safety only when the problem calls for them.
7. Checkpoint a run only when it must survive waiting or interruption.
8. Finish with reusable guidance, handoffs, and complete projects.

## Before chapter 1

Install LIPAS and one local model:

```bash
pip install 'lipas[ollama]'
ollama pull gemma4:12b
```

Ensure Ollama is running. LIPAS connects to `http://localhost:11434` by
default; set `OLLAMA_HOST` if your service is elsewhere.

All code below can live in one ordinary Python file and run with
`python your_file.py`.

## 1. Start with one assistant

An `Agent` is one model, one instruction set, and a loop that lets the model
use the capabilities you give it. Start with no capabilities at all:

```python
from lipas import Agent


with Agent.ollama(
    instructions="Answer concisely and say when you are uncertain.",
) as agent:
    result = agent.ask("What is a good name for a weekly engineering update?")
    print(result.text)
```

`Agent.ollama()` creates an Ollama-backed Agent. Its first positional argument
is `model`, but it is optional: the documented default is `gemma4:12b`.
Specify a model only when your environment uses another one:

```python
agent = Agent.ollama("qwen2.5:7b", instructions="Be concise.")
```

For most scripts, `ask()` is the only method you need. It runs the async agent
loop and returns one `FinalResult`. Async hosts can use `Agent.stream()` or a
`Session`/`RunHandle` for provider-neutral lifecycle and model/tool events.

## 2. Give the assistant one capability

A model can write text, but it cannot inspect your application data unless you
expose a Python function as a tool. The decorator turns the function into an
explicit capability:

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

The function name, parameter types, and docstring become the model-facing
tool description. Keep all three specific. `lookup_customer(customer_id)` is
easier for a model to use safely than a vague `query(value)`.

The model chooses whether to call a tool; LIPAS does not turn a user message
into a direct Python function call. Make the instruction and the request
unambiguous if calling the tool is important. Also choose a model with tool
calling support: `gemma4`, `qwen2.5`, and `llama3.1` are suitable local
families, while some small or older models may ignore tools.

## 3. Tell the truth about side effects

Every `@tool` requires `side_effect=`. It is not decorative metadata: LIPAS
uses it to record effects and decide what may safely run during replay.

| Value | Use it when | Typical example |
| --- | --- | --- |
| `"pure"` | Output depends only on the input; it does not read or change external state. | Format text, calculate a total. |
| `"read_only"` | It may read a database, file, or API but makes no change. | Look up a customer, search documents. |
| `"idempotent_write"` | It changes state, but repeating the same operation has the same final effect. Your application or provider must actually enforce that property. | Upsert a preference with an idempotency key. |
| `"external_write"` | Repeating it could create another meaningful external effect. | Charge a card, submit an order, send a message. |

Use the narrowest truthful class. A database lookup is `read_only`, not
`pure`, because its answer can change without its input changing. A payment is
`external_write` even if it usually succeeds. Do not call a write idempotent
only because you hope a retry will be harmless.

There is one extra flag, not a fifth class:

```python
@tool(side_effect="external_write", observability_only=True)
def emit_metric(name: str, value: float) -> None:
    """Send a metric to the monitoring system."""
```

`observability_only=True` is for logging, metrics, or trace export that have
no application-semantic effect. It does not make an ordinary business write
safe to replay.

## 4. Call an Agent and inspect the result

Use the calling form that matches your application:

| Code | Where it belongs |
| --- | --- |
| `result = agent.ask(prompt)` | A normal synchronous Python script. |
| `result = await agent.run(prompt)` | An async web service, worker, or notebook. |
| `result = await agent(prompt)` | The shorter async spelling; it is an alias for `run`. |

The return value is always a `FinalResult`. A good application handles the
terminal reason rather than assuming every run produced text:

```python
result = agent.ask("Who is customer C-42?")

if result.is_error:
    print("The agent could not finish:", result.error)
elif result.is_natural:
    print(result.text)
else:
    print("The agent stopped:", result.stop_reason)
```

The fields you will use most are `text`, `is_error`, `error`, and
`stop_reason`. `result.state` contains the final message history. Pass it back
to `run(..., state=result.state)` when you intentionally want to continue the
same conversation; separate `ask()` calls otherwise begin with separate prompt
state. Use `close()` when you do not use `with Agent.ollama(...) as agent:`.

## 5. Make a run inspectable

Add a `session` when you need a durable record of model decisions, tool
intents, tool results, and budget accounting:

```python
with Agent.ollama(
    tools=[lookup_customer],
    instructions="Use lookup_customer for customer ids.",
    session="runs/support.db",
) as agent:
    result = agent.ask("Who is customer C-42?")
```

The program still looks like ordinary Python. The difference is that the
SQLite file survives the process. Inspect it after a run:

```bash
python -m lipas.cli trace runs/support.db
python -m lipas.cli effects runs/support.db
```

Omit `session` for an in-memory experiment. Add it before a tool touches data
that you may later need to explain or reproduce.

## 6. Limit work before it happens

Budgets reject a request before an estimated limit is exceeded. They are not a
replacement for application authorization, but they make operational limits
explicit:

```python
agent = Agent.ollama(
    tools=[lookup_customer],
    instructions="Use the lookup when useful.",
    max_tokens=400,
    max_iterations=3,
    budgets={"tool_calls": 3, "tokens_out": 1_200},
)
```

`max_tokens` is the maximum output for one model request;
`max_iterations` is the maximum model/tool loop length. `budgets` applies hard
limits across the run. Add `tool_guards` when a particular call needs a
recorded allow-or-deny decision. See
[`examples/05_budget_limit.py`](../examples/05_budget_limit.py) for a complete
pre-flight rejection you can run without a model server.

## 7. Replay a recorded decision safely

Replay is for inspection and controlled reproduction, not for quietly
repeating the world. The default strict replay substitutes the recorded model
reply and tool result; it does not contact the live model, database, or API.

That is why the side-effect class matters. A live reroute can re-execute a
pure or read-only tool, but an external write is refused unless the caller
explicitly opts in. Read and run
[`examples/06_strict_replay.py`](../examples/06_strict_replay.py) before
enabling any live replay mode. The exact matrix is documented in the
[execution model](execution-model.md#replay-reproduce-decisions-without-accidentally-repeating-effects).

## 8. Treat an external write as a different problem

An Agent may request an external write, but the model and a tool declaration
cannot make a payment or order exactly-once. Networks can fail after a provider
received a request and before your process learned the outcome.

For a provider that supports an idempotency key, use `OperationJournal` around
the submission. It records the key before sending, preserves uncertain states,
and requires reconciliation rather than blind resubmission. Start from
[`examples/09_external_operation.py`](../examples/09_external_operation.py).

This chapter is deliberately late. Most first assistants should only read
facts and prepare a human decision; do not introduce an external write merely
to make an example look advanced.

## 9. Reuse guidance and separate ownership only when needed

A Skill is a portable `SKILL.md` instruction file. It changes how an Agent
approaches a task; it never grants a tool capability:

```python
agent = Agent.ollama(
    tools=[lookup_customer],
    skills="skills/support-triage",
    instructions="Resolve the request using the available tools.",
)
```

Use one Agent for one coherent goal, even when it uses several tools and takes
several steps. Add a `Team` only when work needs a separate owner, restart
boundary, authority, or audit trail. A Team member may be an Agent or an
ordinary async function:

```python
from lipas import Team


async def researcher(prompt: str) -> dict[str, str]:
    return {"finding": f"research complete: {prompt}"}


with Team.open("runs/team.db") as team:
    team.add("research", researcher)
    finding = team.ask_sync("research", "check release risks")
```

`Team` delivery is at least once. Supply a stable `message_id=` if the member
will initiate idempotent or external work. The two-owner project below shows
the complete shape.

## 10. Resume one Agent run after approval or interruption

A durable session records what an Agent did. A durable execution additionally
records where the ReAct loop can resume. Add this boundary when one logical run
must wait for approval, survive process interruption, or accept cooperative
cancellation without appending its prompt or repeating completed effects.

Durable execution uses two SQLite records on purpose:

- the Agent `session` owns Claims, Effects, spend, and stable effect identity;
- `ExecutionStore` owns Task, Run, lease, checkpoint, and Interrupt state.

Passing the Agent's `rowset` to `ExecutionStore` does not change that authority:
it mirrors control transitions into the Claim evidence tape through a local,
crash-repairable outbox.

Create the Task and Run before calling `run_durable()`. A write approval policy
raises `RunSuspended` only after the checkpoint and Interrupt are durable:

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
            # A real application shows suspended.interrupt.request to a user.
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

The Agent must use `session=` or `session_path=`; an in-memory Claim tape is
rejected because a checkpoint alone cannot prove whether an effect already
finished. Resume through `resume_durable()`—the original input is already
checkpointed. Completed terminal runs restore their result without reclaiming
a lease or calling the provider again. Execution schema mismatches fail at open
time instead of interpreting an incompatible checkpoint.

Run [`examples/11_durable_execution.py`](../examples/11_durable_execution.py)
for a provider-free approval/resume flow. Automatic lease heartbeat and typed
model/tool phase timeouts are provided. Run-wide absolute deadlines and
durable event catch-up use the same public contract. The exact failure
semantics are in the
[execution model](execution-model.md#durable-react-runs).

## 11. Guided projects

These are the longer, runnable examples to read after the chapters above.
They deliberately use local data or offline functions so that you can inspect
the LIPAS boundary before replacing a tool body with a real client.

| Project | Read it after | What it brings together |
| --- | --- | --- |
| [Research brief](../examples/02_research_brief.py) | Chapters 1–5 | A read-only search tool, a writing Skill, a session, budgets, and a concise evidence-based answer. |
| [Support triage](../examples/03_support_triage.py) | Chapters 1–6 | Two narrowly scoped customer-support tools, safe guidance, a Skill, budgets, and a durable trace. |
| [Daily brief](../examples/04_daily_brief.py) | Chapters 1–6 | Several read-only sources become one operational recommendation. |
| [Safe external operation](../examples/09_external_operation.py) | Chapters 7–8 | Idempotency keys, uncertainty after failure, reconciliation, and an audit record. |
| [Research review Team](../examples/10_research_review_team.py) | Chapter 9 | Two independently owned handoffs with stable message identities. |
| [Durable execution](../examples/11_durable_execution.py) | Chapter 10 | Separate execution/effect stores, a durable approval Interrupt, and resume of the same run. |
| [Local task product](../examples/12_local_task_product.py) | Chapter 10 | An isolated ChangeSet, command approval, restart, verification evidence, review, and explicit apply. |

Run the first three with a local Ollama model, for example:

```bash
python -m examples.02_research_brief
python -m examples.03_support_triage
python -m examples.04_daily_brief
```

The latter four are provider-free. The complete example catalogue, including
replay and supervision, is in [examples/README.md](../examples/README.md).

## API card

Keep this reference nearby while working through the book:

| Surface | Everyday use |
| --- | --- |
| `Agent.ollama(model="gemma4:12b", ...)` | Build a local Agent. The model argument is optional. |
| `Agent.openai_compatible(model=..., base_url=..., api_key=...)` | Build against an explicit Chat Completions endpoint without provider fallback. |
| `Agent(adapter=..., model=..., ...)` | Build with a provider-specific adapter. `adapter` is required here. |
| `agent.ask(prompt)` | Run synchronously and receive `FinalResult`. |
| `await agent.run(prompt, state=None)` | Run asynchronously; pass a prior state only to continue deliberately. |
| `await agent(prompt)` | Async alias for `run`. |
| `await agent.run_durable(..., execution_store=..., run_id=...)` | Start or continue a checkpointed ReAct run. |
| `await agent.resume_durable(...)` | Resume checkpointed input without appending the prompt again. |
| `ExecutionStore` | Persist Task, Run, lease, Checkpoint, cancellation, and Interrupt state. |
| `ExecutionStore.cancel_task(...)` | Cancel a Task and cooperatively stop its active Run. |
| `ApprovalPolicy` / `writes_require_approval` | Type or use a policy that suspends selected tool calls before execution. |
| `agent.close()` / `with agent:` | Close a durable session. |
| `@tool(side_effect=...)` | Turn a typed, documented Python function into a capability. |

When the API card stops being enough, prefer the closest runnable project over
reading low-level classes. The [execution model](execution-model.md) is the
place for formal behaviour and limits, not the starting point.
