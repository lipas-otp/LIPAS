# Getting started with LIPAS

This tutorial builds a small support agent as ordinary Python: one read-only
tool, a durable audit session, a replay, and a mailbox handoff. LIPAS supplies
the reliability boundary; your application remains normal Python code.

## 1. Install and start a local model

```bash
pip install 'lipas[ollama]'
ollama serve
ollama pull qwen2.5
```

Use `pip install 'lipas[openai]'` when constructing an OpenAI Responses
adapter instead. The core package has no mandatory provider SDK.

## 2. Write a tool and agent

Create `support.py`:

```python
import asyncio

from lipas import Agent
from lipas.adapter import OllamaAdapter
from lipas.tools import SideEffectClass, tool


@tool(side_effect=SideEffectClass.READ_ONLY)
def lookup_customer(customer_id: str) -> str:
    """Look up a customer record."""
    return {"C-42": "Ada Lovelace"}.get(customer_id, "not found")


async def main() -> None:
    agent = Agent(
        adapter=OllamaAdapter(),
        model="qwen2.5",
        instructions="Use lookup_customer when a customer id is given.",
        tools=[lookup_customer],
        session_path="runs/support.db",
        budgets={"tool_calls": 10, "tokens_out": 2_000},
    )
    try:
        result = await agent("Who is customer C-42?")
        print(result.text)
    finally:
        agent.close()


asyncio.run(main())
```

Run it with `python support.py`. The decorator is intentionally explicit:
`PURE`, `READ_ONLY`, `IDEMPOTENT_WRITE`, and `EXTERNAL_WRITE` have different
replay and safety rules.

## 3. Inspect and replay the run

The SQLite file contains the claim tape. You can inspect it directly from
Python:

```python
from lipas import open_session, render_trace

rowset = open_session("runs/support.db")
try:
    print(render_trace(rowset.store))
finally:
    rowset.store.close()
```

For replay, LIPAS defaults to strict tape substitution: recorded LLM replies
and tool results are used without contacting the live model or tool provider.

```python
from lipas import replay

with replay("runs/support.db") as run:
    # Build an Agent/LLM using run.rowset, run.replay_cursor, and
    # run.tool_replayer. See examples/06_react_replay.py for complete wiring.
    pass
```

Use live reroute only deliberately. An `EXTERNAL_WRITE` is refused unless you
explicitly opt in; replay safety is not a claim of exactly-once delivery.

## 4. Add a second worker without a workflow DSL

`AgentCell` adapts an ordinary async function to the durable mailbox. A
message is leased while it is handled, acknowledged on success, released on
failure, and reclaimable after lease expiry.

```python
from lipas import AgentCell, AgentOrchestrator, Mailbox


async def researcher(prompt):
    return {"finding": f"research complete: {prompt}"}


mailbox = Mailbox("runs/team.db")
team = AgentOrchestrator(mailbox)
cell = AgentCell("researcher", researcher)
team.register(cell.name, cell.handle)

result = await team.handoff(
    sender="planner",
    recipient="researcher",
    payload={"prompt": "check release risks"},
    message_id="release-risk-001",
)
print(result)
```

Treat `message_id` as the idempotency/replay key when the worker initiates an
external operation. Delivery is at-least-once by design.

## 5. Add supervision when policy matters

The default agent can take a `supervisor_policy`. Policies observe its audited
effects and can emit retry, terminate, or human-escalation recommendations.

```python
from lipas.supervisor import Policy, PolicyRule, TerminateAction

policy = Policy.of(
    PolicyRule("demo_stop", lambda view, ctx: TerminateAction("review")),
)

agent = Agent(..., supervisor_policy=policy)
```

Use policies for concrete operational rules: spend ceilings, repeated tool
failures, required approval, or a known uncertain external operation. Keep
the business logic in Python; LIPAS records why it was permitted, denied,
halted, or escalated.
