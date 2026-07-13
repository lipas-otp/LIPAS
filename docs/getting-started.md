# Quick start

> Language: [English](getting-started.md) | [中文](getting-started.zh-CN.md)

This page is intentionally short. Copy it first; read the
[step-by-step tutorial](tutorial.md) when you want to understand why each
piece is there.

## 1. Start a local model

```bash
pip install 'lipas[ollama]'
ollama pull gemma4:12b
```

Make sure the Ollama service is running. LIPAS uses
`http://localhost:11434` by default (or the `OLLAMA_HOST` environment
variable).

## 2. Copy one useful agent

Create `welcome.py`:

```python
from lipas import Agent, tool


@tool(side_effect="read_only")
def welcome_customer(customer_id: str) -> str:
    """Welcome a new customer without changing any customer data."""
    return f"Welcome, {customer_id}!"


with Agent.ollama(
    tools=[welcome_customer],
    instructions="Use welcome_customer for new customers; answer concisely.",
    session="runs/welcome.db",  # omit for an in-memory run
) as agent:
    result = agent.ask("Welcome the new customer Jason.")

    if result.is_error:
        print("agent error:", result.error)
    else:
        print(result.text)
```

Run it with `python welcome.py`.

`Agent.ollama()` supplies the local Ollama adapter and defaults to the
`gemma4:12b` model, so no model name is needed in this first version. The
model receives the tool name, its docstring, and its type-derived input schema;
it decides whether to call the tool. Give it a focused request such as
“Welcome the new customer Jason” rather than only `Jason` when you want a tool
call to be likely.

`session=` writes an inspectable SQLite record of the run. `with` closes that
file cleanly; `agent.close()` is the equivalent when a context manager is not
convenient.

## Next

- Read [LIPAS, step by step](tutorial.md) for the small-book introduction:
  tools, results, sessions, budgets, replay, writes, Skills, and Teams.
- Run [`examples/01_first_agent.py`](../examples/01_first_agent.py) for the
  same shape as a complete module.
- Read the [execution model](execution-model.md) only when you need the exact
  replay, durability, or external-operation guarantees.
