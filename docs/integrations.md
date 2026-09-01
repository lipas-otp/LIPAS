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
ActionGateway when they change product state. An MCP `tools/call` notification
without a JSON-RPC id must provide `_lipas_request_id`; otherwise the server
rejects it because there is no replayable operation identity.

For bounded read retrieval, wrap the same allowlisted client with
`fetch_url_tool`:

```python
from lipas import EgressPolicy, HttpClient, fetch_url_tool

http = HttpClient(
    base_url="https://docs.example.test",
    egress=EgressPolicy(frozenset({"docs.example.test"})),
)
fetch_url = fetch_url_tool(http)
```

The Tool follows the client's HTTPS/host, timeout, and redirect policy and
returns a size-limited visible-text extraction plus a content digest. Search
provider adapters should call this boundary only for URLs returned by their
own allowlisted API and preserve source/citation metadata.

For local retrieval-augmented context, `KnowledgeStore` provides a durable
lexical index without becoming conversation or Claim authority:

```python
from lipas import KnowledgeStore

with KnowledgeStore("knowledge.db") as knowledge:
    knowledge.ingest("docs/guide.md", guide_text, scope="team-a")
    hits = knowledge.search("approval workflow", scope="team-a")
```

Every hit carries its source, chunk number, scope, and document digest. Hosts
should ingest only text they are authorized to expose and keep the returned
citation with the generated answer.

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
must establish the provider outcome and a provider reference before any new
delivery key is chosen; a found result without that reference remains
`uncertain`.

## Remote workers, Web SSE, and attachments

`RemoteWorkerHTTPClient` and `RemoteWorkerHTTPServer` are a provider-neutral
reference transport for hybrid execution. The client requires HTTPS by
default, sends a fenced lease, and signs the worker capability fingerprint
with HMAC-SHA256. The reference server verifies the worker id and attestation
before invoking the worker; it returns a structured result containing events,
checkpoints, and Effect observations. `allow_http=True` is only for local
tests. Production deployments still own TLS certificates, secret rotation,
network policy, and worker admission.

`TLSConfig` validates private-key permissions and exposes a certificate
fingerprint for rotation records. `OperatorServer.reload_tls()` and
`RemoteWorkerHTTPServer.reload_tls()` replace the context used by future
connections without rebinding the listening port; in-flight connections keep
their negotiated session. `RemoteWorkerHTTPClient.reload_tls()` performs the
same future-connection swap for a rotated CA/client trust context. Rotate
certificate and key files atomically in the deployment layer, construct a new
`TLSConfig`, record both fingerprints, then reload and probe the endpoint.

```python
from lipas import RemoteWorkerHTTPClient, RemoteWorkerRunner, WorkerCapabilities

worker = WorkerCapabilities("worker-a", capabilities=frozenset({"code"}))
runner = RemoteWorkerRunner(
    "workspace.db",
    RemoteWorkerHTTPClient(
        "https://worker.example.test",
        worker,
        attestation_secret=secret_from_deployment,
    ),
)
await runner.run(run_id)  # the host still owns claim/heartbeat/settle
```

The local operator exposes reconnectable, cursor-based SSE batches:

```text
GET /api/conversations/<id>/stream?after=0&limit=100
GET /api/runs/<id>/stream?after=0&limit=100
```

Use `Authorization: Bearer <token>` when `require_authentication=True`.
Browser `EventSource` clients that cannot set headers may use the equivalent
short-lived `access_token` query parameter; avoid putting long-lived secrets in
URLs or access logs.
SSE is a catch-up transport rather than an unbounded in-memory stream; the
SQLite conversation/Run event log remains the replay authority. Conversation
attachments are uploaded as bounded JSON base64 payloads, stored under the
workspace with generated names, SHA-256 digests, and idempotent attachment
ids. Filenames are metadata only and cannot escape the workspace.

## Signed extension registry

`ExtensionSigner` creates an HMAC-SHA256 signature over canonical manifest
metadata plus the artifact digest. `ExtensionRegistry` verifies the signature
before certification when a signer is configured; a tampered artifact or
manifest fails closed. `ExtensionRegistryService` provides authenticated POST
registration/revocation and read-only certification metadata under
`/v1/extensions`; it never imports or executes package code. Keep signing
secrets in a deployment secret store and treat certification as admission
metadata, not execution authority.

## Design-partner validation

`run_design_partner_validation()` runs the same bounded cases against a local
fixture or an external adapter (set `evidence_scope="external_adapter"`) and emits a structured report containing run
identity, unsafe-delivery flag, reconciliation time, operator acceptance, and
failure categories. Reports generated locally are marked `local_fixture` and
explicitly require external partner evidence; they must not be presented as
customer validation. After a real partner supplies an acceptance artifact,
record its SHA-256 with `DesignPartnerSignoff.from_file()` and attach it with
`report.with_signoff()`. The report then exposes `externally_accepted=True`
only while the artifact still matches its recorded digest.

For a real-provider durable workflow, use the explicit opt-in helper:

```python
from lipas import run_provider_workflow

evidence = await run_provider_workflow(
    agent,
    runtime.execution,
    "summarize the release notes",
    workspace=project_dir,
    live=True,  # required; this may incur provider cost
    request_id="release-notes-2026-08-30",
)
```

The helper creates one deterministic Task/Run, preserves the normal durable
Agent/Effect path, and returns bounded provider/model/terminal evidence.
`live=True` is intentionally mandatory; local fixtures should use normal
Agent APIs and remain labelled as fixtures.

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
`LangGraphToolAdapter` likewise requires `_lipas_request_id` in its input or a
non-empty configured `run_id`; it never invents a random identity on replay.

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
For a deployment-managed KMS/HSM/secret manager, inject a
`ManagedSecretResolver` with a lookup callback and (when provider-specific
masking is needed) an explicit redactor. Without one, LIPAS uses a bounded
in-memory exact-value redactor. Custom namespaces such as `vault://` are
allowlisted and resolved rather than being passed through as plain strings.
LIPAS still stores only the opaque reference; the callback owns
authentication, rotation, lease/TTL policy, and audit in the external secret
system. This adapter is an integration boundary, not a claim that the local
workspace is a vault.

The OpenAI-compatible adapter can use the same boundary for its provider key:
pass `api_key_reference="secret://..."` and a `SecretResolver` instead of an
inline `api_key`. Resolution happens at adapter construction, the key remains
in memory for the HTTP client, and only the opaque reference should be kept in
deployment configuration.
After an external rotation, call `adapter.reload_api_key()` (or construct a
new Agent) before the next provider request; in-flight requests retain their
already-built header.

Path checks, allowlists, approvals, and redaction prevent accidents; they are
not a security boundary against an adversarial model. The local workbench now
executes commands through Bubblewrap on Linux by default: `auto` and `bwrap`
fail closed if filesystem and network namespace isolation cannot be established.
Use `--sandbox local` only as an explicit unsafe fallback for trusted code.
Bubblewrap covers first-party workbench commands; arbitrary Python tools used
through the Action Gateway still need their own isolated capability, container,
or remote sandbox.
