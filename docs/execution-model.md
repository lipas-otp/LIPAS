# LIPAS execution model

This is the one conceptual document for LIPAS. You do not need it to write a
first agent; read [Getting started](getting-started.md) first. Read this when
you need to know what a durable trace, replay, or Team actually guarantees.

## Start from the application's need

LIPAS has three public ideas:

| Idea | Meaning | Add it when |
|---|---|---|
| `Agent` | One assistant: model, tools, and a reason/act loop | The normal starting point |
| `@tool` | An explicit Python capability with a declared side-effect class | The assistant needs to read or change something |
| `Team` | A durable handoff boundary between named assistants or functions | Work needs a separate owner, restart boundary, or audit trail |

An Agent can use many tools and make many model calls. That alone does **not**
call for a Team. Add a Team only when the next piece of work should survive as
a separately owned handoff—for example, a research task handed from a planner
to an independently restartable researcher, or a payment requiring a distinct
approval boundary.

Tools are not agents. They are the explicit hands of an Agent. A Team member
is usually one Agent, but can be a plain async function when no model is
needed.

## One record, three views

Every reliability-relevant event becomes a **Claim**: an immutable record with
a tag, fields, source, and stable `claim_id`. A store admits one logical claim
id once. Re-delivering the same payload is a no-op; reusing an id for different
content is rejected.

A **fold** appends that claim and updates derived views. This is the central
rule of the runtime: decisions and effects are recorded before they become
easy to forget. A merge strategy must be deterministic; a field that uses a
semilattice strategy is also order-independent. History deliberately remains
ordered.

The standard session has only three rows:

| Row | Question | Responsibility |
|---|---|---|
| History | What happened or was decided? | observations, replay choices, supervision, mailbox and operation transitions |
| Capability | May this spend happen? | budgets, resource spend, quota and rate events |
| Effect | What call was intended, and what became of it? | model/tool intent, result, rejection, and causal links |

Rows are projections over the same tape, not separate databases or a hidden
workflow state. A new row should exist only when a concern owns its own tags
and genuinely needs a separate invariant or view. Domain memory, search
indexes, and user profiles remain ordinary application data—not a LIPAS row.

## Effects: make the live boundary visible

Every model or tool invocation has a lifecycle:

```text
effect_intent  →  effect_result | effect_rejected
```

Intent is recorded before the live invocation. A result or rejection makes the
outcome explicit. An intent without either is an **orphan**: an interrupted or
unknown operation that must be investigated, not silently treated as success.

Guards and budgets run before the live effect. A denial is still recorded as an
intent plus a typed rejection. `caused_by` can link an Agent effect to its Team
message; `compensates` can link a compensating effect to an earlier one.

## Replay: reproduce decisions without accidentally repeating effects

LLM replay substitutes a recorded reply. Tool replay is strict by default: a
recorded tool result is substituted and no live tool executes. `BEST_EFFORT`
can execute a missing call; `LIVE_REROUTE` refuses an external write unless the
caller explicitly opts in.

Replay proves which recorded decision was used. It does **not** prove that the
original external operation was delivered exactly once.

## The external boundary

`OperationJournal` is the boundary for an external write that supports an
idempotency key. It persists the caller's key before submission and records
`prepared`, `uncertain`, `succeeded`, and `failed`. Its transitions can link to
the originating effect.

After a crash or an ambiguous provider error, the state is `uncertain`.
LIPAS refuses blind resubmission; application code must reconcile with the
provider. Exactly-once is possible only when that provider honors the same key
and offers a way to determine the outcome.

## Teams: reliable handoff, not a graph DSL

`Team` records handoff, lease, acknowledgement, release, and recovery in its
own durable session. Delivery is at least once: a crashed member's lease can
expire and the same stable message can be delivered again. The receiver uses
the message id as its idempotency/replay key.

This is intentionally smaller than distributed ownership or a workflow graph.
Each Agent keeps its own authority and budget today; cross-Team budget sharing,
capability delegation, and mailbox replay are explicit application work.

## Deliberate boundaries

LIPAS does not provide a graph DSL, a hosted control plane, magical long-term
memory, global distributed transactions, or provider-independent exactly-once
delivery. Its job is narrower: make an Agent's decisions, costs, effects,
failures, and recovery state explicit enough to inspect and replay safely.

The provider-neutral `Request`, `Reply`, content, usage, and stream-event
shapes live in `lipas.adapter`. Ollama, injected-client Anthropic, and the
optional-SDK OpenAI Responses adapter implement those shapes.
