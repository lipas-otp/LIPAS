# LIPAS

**LIPAS lets you write ordinary Python agents with an explicit record of what
they decided, spent, and did.** It is a small Python reference implementation
of a claim-based execution model for reliable AI agents.

Start with one assistant. Add only the reliability boundary the application
actually needs.

```text
Agent  = one assistant that thinks and uses tools
@tool  = an explicit capability with a declared side effect
Team   = a durable handoff between named assistants or functions
```

> **0.9.7 public beta.** Ollama, injected-client Anthropic, and OpenAI
> Responses adapters are available, along with durable SQLite sessions, safe
> replay, supervision, and at-least-once Team handoffs.

## The one idea underneath

LIPAS does not ask you to write a graph or a special workflow language. You
write ordinary Python; an `Agent` calls a model and ordinary `@tool` functions.
The runtime records the reliability-relevant parts of that work as immutable
**Claims**.

A **fold** accepts each stable claim once, validates it, and updates small
derived views of the same record: history answers what happened, capability
enforces spend limits, and effects record `intent → result | rejection`.

```text
ordinary Python Agent / Tool / Team
                 │
                 ▼
           append-only Claims
                 ├── history:    decisions and handoffs
                 ├── capability: budgets and spend
                 └── effect:     intent, result, lineage
```

That one record is why the pieces fit together rather than becoming unrelated
features: guards and budgets decide before a call; replay substitutes a
recorded result; supervision records its recommendation; a Team handoff has a
stable causal id; an external write can be reconciled against its recorded
intent. Your code remains natural Python because the runtime records the
boundary around it instead of replacing its control flow.

For the precise guarantees and limits, read the short [Execution
model](docs/execution-model.md).

## Start here

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

`agent.ask(...)` is the normal-script API. In an async application, use
`await agent.run(...)`. The first runnable example is
[`examples/00_playground.py`](examples/00_playground.py).

## When to add more

Keep one Agent when one coherent goal shares one conversation, tool set,
budget, and answer. Multiple steps or multiple tools do not require a Team.

Add a `Team` only when work needs a separate owner or recovery boundary: an
independently restartable task, a different authority/budget, a separately
audited result, or a human/external-operation handoff. A Team member is usually
an Agent, but can be a plain async function. In a normal script:

```python
from lipas import Team


async def researcher(prompt):
    return {"finding": f"researched: {prompt}"}


with Team.open("runs/team.db") as team:
    team.add("research", researcher)
    finding = team.ask_sync("research", "check release risks")
```

## Reliability, only when you ask for it

| Add | LIPAS provides |
|---|---|
| `@tool(side_effect="read_only")` | explicit replay and retry safety class |
| `session="runs/app.db"` | durable trace of intent, result, spend, and decisions |
| `budgets={...}` | pre-flight rejection before a known limit is exceeded |
| `tool_guards=[...]` | recorded policy denial before a live call |
| `OperationJournal` | idempotency-key persistence and reconciliation state for an external write |
| `Team` | durable, at-least-once handoff with leases and acknowledgement |

The record is not a magic memory system and LIPAS is not a graph/workflow DSL.
Your application still owns its domain data, business rules, and user-facing
workflow.

## Try and inspect

The optional CLI is for trying an ordinary Python Agent and inspecting its
session; it is not a second configuration language.

```bash
pip install 'lipas[ollama,cli]'
lipas init support-demo --model gemma4:12b
cd support-demo
lipas chat --factory agent:build_agent
lipas trace runs/chat.db
lipas effects runs/chat.db
```

From a source checkout before installation, use `python -m lipas.cli` instead.
The session file is created automatically. Ollama is local but accessed through
its local HTTP service; a timeout means the local daemon/model did not answer
in time, not that LIPAS contacted the internet.

## Read only what you need

- [Getting started](docs/getting-started.md) — build a small Agent, then add
  replay, a Team handoff, and supervision.
- [Execution model](docs/execution-model.md) — the exact semantics and limits
  of claims, effects, replay, external operations, and Teams.
- [Examples](examples/README.md) — focused, runnable scenarios from the high
  level API down to the lower-level harnesses.
- [Changelog](CHANGELOG.md) — release history.

## License

[Apache License 2.0](LICENSE)
