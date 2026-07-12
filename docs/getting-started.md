# Getting started with LIPAS

This tutorial builds a small support agent as ordinary Python, gives it a
durable audit session, and then shows the one reason to add a Team. LIPAS
supplies the reliability boundary; your application remains normal Python code.

## 1. Install and start a local model

```bash
pip install 'lipas[ollama,cli]'
ollama serve
ollama pull gemma4:12b
```

Use `pip install 'lipas[openai]'` when constructing an OpenAI Responses
adapter instead. The core package has no mandatory provider SDK; the `cli`
extra supplies the optional interactive line editor used by `lipas chat`.

## 2. Write a tool and agent

Create `support.py`:

```python
from lipas import Agent, tool


@tool(side_effect="read_only")
def lookup_customer(customer_id: str) -> str:
    """Look up a customer record."""
    return {"C-42": "Ada Lovelace"}.get(customer_id, "not found")


def main() -> None:
    with Agent.ollama(
        "gemma4:12b",
        instructions="Use lookup_customer when a customer id is given.",
        tools=[lookup_customer],
        session="runs/support.db",
        # A request's maximum output must fit inside its hard output budget.
        max_tokens=600,
        budgets={"tool_calls": 10, "tokens_out": 1_800},
    ) as agent:
        result = agent.ask("Who is customer C-42?")
        if result.is_error:
            print("agent error:", result.error)
        else:
            print(result.text)


main()
```

Run it with `python support.py`. The decorator is intentionally explicit:
`PURE`, `READ_ONLY`, `IDEMPOTENT_WRITE`, and `EXTERNAL_WRITE` have different
replay and safety rules.

### Optional: reuse guidance with a Skill

A Skill is a small portable `SKILL.md` file. It teaches an Agent how to
approach recurring work; it does **not** grant a capability. Put this file at
`skills/support-triage/SKILL.md`:

```markdown
---
name: support-triage
description: Diagnose support requests safely.
---
Look up facts before answering. Never expose secrets. Say when a human
approval or a write action is required. End with one concrete next step.
```

Then add one ordinary keyword to the Agent constructor:

```python
Agent.ollama(
    tools=[lookup_customer],
    skills="skills/support-triage",
    # ...the other arguments from the previous example...
)
```

The Skill directory is loaded when the Agent is built. The repository includes
larger ready-to-copy [research, support, daily-brief, and external-action
Skills](../examples/skills/).

## 3. Inspect the run

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

Replay is deliberately not hidden behind a convenient “run it again” button.
The default policy substitutes recorded model replies and tool results without
contacting live systems; live rerouting is explicit. See the [Execution
model](execution-model.md#replay-reproduce-decisions-without-accidentally-repeating-effects)
and the concise, provider-free `examples/06_strict_replay.py` when you need
that boundary.

## 4. Add a team member without a workflow DSL

`Team` is the small convenience entry point that adapts an ordinary async
function or an Agent to a durable mailbox. A message is leased while handled,
acknowledged on success, released on failure, and reclaimable after lease
expiry.

```python
from lipas import Team


async def researcher(prompt):
    return {"finding": f"research complete: {prompt}"}


with Team.open("runs/team.db") as team:
    team.add("researcher", researcher)
    result = team.ask_sync(
        "researcher",
        "check release risks",
        sender="planner",
        message_id="release-risk-001",
    )
    print(result)
```

Treat `message_id` as the idempotency/replay key when the member initiates an
external operation. Delivery is at-least-once by design. In an async service,
use `await team.ask(...)` instead.

## Next only when the need appears

- Add `budgets={...}` or `tool_guards=[...]` to the Agent when a call must be
  limited or denied before it reaches a provider.
- Add `supervisor_policy=...` for a concrete termination or escalation rule;
  see `examples/07_supervision.py`.
- Use `OperationJournal` only around an external write with a provider
  idempotency key; see `examples/09_external_operation.py`.
