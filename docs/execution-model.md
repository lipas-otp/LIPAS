# Execution model

LIPAS is a Python reference implementation of a claim-based execution model
for reliable AI agents. It does not require a graph DSL: an `Agent` is an
ordinary assistant, a `Tool` is an explicitly classified capability, and a
`Team` is a durable handoff boundary between assistants or async functions.

## Claims and folds

A **claim** is an immutable, append-only record: a tag, fields, source, and
identity. A **fold** validates a claim against its owning row and updates that
row's projection with its declared merge strategy. The tape is the audit
record; projections are derived views for decisions and queries.

Rows own namespaces and invariants. `EffectRow` owns call lifecycle,
`CapabilityRow` owns budgets, and `HistoryRow` owns observations and
coordination transitions. A producer must fold through a `RowSet`, never write
an effect tag directly to an unrelated store.

## Effects

An LLM or tool invocation is an effect with a generated or supplied
`effect_id`:

```text
effect_intent → effect_result | effect_rejected
```

Intent is written before an invocation. A terminal result or rejection makes
the recorded outcome explicit; an intent without a terminal record is an
orphan and represents interruption or a bug worth investigating. `compensates`
links one effect to an earlier effect. `caused_by` links an effect to an
external causal root such as a Team `message_id`.

Guards and capability budgets run before the live effect. Their denial is
still an effect intent plus a typed rejection, not an invisible exception.
Supervisor recommendations are claims too; the default ReAct agent may honor
recorded termination or escalation recommendations.

## Replay

Replay is part of execution semantics, not a debugging afterthought. LLM tape
replay substitutes a recorded reply. `ToolReplayer` is strict by default:
recorded output is substituted and no live tool is invoked. `BEST_EFFORT` may
run a missing tool; `LIVE_REROUTE` refuses external writes unless explicitly
allowed.

This proves what the runtime replayed and which decision it made. It does not
prove that an original external operation happened exactly once.

## Teams

`Team` writes handoff, lease, acknowledgement, release, and recovery claims to
its durable audit session. Mailbox delivery is at least once: after an expired
lease, the same stable message can be delivered again. An Agent receiving a
Team message places its stable `message_id` in `caused_by` on its LLM and tool
effect intents. This associates independent Team and Agent tapes without
pretending they are one distributed transaction.

## The external boundary

`OperationJournal` is where a caller-supplied idempotency key crosses into an
external provider. It records `prepared`, `uncertain`, `succeeded`, and
`failed` transitions durably and can optionally fold the same transitions into
a claim session. Supply the originating `effect_id` to link that boundary to
the effect tape.

After a crash or ambiguous provider error, LIPAS marks the operation
`uncertain`; it refuses blind resubmission and requires reconciliation. An
exactly-once statement is valid only when the provider itself honors the
idempotency key and reconciliation contract.

## Canonical interchange shape and non-goals

`lipas.adapter` defines the canonical public `Request`, `Reply`, content,
usage, and stream-event shapes. Provider adapters normalize to these shapes;
new applications should not use the legacy `lipas.types` module.

LIPAS deliberately does not provide a graph/workflow DSL, global distributed
transactions, or provider-independent exactly-once delivery. Its boundary is
smaller and stricter: make decisions, effects, failures, and recovery states
explicit in a record that can be inspected and replayed safely.
