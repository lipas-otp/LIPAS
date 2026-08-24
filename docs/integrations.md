# Capability clients and experimental interoperability adapters

The HTTP and MCP clients in this guide are first-party capability boundaries.
The LangGraph, AutoGen, OpenClaw, and MCP-server bridges below are compatibility
samples, not core LIPAS product surfaces, and may change without compatibility
guarantees.

LIPAS prioritizes its own local task-agent experience. These entry points are
kept only for users who already have a concrete interoperability need. Every
entry point uses the same `ActionGateway`, so experimental adapters do not
create separate execution semantics inside the product.

## First-party HTTP and MCP clients

Use the runtime clients when a Scenario needs a real external capability. They
are provider-neutral and do not silently retry an ambiguous write:

```python
from lipas import EgressPolicy, HttpClient, OperationJournal
from lipas.integrations import MCPClient, MCPHttpClient

http = HttpClient(
    base_url="https://api.example.test/v1",
    egress=EgressPolicy(frozenset({"api.example.test"})),
    journal=OperationJournal("operations.db"),
)
response = await http.request(
    "POST", "messages", json_body={"text": "hello"},
    idempotency_key="ticket-42-message-1",
)

mcp = MCPClient(MCPHttpClient("https://mcp.example.test/mcp"))
await mcp.initialize()
tools = await mcp.list_tools()
result = await mcp.call_tool(
    "lookup_ticket", {"id": "42"}, request_id="ticket-42-lookup-1",
)
```

HTTP writes persist `prepared` before the request and become `uncertain` on
transport/timeout failure. Reconcile through the same `OperationJournal` (or
`/api/operations/<key>/reconcile`) before submitting a new key. MCP session
state is transport state only; tool effects still pass through the host's
ActionGateway when they change product state.

## Idempotent email delivery

Email delivery is a connector boundary, not an Agent type. A provider must
return a provider reference and implement lookup by idempotency key:

```python
from lipas import EmailConnector, EmailMessage, OperationJournal

connector = EmailConnector(provider, OperationJournal("operations.db"))
operation = connector.send(
    EmailMessage(
        sender="bot@example.test",
        recipients=("owner@example.test",),
        subject="Draft ready",
        text="Please review the staged draft.",
    ),
    idempotency_key="draft-42-delivery-1",
    approved=True,  # after the operator reviewed recipient/scope/preview
)
```

The connector refuses a pending or uncertain key. `connector.reconcile(key)`
must establish the provider outcome before any new delivery key is chosen.

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
failure. Async gateway calls keep the isolated invocation alive so a late
completion can converge the Effect; a sync tool that cannot be force-killed
can be closed explicitly with `ToolHarness.reconcile_orphan()` after an
operator/provider lookup.

## LangGraph

```python
from lipas import ActionGateway
from lipas.integrations import (
    LangGraphActionNode,
    LangGraphHandoffNode,
    LangGraphToolAdapter,
)

gateway = ActionGateway(build_tools(), session="actions.db")
execute = LangGraphActionNode(gateway, approved=True)  # after an interrupt node
tool = LangGraphToolAdapter(gateway, "save_note", approved=True)
langchain_tool = tool.as_langchain_tool()  # only this needs langchain-core
```

`state["action"]` for the node must contain `tool_name`, `arguments`, and a
stable `request_id`.

For a LangGraph node that delegates to a named LIPAS Agent member, use
`LangGraphHandoffNode`. Pass a stable checkpoint id in `configurable`; the node
returns the durable Run id and replay flag without importing LangGraph state
into LIPAS:

```python
from lipas import LIPASRuntime

with LIPASRuntime.open(".lipas") as runtime:
    node = LangGraphHandoffNode(runtime.coordinator(), "reviewer")
    state = await node(
        {"input": "review this", "coordination_id": "thread-42"},
        {"configurable": {"checkpoint_id": "checkpoint-7"}},
    )
```

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

## AutoGen

`AutoGenHandoffHandler` treats one AutoGen conversation message as a durable
LIPAS handoff. `AutoGenToolAdapter` exposes an audited ActionGateway tool with
`run`/`arun` methods. Both require a stable request id; they do not create a
second team or conversation authority:

```python
from lipas.integrations import AutoGenHandoffHandler

handler = AutoGenHandoffHandler(runtime.coordinator(), "reviewer")
result = await handler.handle(
    "review this", conversation_id="thread-42", request_id="message-7",
)
```

Both handoff adapters treat the host's thread/checkpoint/message id as the
replay identity. They never generate a random handoff id, import a framework
team/graph state model, or bypass LIPAS approval, cancellation, budget, and
audit rules. Re-register the same member contract version after a restart;
changing its meaning requires a new handoff identity.

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
