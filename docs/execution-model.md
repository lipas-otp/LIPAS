# LIPAS execution model

> Language: [English](execution-model.md) | [中文](execution-model.zh-CN.md)

This is the one conceptual document for LIPAS. You do not need it to write a
first agent; start in the [README](../README.md) or the
[step-by-step tutorial](tutorial.md). Read this when
you need to know what a durable trace, replay, or handoff actually guarantees.

## Start from the application's need

LIPAS has four execution ideas:

| Idea | Meaning | Add it when |
|---|---|---|
| `Agent` | One assistant: model, tools, and a reason/act loop | The normal starting point |
| `@tool` | An explicit Python capability with a declared side-effect class | The assistant needs to read or change something |
| `ExecutionStore` | Durable Task/Run ownership, checkpoints, cancellation, and approval Interrupts | The same Agent run must survive waiting or process loss |
| `AgentCoordinator` | ExecutionStore-backed handoffs and bounded policies across named members | Work needs a separate owner, restart boundary, or audit trail |

An Agent can use many tools and make many model calls. That alone does **not**
call for multiple Agents. Add `ExecutionStore` when the same Agent run needs recovery;
add a coordinator only when the next piece of work should survive as
a separately owned handoff—for example, a research task handed from a planner
to an independently restartable researcher, or a payment requiring a distinct
approval boundary.

Tools are not agents. They are the explicit hands of an Agent. A coordinator member
is usually one Agent, but can be a plain async function when no model is
needed.

A `Skill` is optional reusable guidance in a portable `SKILL.md` file. It is
loaded into an Agent's instructions, but creates no capability and no new
execution semantics: the Agent can still act only through its declared tools.

## One evidence tape, explicit control stores

Every reliability-relevant model, tool, budget, replay, and supervision event
becomes a **Claim** with a tag, fields, source, and stable `claim_id`. Once a
store admits a Claim, it owns an immutable snapshot: re-delivering the same
logical id and payload is a no-op, reusing an id for different content is
rejected, and later caller mutation cannot rewrite the tape. The Python
`Claim` object used to prepare an event is not itself a frozen value.

A **fold** appends that claim and updates derived views. This is the central
rule of the runtime: decisions and effects are recorded before they become
easy to forget. A merge strategy must be deterministic; a field that uses a
semilattice strategy is also order-independent. History deliberately remains
ordered.

The standard session has only three rows:

| Row | Question | Responsibility |
|---|---|---|
| History | What happened or was decided? | observations, replay choices, supervision, execution, mailbox, and operation transitions |
| Capability | May this spend happen? | budgets, resource spend, quota and rate events |
| Effect | What call was intended, and what became of it? | model/tool intent, result, rejection, and causal links |

Rows are projections over the same tape, not separate databases or a hidden
workflow state. A new row should exist only when a concern owns its own tags
and genuinely needs a separate invariant or view. Domain memory, search
indexes, and user profiles remain ordinary application data—not a LIPAS row.

Mutable coordination state has a different job and an explicit authority:

| State | Authoritative store | Claim role |
|---|---|---|
| model/tool Effects, spend, replay, supervision | Agent Claim/Effect session | authoritative evidence |
| Task, Run, lease, checkpoint, Interrupt | `ExecutionStore` | recoverable transition mirror when a `RowSet` is attached |
| external-write reconciliation | `OperationJournal` | recoverable transition mirror |
| coordinator handoff identity, lease, cancellation, terminal result | `ExecutionStore` | the same Task/Run event stream |
| legacy Team delivery and acknowledgement | mailbox SQLite database | recoverable transition mirror |

Leases and compare-and-swap checkpoints are not forced through Claim merge.
Their authoritative SQLite transition instead appends a Claim-shaped event to
a local outbox in the same transaction. Mirroring that outbox can lag after a
process stop, but `repair_audit()` restores each event with a stable Claim id.
This keeps control state precise while preserving one evidence vocabulary.

## Effects: make the live boundary visible

Every model or tool invocation has a lifecycle:

```text
effect_intent  →  effect_result | effect_rejected
```

Intent is recorded before the live invocation. A result or rejection makes the
outcome explicit. An intent without either is an **orphan**: an interrupted or
unknown operation that must be investigated, not silently treated as success.

Model requests carry a stable `request_id` derived from the Effect identity
when the caller does not provide one. OpenAI-compatible adapters forward it as
`Idempotency-Key` and `X-Request-ID`; every retry also records an `llm_attempt`
claim. The final Effect stores aggregate billed usage, including failed retry
attempts.

Guards and budgets run before the live effect. A denial is still recorded as an
intent plus a typed rejection. A tool with an `estimate=` must produce finite,
non-negative estimates before it can run; an invalid or failing estimate is an
`estimate_invalid` rejection, never a reason to bypass a hard budget. The
accepted intent snapshots the submitted input. `caused_by` can link an Agent
effect to its handoff envelope; `compensates` can link a compensating effect to an
earlier one.

## Replay: reproduce decisions without accidentally repeating effects

LLM replay substitutes a recorded reply. Tool replay is strict by default: a
recorded tool result is substituted and no live tool executes. `BEST_EFFORT`
can execute a missing call; `LIVE_REROUTE` refuses an external write unless the
caller explicitly opts in.

Replay proves which recorded decision was used. It does **not** prove that the
original external operation was delivered exactly once.

Repeated tool calls with identical names and arguments consume matching source
recordings in fold order. A replay target therefore reproduces changing
read-only observations instead of substituting the first matching result for
every occurrence.

## The external boundary

`OperationJournal` is the boundary for an external write that supports an
idempotency key. It persists the caller's key before submission and records
`prepared`, `uncertain`, `succeeded`, and `failed`. Its transitions can link to
the originating effect. A known terminal outcome is immutable: repeated
reconciliation returns it rather than allowing stale information to rewrite it.

After a crash or an ambiguous provider error, the state is `uncertain`.
LIPAS refuses blind resubmission; application code must reconcile with the
provider. Exactly-once is possible only when that provider honors the same key
and offers a way to determine the outcome.

If LIPAS cannot durably record a provider return—for example, because a result
cannot be serialized—it also marks a still-pending submission `uncertain`.
Recording trouble is never treated as evidence that the external write did not
happen.

## Durable ReAct runs

An ordinary `Agent.run()` owns an in-memory reason/act/observe loop while its
session records Effects. `Agent.run_durable()` additionally connects that loop
to an `ExecutionStore`. The execution store owns Task/Run state, a fenced run
lease, versioned phase checkpoints, and approval Interrupts; the Agent's SQLite
session remains the source of truth for model and tool Effects.
The checkpoint records that session's stable `store_id`; resuming with a
different claim database fails before any live model or tool call.
Every authoritative SQLite control store carries an explicit schema version:
`ExecutionStore`, `OperationJournal`, and the legacy Team mailbox all fail closed when
opened by an incompatible release. If `ExecutionStore` is constructed with
`rowset=...`, each Task/Run/checkpoint/Interrupt transition is also mirrored
from its transactional outbox into that Claim tape; the execution database
remains authoritative for control decisions.

The durable loop checkpoints before and after each model call, after each
serial tool result or completed safe parallel batch, after each observation,
and before terminal settlement. Model and tool Effects receive deterministic
identities scoped to the run. If a
process stops after a terminal Effect was recorded but before its checkpoint,
the next lease owner restores that result from the Effect tape. If only the
intent exists, recovery raises `OrphanedEffectError` instead of guessing that a
second submission is safe.

One model reply may request several independent tools. Up to
`Agent(max_parallel_tools=4)` contiguous `pure`/`read_only` calls can execute
concurrently. Result blocks retain the model's original order, and every call
keeps its own deterministic Effect identity, so a crash after results but
before the batch checkpoint restores them without repeating live work. Writes
always remain serial. Calls also remain serial when hard budgets, guards, a
tool replay cursor, or custom argument/result hooks are active, because
concurrent preflight could otherwise admit work against stale
policy/accounting state. An in-flight call with only
an intent remains an orphan; parallelism does not weaken that fail-closed rule.

An approval policy can atomically checkpoint and move the Run to `waiting`
before a tool executes. Resolving the Interrupt with `allow=True` makes the Run
claimable again; `Agent.resume_durable()` restores the checkpoint without
appending the original prompt twice. Durable execution currently requires a
SQLite Agent session. Cooperative cancellation is checkpointed and an expired
cancel-requested lease can be reclaimed solely to finish cancellation.
Supervisor ticks use stable claim identities in durable runs, so recovery can
repair a crash between a recommendation and its checkpoint without duplicating
the recommendation. The development line now includes automatic lease
heartbeats and model/tool phase timeouts. Sync tools leave the event loop;
cancellation leaves an orphan when the runtime cannot prove that the thread
stopped. A phase timeout persists `recovery_required`; the operator must
reconcile the Effect/provider, record an observation/evidence object, and
explicitly reopen the Run before resume. A boolean acknowledgement alone is
not evidence.
`ToolHarness.reconcile_orphan()` provides the equivalent explicit closeout for
a synchronous tool whose thread cannot be force-killed. For an intent-only
model Effect, `LLMHarness.reconcile_orphan()` records the provider-observed
`Reply` (or an explicit error observation) without issuing another request.
The recovery contract
is exercised with a real subprocess
`SIGKILL` after a completed write Effect and before its checkpoint: restart
restores the Effect result and does not execute the write a second time.

## Persistent local Task dispatch

`TaskDispatcher` turns pending Runs into a bounded local worker queue without
creating another scheduler database. Discovery is advisory; the conditional
`ExecutionStore.claim_run()` transition is the atomic ownership boundary, so
two workers cannot execute the same active lease. Pending Runs are dispatched
FIFO. Expired running leases are reclaimable after restart, including the
cancel-only recovery path.

Several Tasks may execute concurrently, but each Run owns a separate SQLite
Claim/Effect session. The global execution database owns Task/Run/lease state;
per-Run sessions own model/tool evidence and budgets. This avoids sharing one
hot Claim sequence or budget projection across unrelated Tasks.
Runs created before this layout retain their checkpoint-bound legacy session.

A Run that suspends for approval becomes `waiting`, drops its lease, and frees
the worker slot. Resolving the Interrupt with `allow=True` returns it to
`pending`; a worker may then claim and resume it. `lipas task submit` persists
work, `lipas task worker` continuously dispatches it, and `--max-concurrency`
bounds simultaneous Tasks. Stopping a worker cancels its local heartbeat; the
unsettled lease must expire before another worker can reclaim the Run.

## Staged ChangeSets and delivery

First-party CLI Tasks receive a per-Run staging workspace. The Agent reads,
writes, and runs verification there; the selected source workspace remains
unchanged while the Run executes. Staged writes retain their normal idempotent
Effect classification but do not require individual human approval because
they are contained inside product state. Commands keep their approval and OS
isolation boundary.

When the Run completes, the workbench compares the stage with its baseline and
marks the ChangeSet `ready`. The report and `lipas task diff` expose the full
file change. `lipas task apply` is the explicit delivery decision and is
allowed only for a completed Run. Before touching any destination, it verifies
that every changed path is still either at its recorded baseline or already at
the desired staged hash. Any unrelated drift fails closed.

Each file replacement is atomic. Applying several files is not one filesystem
transaction, but the operation is restartable: a path already equal to its
desired hash is accepted, while remaining baseline paths continue. A discarded
stage cannot be applied, and an applied stage cannot be discarded. Apply and
discard transitions are persistent product events and update report delivery
state.

The first snapshot backend copies Git tracked/non-ignored files, or ordinary
files for a non-Git workspace. Before persistence it excludes likely secret
paths/text, symlinks, oversized files, and common generated caches, and it
enforces aggregate file/byte limits. This is a bounded safety
backend, not yet a copy-on-write filesystem or Git worktree transaction.

## Multi-Agent: policies over authoritative Runs

`AgentCoordinator` maps every `HandoffEnvelope` to one deterministic Task/Run
in `ExecutionStore`. The Run owns the lease, attempt, persisted cancellation,
terminal value or error, and public handoff events. Reusing one envelope
identity with different input fails closed; repeating a completed request
replays its stored JSON-compatible result without invoking the member.

Sequential, RoundRobin, bounded parallel, map/reduce, Selector, and bounded
Swarm transfer only compose these Runs. They do not own another queue or graph
projection. Member registration remains host code configuration. Expired work
is not redelivered unless that member explicitly declares its whole invocation
`redelivery_safe`; a live lease conflict is visible rather than silently
retried. Parent cancellation and absolute deadline reach every branch, while a
persisted `cancel_handoff()` request is observed by heartbeat.

An ordinary `Agent` member receives causality and branch `RunContext`, and its
own session still records model/tool Effects. A SQLite-backed Agent member is
bridged differently: its already-claimed coordination Run is passed to
`Agent.run_durable()`, so checkpoints, Approval/Input Interrupts, heartbeat,
and Effect recovery share one lease and one authority. An uncertain external
Effect fails closed. Shared Team budgets, capability delegation, and durable
nested Agent interrupts still need explicit future policy.

Legacy `Team` continues to expose its mailbox handoff/acknowledgement API for
existing applications. Its mailbox remains authoritative for that legacy API;
new orchestration should use `AgentCoordinator` and must not treat both systems
as authority for one logical handoff. See [Multi-Agent coordination](multi-agent.md).

## Streaming: one public event protocol

`LLMHarness.stream(...)` can yield normalized `Delta`, `ToolUseDelta`, and
terminal `Done` events while preserving the same effect record. Once an event
is visible, that attempt is not retried: emitted output cannot be taken back.
`Agent.run()` returns a final `FinalResult`; `Agent.stream()`, `Session`, and
durable cursor catch-up expose the normalized events at the application
boundary. Adapters that are honestly marked single-shot emit lifecycle events
without pretending to provide token deltas.

## Deliberate boundaries

LIPAS does not provide a graph DSL, a hosted control plane, magical long-term
memory, global distributed transactions, or provider-independent exactly-once
delivery. Its job is narrower: make an Agent's decisions, costs, effects,
failures, and recovery state explicit enough to inspect and replay safely.

The provider-neutral `Request`, `Reply`, content, usage, and stream-event
shapes live in `lipas.adapter`. Ollama, injected-client Anthropic, and the
optional-SDK OpenAI Responses adapter implement those shapes.

The authoritative SQLite state of `OperationJournal` and the legacy Team mailbox
delivery is durable, but their optional Claim audit is a separate transaction.
Each authoritative mutation appends a Claim-shaped event to an outbox in the
same SQLite transaction. Constructors and idempotent retry paths call
a bounded repair batch with stable Claim ids; explicit `repair_audit()` streams
the complete remainder. A process stop between databases can therefore make
the audit temporarily lag but cannot permanently lose or duplicate the mirrored
event. This is recoverable mirroring, not a distributed transaction: callers
must still use the journal/mailbox database, not presence of a Claim alone, to
decide whether an operation or legacy handoff exists. Coordinator handoffs are
already Task/Run facts in `ExecutionStore` and need no mailbox mirror.
