# LIPAS architecture at a glance

> Language: [English](architecture.md) | [中文](architecture.zh-CN.md)

This page is the short map of the repository. It explains where a request
goes and which component owns each kind of state. For exact recovery and
replay guarantees, continue to the [execution model](execution-model.md).

## The request path

```text
User / CLI / Web / Python host
             │
             ▼
Conversation or direct prompt       (optional product entry point)
             │
             ▼
Agent ── adapter + instructions + Skills + Tools
             │
             ▼
Preflight: guard → budget → side-effect policy → approval
             │
             ▼
Effect intent (durable before live work)
             │
             ├── model adapter
             ├── Tool / Harness
             ├── sandbox / workspace capability
             └── connector / external provider
             │
             ▼
Observation or typed rejection
             │
             ▼
Artifact / report / replay / delivery
```

The runtime records the intent before invoking a model or tool. An intent
without an observation is an orphan and remains recoverable/uncertain; it is
never silently treated as success. A Skill only changes instructions. A Tool
is the executable capability, and its `side_effect` declaration is part of
the admission policy.

## Module ownership

| Layer | Main modules | Owns |
| --- | --- | --- |
| Entry points | `cli.py`, `conversation.py`, `operator.py` | User-facing commands, chat, and local Web projection |
| Agent loop | `agent.py`, `behaviour.py`, `react.py`, `adapter/` | Reason/act/observe and provider-neutral messages |
| Admission and evidence | `tools.py`, `guard.py`, `effect.py`, `harness.py`, `tool_harness.py` | Side effects, preflight decisions, intent/result claims |
| Durable control | `execution.py`, `durable.py`, `dispatcher.py` | Task/Run leases, checkpoints, cancellation, Interrupts |
| Product workspace | `workbench.py`, `workspace_storage.py` | Workspace policy, ChangeSet, Artifact, Verification, Report |
| External boundaries | `operations.py`, `http_client.py`, `email.py`, `gateway.py` | Idempotency, uncertain outcomes, reconciliation |
| Collaboration | `coordination.py`, `coordination_policy.py`, `orchestration.py` | Named ownership, handoffs, bounded parallel work, legacy mailbox |
| Domain guidance | `skills.py`, `scenarios.py`, `builtin_skills/` | Portable instructions and declarative capability requirements |
| Persistence | `serialization/`, `sqlite_storage.py`, `conversation_store.py` | Claim tape, projections, conversations, SQLite policy |
| Bounded capabilities | `document_tools.py`, `code_tools.py`, `archive_tools.py`, `web_tools.py`, `knowledge.py` | Parsing, computation, retrieval; no authority or approval logic |

The bottom capability modules are deliberately small and dependency-optional.
They accept already-authorized input. `Workbench` supplies path policy,
staging, approval, and evidence, so those concerns do not get duplicated in
each parser or calculator.

The CLI has two intentionally different tool bundles: `chat --workspace` is
read-only and lightweight for conversation, while `task` obtains the full
Workbench bundle (writes, verification, staging, and artifacts). Keeping this
split explicit prevents a chat prompt from silently gaining task authority.

## Which store is authoritative?

| State | Authority | What the Claim tape does |
| --- | --- | --- |
| Model/tool intent, result, spend, replay | Agent session (`Claim`/`Effect`) | Durable evidence and projections |
| Task, Run, lease, checkpoint, approval | `ExecutionStore` | Emits a repairable audit mirror when attached to a RowSet |
| External write status | `OperationJournal` | Records causal evidence and reconciliation history |
| Workspace files and staged delivery | Filesystem + `Workbench` tables | Artifacts and reports point to exact paths/digests |
| Conversation/message identity | `SessionStore` / `conversation_store.py` | Projects user-visible history and promotion events |

Do not create a second Task/Run authority in an adapter or integration. A
LangGraph/AutoGen/MCP bridge should translate into the same Agent, Tool,
Effect, or coordinator contracts. The legacy `Team` mailbox remains only for
existing mailbox applications.

## Choosing an entry point

1. Use `Agent` for one coherent goal, even when it calls several tools.
2. Add a persistent session when you need replayable history or budgets.
3. Add `ExecutionStore` and `run_durable()` when the same run must survive
   approval, cancellation, or process loss.
4. Use `Workbench` for local files, staged changes, verification, and reports.
5. Use `OperationJournal` for an external write that has an idempotency key
   and a provider reconciliation API.
6. Use `AgentCoordinator` only when work needs a separate owner or restart
   boundary; use `Team` only for its legacy mailbox contract.

The repository's numbered [examples](../examples/README.md) follow this same
order, from a single Agent to a staged local task and external connectors.
