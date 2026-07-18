# LIPAS execution model

> Language: [English](execution-model.md) | [中文](execution-model.zh-CN.md)

This is the one conceptual document for LIPAS. You do not need it to write a
first agent; start in the [README](../README.md) or the
[step-by-step tutorial](tutorial.md). Read this when
you need to know what a durable trace, replay, or Team actually guarantees.

## Start from the application's need

LIPAS has four execution ideas:

| Idea | Meaning | Add it when |
|---|---|---|
| `Agent` | One assistant: model, tools, and a reason/act loop | The normal starting point |
| `@tool` | An explicit Python capability with a declared side-effect class | The assistant needs to read or change something |
| `ExecutionStore` | Durable Task/Run ownership, checkpoints, cancellation, and approval Interrupts | The same Agent run must survive waiting or process loss |
| `Team` | A durable handoff boundary between named assistants or functions | Work needs a separate owner, restart boundary, or audit trail |

An Agent can use many tools and make many model calls. That alone does **not**
call for a Team. Add `ExecutionStore` when the same Agent run needs recovery;
add a Team only when the next piece of work should survive as
a separately owned handoff—for example, a research task handed from a planner
to an independently restartable researcher, or a payment requiring a distinct
approval boundary.

Tools are not agents. They are the explicit hands of an Agent. A Team member
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
| Team delivery and acknowledgement | mailbox SQLite database | recoverable transition mirror |

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

Guards and budgets run before the live effect. A denial is still recorded as an
intent plus a typed rejection. A tool with an `estimate=` must produce finite,
non-negative estimates before it can run; an invalid or failing estimate is an
`estimate_invalid` rejection, never a reason to bypass a hard budget. The
accepted intent snapshots the submitted input. `caused_by` can link an Agent
effect to its Team message; `compensates` can link a compensating effect to an
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
`ExecutionStore`, `OperationJournal`, and the Team mailbox all fail closed when
opened by an incompatible release. If `ExecutionStore` is constructed with
`rowset=...`, each Task/Run/checkpoint/Interrupt transition is also mirrored
from its transactional outbox into that Claim tape; the execution database
remains authoritative for control decisions.

The durable loop checkpoints before and after each model call, after every tool
result, after each completed observation, and before terminal settlement. Model
and tool Effects receive deterministic identities scoped to the run. If a
process stops after a terminal Effect was recorded but before its checkpoint,
the next lease owner restores that result from the Effect tape. If only the
intent exists, recovery raises `OrphanedEffectError` instead of guessing that a
second submission is safe.

An approval policy can atomically checkpoint and move the Run to `waiting`
before a tool executes. Resolving the Interrupt with `allow=True` makes the Run
claimable again; `Agent.resume_durable()` restores the checkpoint without
appending the original prompt twice. Durable execution currently requires a
SQLite Agent session. Cooperative cancellation is checkpointed and an expired
cancel-requested lease can be reclaimed solely to finish cancellation.
Supervisor ticks use stable claim identities in durable runs, so recovery can
repair a crash between a recommendation and its checkpoint without duplicating
the recommendation. Timeout policy and automatic lease heartbeats remain
future work. The recovery contract is exercised with a real subprocess
`SIGKILL` after a completed write Effect and before its checkpoint: restart
restores the Effect result and does not execute the write a second time.

## Teams: reliable handoff, not a graph DSL

`Team` records handoff, lease, acknowledgement, release, and recovery in its
own durable session. Delivery is at least once: a crashed member's lease can
expire and the same stable message can be delivered again. The receiver uses
the message id as its idempotency/replay key. An acknowledgement is valid only
while its lease is active; an expired worker cannot acknowledge late work.

This is intentionally smaller than distributed ownership or a workflow graph.
Each Agent keeps its own authority and budget today; cross-Team budget sharing,
capability delegation, and mailbox replay are explicit application work.

## Streaming: a lower-level boundary today

`LLMHarness.stream(...)` can yield normalized `Delta`, `ToolUseDelta`, and
terminal `Done` events while preserving the same effect record. Once an event
is visible, that attempt is not retried: emitted output cannot be taken back.
The high-level `Agent` API deliberately returns a final `FinalResult` only; it
does not yet expose caller-facing token streaming.

## Deliberate boundaries

LIPAS does not provide a graph DSL, a hosted control plane, magical long-term
memory, global distributed transactions, or provider-independent exactly-once
delivery. Its job is narrower: make an Agent's decisions, costs, effects,
failures, and recovery state explicit enough to inspect and replay safely.

The provider-neutral `Request`, `Reply`, content, usage, and stream-event
shapes live in `lipas.adapter`. Ollama, injected-client Anthropic, and the
optional-SDK OpenAI Responses adapter implement those shapes.

The authoritative SQLite state of `OperationJournal` and the Team mailbox
delivery is durable, but their optional Claim audit is a separate transaction.
Each authoritative mutation appends a Claim-shaped event to an outbox in the
same SQLite transaction. Constructors and idempotent retry paths call
`repair_audit()` to replay that outbox with stable Claim ids, so a process stop
between databases can make the audit temporarily lag but cannot permanently
lose or duplicate the mirrored event. This is recoverable mirroring, not a
distributed transaction: callers must still use the journal/mailbox database,
not presence of a Claim alone, to decide whether an operation or handoff exists.
