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
connections share the 0.63 SQLite WAL, timeout, transaction, and failure
policy. Claim tapes coordinate concurrent connections while remaining bounded
by SQLite's one physical writer; details are in
[SQLite storage and concurrency](sqlite-storage.md).

## Storage migration and diagnostics

Opening a legacy workspace never mutates it implicitly:

```bash
lipas install --home .lipas
lipas release check --home .lipas
lipas upgrade --home .lipas
lipas backup --home .lipas --destination /safe/lipas.db
lipas restore --home .lipas --source /safe/lipas.db --yes
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

The installation manifest is non-secret metadata. Workspace, database, runs,
manifest, and local secret files are permission-hardened on POSIX; an
installation fails readiness if group/other access is exposed. Use
`FileSecretResolver` for atomic `secret://file/NAME` rotation and `TLSConfig`
for TLS 1.2+ operator/worker endpoints. Non-loopback servers require both TLS
and authentication.

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

`LIPASRuntime.execute_workflow()` runs a `CompiledWorkflow` as one
lease-fenced Task/Run and records step lifecycle events. The callback is
host-owned; any world-changing action must still pass through the normal
`EffectProposal`/Harness bridge so workflow execution cannot become a second
authority. Hosts may provide a cooperative `cancel_check`; cancellation is
returned as a distinct `cancelled` workflow result and durably settles the
underlying Run as `CANCELLED` (rather than being reported as an ordinary
callback failure).

### Release 0.41 contract: conversation kernel

The conversation kernel extends the same store with explicit `Conversation`,
`Message`, and `ConversationEvent` resources. A Message has a stable
caller-visible identity; `LIPASRuntime.promote_message_to_task()` uses it to
derive one deterministic Task/Run link and is safe to retry. Conversation
events have a per-conversation sequence and catch-up cursor. ExecutionStore
remains authoritative for control transitions.

### Release 0.42 contract: local Web operator

The local Web operator projects the 0.41 resources under
`/api/conversations`, with cursor-based SSE catch-up, authenticated
streams/mutations, and bounded content-addressed attachments. Execution
AgentEvents and Interrupts are projected by event identity; the persisted event
log remains the replay authority.

### Release 0.43 contract: remote Worker

`RemoteWorkerRunner` and the HTTPS-gated `RemoteWorkerHTTPClient`/
`RemoteWorkerHTTPServer` add capability declaration, HMAC attestation, lease
heartbeat, attempt fencing, and explicit complete/fail transitions around
`ExecutionStore`; they do not provide a second queue. TLS and certificate/key
custody remain deployment concerns.

### Release 0.44 contract: shared workspace policy

`WorkspaceIdentity` and `ApprovalDelegation` are host policy values. The
policy store is durable and revocable, but it does not resolve an Interrupt or
create authority outside the host-owned Run.

### Release 0.45 contract: connector recovery

Connector descriptors, provider request identity, and `RateLimitPolicy` make
egress capability and local throttling explicit. HTTP, MCP, and Email writes
use the OperationJournal timeout → uncertain → reconcile contract; policy does
not replace reconciliation.

### Release 0.46 contract: Plan/Handoff boundary

`AgentPlan` and `PlanStep` are plan/handoff envelopes. External graph or team
state stays outside LIPAS and is hosted as one scoped Run rather than a second
scheduler.

### Release 0.47 contract: observability projection

`measure_execution()` is a bounded metrics/SLO projection over the existing
store. It exposes cost, latency, replay, and failure evidence without making a
metrics database authoritative.

### Release 0.48 contract: signed extension registry

`ExtensionSigner` and `ExtensionRegistryService` verify and publish provenance
and certification metadata without importing third-party code. Package
installation and tenancy remain explicit deployment concerns.

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

The 0.43 worker boundary may return a `RemoteExecutionResult`. Its event
identities are logical action identities (not worker-attempt identities), so a
redelivery on another worker is idempotent. Checkpoints are saved with the
current lease and version, and remote `EffectObservation` values are projected
as durable Run events before terminal completion. The reference transport
verifies the worker capability attestation and rejects a mismatched worker,
task, or expired lease before invocation.

For shared workspaces, `WorkspacePolicyStore` stores immutable identity
contracts, bounded `ApprovalDelegation` grants, revocation, and policy audit in
the same SQLite authority. It does not resolve an Interrupt by itself. Connector
descriptors and `RateLimitPolicy` make egress capability and local throttling
explicit; they do not replace `OperationJournal` reconciliation.

`ExternalRunEnvelope`/`AgentCoordinator.execute_external()` is the framework
boundary for LangGraph, AutoGen, or another workflow host. It creates one
deterministic LIPAS Task/Run, propagates the RunContext deadline/cancellation,
renews the lease, and records terminal evidence. External graph/team state
remains outside LIPAS and cannot create a parallel scheduler.

The 0.40 operator keeps this boundary: `LocalWebOperator` is a thin local HTTP
projection of the same Tasks, Runs, Interrupts, and event cursors. It never
returns lease tokens, binds to loopback by default, and requires an explicit
bearer token for mutations (and streams when configured). Runtime-created operators may additionally project
bounded Workbench task detail (product events, artifacts, ChangeSet diff state,
and reports), but those remain Workbench projections rather than a second
authority. The root browser page uses SSE catch-up with bounded polling
fallback, and `/api/runs/<id>/events` remains a thin cursor projection.
`FaultPlan`/`FaultCampaign`,
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

## Release 0.49 contract: backup and restore

Workspace backup uses copy-on-write SQLite snapshots behind the workspace
lease. The snapshot is integrity-checked before it is accepted, and restore
cannot bypass the same lease or schema-version checks. Installer UX,
compatibility policy, and rollback drills are release evidence rather than a
new authority.

## 0.50 Runtime Semantics façade

The 0.50 public façade is `AgentRuntime`, a thin product-facing name over
`LIPASRuntime`. Its `decide_effect()` method evaluates an `EffectProposal`
against host-declared capabilities, the remaining budget, and approval state.
`execute_effect()` passes that decision into a matching existing Harness and
returns an `EffectObservation` projected from its durable Claim tape. The
Harness records proposal identity/provenance on the concrete LLM or Tool
intent; it remains the only component that performs the call. A proposal is
therefore an intent, not proof that anything happened. Repeated proposal
identity recovers a terminal result without a second call, while an
intent-only Effect projects as `uncertain` and must enter reconciliation before
an external write is retried. Proposal metadata is namespaced and `caused_by`
is retained as an explicit causal link; reconciliation accepts either the
product proposal id or its mapped claim id.
Reusing that identity with changed actor, risk, capability, causal, or metadata
fields fails closed rather than silently returning the old result.

Product paths should use `LIPASRuntime.execute_effect_for_run()` when a Run is
known. It clones the Harness configuration onto that Run's isolated durable
Claim tape, preventing a convenient in-memory store from being mistaken for
the Runtime's authoritative evidence. The lower-level `execute_effect()` is
kept for compatibility and intentionally documents that the caller owns its
Harness evidence sink.

The same identity rule applies to direct gateway calls: a pending approval is
bound to its tool, argument digest, and causal parent. HTTP connector request
identity is explicit and separate from the operation idempotency key; a write
redirect is uncertain and requires reconciliation. `SLOReport` reports an
empty terminal sample as not healthy rather than treating missing evidence as
success. `run_design_partner_validation()` normalizes local fixtures and
external adapters into the same evidence shape (run identity, unsafe delivery,
reconciliation time, operator acceptance, and failure categories). A local
report is marked `local_fixture` and cannot satisfy the external partner gate.

## 0.51 bounded workflow compiler

`AutonomousWorkflowCompiler` turns a `WorkflowGoal` (a goal, strict-JSON
constraints, workspace, and adaptive-step bound) plus optional declared steps
into a deterministic `CompiledWorkflow`. Each `WorkflowStep` is explicitly
`fixed` or `adaptive`; the compiled value also carries the existing
`AgentPlan` so normal handoff and Run ownership remain unchanged. Adaptive
steps cannot exceed the goal bound, and dependency cycles are rejected.
Canonical goal constraints are copied onto every compiled step and included in
handoff metadata, so downstream agents receive the same immutable planning
envelope without gaining additional authority.
Compilation is a pure planning operation: it creates no Task, Run, Effect,
Tool claim, or approval. `LIPASRuntime.compile_workflow()` supplies the
Runtime workspace by default.

`run_provider_workflow(..., live=True)` is the explicit production probe for
one configured real provider. It creates one deterministic durable Task/Run
and returns bounded provider/model/terminal evidence; the live flag prevents
accidental billable calls. `run_execution_soak()` and `lipas soak` exercise the
local transition path for a bounded count/time window and report invariant
failures separately from provider availability.
Provider evidence also carries an operator-facing `outcome` classification
(`succeeded`, `provider_error`, `uncertain`, `cancelled`, or `non_success`) and
aggregates usage from durable model-completed events when available; the
projection excludes the prompt and raw, unredacted provider diagnostics.

## 0.63 production contracts

The 0.63 deployment layer keeps the same authority boundaries while making the
single-workspace path operational: `install`/`upgrade` maintain a restrictive
manifest, backup bundles include verifiable workspace and per-Run evidence,
and `verify-bundle` checks them without mutation. `lipas soak` supplies bounded
local durability evidence. `TLSConfig` supports TLS 1.2+ and live certificate
or trust-context reload for Operator and remote Worker endpoints;
`ManagedSecretResolver` is an integration boundary for external KMS/HSM or
secret managers and does not claim custody by itself. Real provider runs and
external partner signoff remain deployment evidence, not an automatic result
of local tests or fixtures.

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
