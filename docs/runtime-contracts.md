# Unified runtime contracts

> Language: [English](runtime-contracts.md) | [中文](runtime-contracts.zh-CN.md)

LIPAS gives applications one composition root, one public lifecycle
vocabulary, and one versioned global database. Schema v2 co-locates compatible
control, product, operation, handoff, conversation, and evidence tables while
preserving one isolated Claim/Effect tape per Run.

```python
from lipas import LIPASRuntime

with LIPASRuntime.open(".lipas") as runtime:
    runtime.execution   # authoritative Task / Run state machine
    runtime.claims      # audit evidence and projections
    runtime.operations  # idempotent external-operation boundary
    runtime.handoffs    # optional at-least-once coordination boundary
    runtime.sessions    # optimistic conversation snapshots
    runtime.artifacts   # product artifact repository
```

`ExecutionStore` remains authoritative for durable Task/Run/Interrupt control.
`workspace.db` is the only global product database opened by the composition
root. Callers no longer construct, select paths for, or close a set of loosely
related stores themselves. Run evidence stays in
`runs/<run-id>/claims.db`; this preserves budget, replay, and single-writer
isolation without creating another Run state machine.

## Storage migration and diagnostics

Opening a legacy workspace never mutates it implicitly:

```bash
lipas migrate plan --home .lipas
lipas migrate apply --home .lipas --yes
lipas migrate verify --home .lipas
lipas doctor --home .lipas
lipas audit --home .lipas
```

Migration takes SQLite-consistent backups, assembles a temporary target,
checks source/target row counts, SQLite integrity, foreign keys, event cursors,
interrupt state, and evidence path containment, then atomically activates
`workspace.db`. Original v1 files remain untouched. `rollback --yes` preserves
the v2 database in a backup before returning to those retained files; it does
not pretend that v2-only writes can be represented in v1. Runtime instances
hold a shared workspace lease; migration and rollback require the exclusive
lease and refuse active workers or SQLite writers. Rollback checkpoints WAL
and verifies its SQLite backup before deactivating schema v2. Dead-PID
migration locks are diagnosed and safely recovered; live locks are retained.

`doctor` performs a bounded launch probe of the default OS sandbox and reports
storage health separately from complete runtime readiness. Discovering a
binary on `PATH` is not treated as proof that the isolation works.

## One invocation contract

Every ordinary invocation, conversational turn, and durable Run can use the
same concepts:

- `RunContext`: stable run id, cooperative cancellation token, and optional
  absolute monotonic deadline. The deadline spans model and tool phases; it is
  not restarted for each phase. `current_run_context()` exposes the context to
  tool code without adding model-visible schema parameters, including inside
  synchronous tools executed with `asyncio.to_thread`.
- `AgentEvent`: ordered provider-neutral run/model/tool events. `Agent.stream`,
  `Session`, and `RunHandle` emit this protocol. Durable events are persisted
  by `ExecutionStore` and can be fetched after a cursor.
- `Session`: explicit conversation state. `SQLiteSessionStore` persists named
  snapshots with optimistic version checks.
- `RunHandle`: one running Session call with `result()`, `events()`, and
  cooperative `cancel()`.

For durable reconnects, pass `event_sink=` and the last acknowledged
`event_cursor=` to `run_durable` or `resume_durable`. Persistence is
authoritative: a failing event sink does not change the Run outcome.
`LIPASRuntime.run_durable()` and `resume_durable()` serialize their convenience
calls because one composition-root Workbench owns one mutable audit
attachment. Concurrent workers use separate Workbench views over the same
authoritative database, as the built-in dispatcher does.

## Input is not approval

`InputPolicy` and `ApprovalPolicy` can both suspend a durable Run but answer
different questions. An input interrupt supplies missing information and its
response becomes exactly one tool result; the tool body is not executed.
Approval permits exactly one pending capability call. Resolving an input can
never authorize that or a later write.

## Honest model capabilities

`ModelCapabilities` uses `None` for unknown support. `ModelRequirements` turns
selected capabilities into an explicit startup gate, and
`ModelCapabilityReport` explains every mismatch. The current Anthropic and
Ollama adapters are correctly advertised as single-shot (`streaming=False`)
even though their providers may offer streaming through other integrations.
No validation path silently swaps models or degrades a requested capability.

The generic Chat Completions adapter uses provider names
`openai-compatible` (single terminal response) and
`openai-compatible-stream` (real SSE). Only that configured streaming mode is
declared true or false. Tool calling, structured output, reasoning, context
length, and locality remain unknown until an application registers the exact
provider/model route it has tested. Vision is explicitly false because this
adapter currently accepts only text/tool message blocks.

## Observer boundary

`RunObserver` receives a frozen `RunSnapshot` and `RunContext` and may return a
`Recommendation`. Recommendations are recorded as evidence and emitted as
events, but are advisory by default. Set
`honor_observer_recommendations=True` only when the host explicitly wants the
ReAct behaviour to map `terminate` or `escalate` recommendations to terminal
results. Existing Supervisor policies remain compatible while applications
migrate away from ReAct-specific supervision.

## Authority boundaries

- Skills are instructions, not capabilities.
- Conversation or future memory state is context, not replay or approval
  authority.
- Claims and Effects are audit evidence.
- Tools are the only executable capabilities.
- The legacy `Team`/`Mailbox` surface remains available but is now a
  compatibility orchestration layer, not a second identity for core Runs.
- `StrategyRegistry` and belief-adaptive calculus remain supported for
  advanced/experimental projections. Core Run, Interrupt, event, and operation
  control use fixed reducers and explicit state machines.

`lipas audit` is read-only by default and checks storage invariants; its JSON
explicitly marks Claim lint as `not_run` instead of presenting an empty list as
a completed check. `LIPASRuntime.audit(repair=True)` and `lipas audit --repair`
repair recoverable audit outboxes and run the persistent Claim lint. They do
so for both global evidence and every registered Run tape, and do not invent
missing external outcomes.
