# Experimental interoperability adapters

> **Experimental:** these adapters are compatibility samples, not core LIPAS
> product surfaces. They may change or move without compatibility guarantees.

LIPAS prioritizes its own local task-agent experience. These entry points are
kept only for users who already have a concrete interoperability need. Every
entry point uses the same `ActionGateway`, so experimental adapters do not
create separate execution semantics inside the product.

## Define actions

```python
# actions.py
from lipas import tool

@tool(side_effect="idempotent_write")
def save_note(path: str, text: str) -> dict[str, str]:
    """Save one note after the host has approved the write."""
    ...

def build_tools():
    return [save_note]
```

Exercise the gateway directly:

```bash
lipas action call \
  --factory actions:build_tools \
  --session ~/.lipas/actions.db \
  --tool save_note \
  --arguments '{"path":"notes/a.md","text":"hello"}' \
  --request-id task-42-save-1 \
  --approved
```

Redelivering the same request id and arguments restores the recorded result;
reusing that id with different arguments fails. Sync tools run in a thread so
they do not block heartbeats. A thread cannot be safely killed, so timeout
returns `uncertain` with an orphan intent instead of fabricating a terminal
failure.

## LangGraph

```python
from lipas import ActionGateway
from lipas.integrations import LangGraphActionNode, LangGraphToolAdapter

gateway = ActionGateway(build_tools(), session="actions.db")
execute = LangGraphActionNode(gateway, approved=True)  # after an interrupt node
tool = LangGraphToolAdapter(gateway, "save_note", approved=True)
langchain_tool = tool.as_langchain_tool()  # only this needs langchain-core
```

`state["action"]` for the node must contain `tool_name`, `arguments`, and a
stable `request_id`.

## Hermes MCP

Run the dependency-free standard stdio MCP server:

```bash
lipas mcp serve \
  --factory actions:build_tools \
  --session ~/.lipas/hermes-actions.db
```

Register that command as a Hermes stdio MCP server. Writes fail closed by
default. Add `--allow-writes` only when the trusted Hermes host has already
performed per-call approval. The flag grants write authority to the whole
server; it is not an OS sandbox.

## OpenCrew / OpenClaw

```bash
lipas action openclaw \
  --factory actions:build_tools \
  --session ~/.lipas/opencrew-actions.db \
  --payload '{"task_id":"thread-42","request_id":"thread-42-save-1",\
"tool_name":"save_note","arguments":{"path":"notes/a.md","text":"hello"},\
"approved":true}' \
  --trust-caller-approval
```

Trust caller approval only when the host is authenticated and the model cannot
forge `approved`. The response includes Effect identity and OpenCrew closeout
fields for safe redelivery and reconciliation.

## Secret and isolation boundary

The Action Gateway rejects common raw secrets before writing any Claim. Pass
an opaque reference and resolve it only at tool execution time. The built-in
environment resolver is deliberately allowlisted:

```python
from lipas import ActionGateway, EnvironmentSecretResolver

gateway = ActionGateway(
    build_tools(),
    session="actions.db",
    secret_resolver=EnvironmentSecretResolver(["CUSTOMER_API_KEY"]),
)
# Tool arguments may now contain secret://env/CUSTOMER_API_KEY.
```

The Effect intent retains the reference, not the resolved value. Exact resolved
values are redacted from returned values and exceptions before persistence.
LIPAS does not yet provide a general secret vault or arbitrary secret-provider
plugins.

Path checks, allowlists, approvals, and redaction prevent accidents; they are
not a security boundary against an adversarial model. The local workbench now
executes commands through Bubblewrap on Linux by default: `auto` and `bwrap`
fail closed if filesystem and network namespace isolation cannot be established.
Use `--sandbox local` only as an explicit unsafe fallback for trusted code.
Bubblewrap covers first-party workbench commands; arbitrary Python tools used
through the Action Gateway still need their own isolated capability, container,
or remote sandbox.
