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
    runtime.handoffs    # legacy mailbox compatibility boundary
    runtime.sessions    # optimistic conversation snapshots
    runtime.artifacts   # product artifact repository
    coordinator = runtime.coordinator()  # ExecutionStore-backed handoffs
```

`ExecutionStore` remains authoritative for durable Task/Run/Interrupt control.
`workspace.db` is the only global product database opened by the composition
root. Callers no longer construct, select paths for, or close a set of loosely
related stores themselves. Run evidence stays in
`runs/<run-id>/claims.db`; this preserves budget, replay, and single-writer
hotspot isolation without creating another Run state machine. All normal
connections share the 0.40 SQLite WAL, timeout, transaction, and failure
policy. Claim tapes coordinate concurrent connections while remaining bounded
by SQLite's one physical writer; details are in
[SQLite storage and concurrency](sqlite-storage.md).

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
  synchronous tools executed by the bounded context-propagating executor.
- `AgentEvent`: ordered provider-neutral run/model/tool events. `Agent.stream`,
  `Session`, and `RunHandle` emit this protocol. Durable events are persisted
  by `ExecutionStore` and can be fetched after a cursor.
- `Session`: explicit conversation state. `SQLiteSessionStore` persists named
  snapshots with optimistic version checks.
- `RunHandle`: one running Session call with `result()`, `events()`, and
  cooperative `cancel()`.

`AgentCoordinator` extends the same contract to named members. Each handoff is
a deterministic ExecutionStore Run with a branch `RunContext`, lease heartbeat,
persisted cancellation, terminal replay, and handoff lifecycle events. The
member registry and policies are application composition, not another durable
state machine. See [Multi-Agent coordination](multi-agent.md).

`coordinator.event_handle(coordination_id)` provides bounded reconnectable
aggregate pages over the per-Run `AgentEvent` streams. `SharedBudgetPolicy`
reserves one hard shared pool atomically before a handoff claim, while
`CapabilityPolicy` checks host-declared member capabilities at registration;
neither policy grants authority to Skills or Memory.

The 0.40 operator keeps this boundary: `LocalWebOperator` is a thin local HTTP
projection of the same Tasks, Runs, Interrupts, and event cursors. It never
returns lease tokens, binds to loopback by default, and requires an explicit
bearer token for mutations. Runtime-created operators may additionally project
bounded Workbench task detail (product events, artifacts, ChangeSet diff state,
and reports), but those remain Workbench projections rather than a second
authority. The root browser page and `/api/runs/<id>/events` are thin polling
clients over the same bounded cursor contract. `FaultPlan`/`FaultCampaign`,
`run_fault_matrix()`, and `benchmark_execution_store()` are bounded
fault/measurement helpers; they do not create a queue, metrics database, or
retry policy.

For durable reconnects, pass `event_sink=` and the last acknowledged
`event_cursor=` to `run_durable` or `resume_durable`. Persistence is
authoritative: a failing event sink does not change the Run outcome.
`LIPASRuntime.run_durable()` and `resume_durable()` create a Run-scoped
ExecutionStore evidence attachment. The composition-root Workbench control
store remains stable, so unrelated durable calls may run concurrently without
closing or redirecting one another's audit sink. SQLite still serializes their
brief control commits.

Direct Workbench embeddings use the same ownership boundary:

```python
with workbench.execution_scope(agent.rowset, run_id=run.id) as execution:
    result = await agent.run_durable(
        task.goal, execution_store=execution, run_id=run.id,
    )
```

The scope owns and closes only its temporary connection; it never replaces
`workbench.execution`.

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
- `AgentCoordinator` composes deterministic handoff Runs under the existing
  `ExecutionStore`; it owns no mailbox or graph authority.
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
