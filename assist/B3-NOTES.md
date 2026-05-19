# B3 — Supervisor: Design Notes

## Where supervisor lives in the architecture

LIPAS has no single "agent loop" file. Each `AgentBehaviour` (ReAct,
Plan-and-Execute, Critique-and-Revise, ...) owns its own loop shape.
Supervisor is therefore a **behaviour-level** component, not a
loop-level one:

- **ReActAgent** calls `supervisor.tick(view, ctx)` at the end of
  every R-A-O cycle, after `_fold_iteration` and before the loop
  continues. Natural-stop / error / max-iterations paths do not
  tick — those terminations are already final.
- Future behaviours (PaE, C-and-R) pick their own tick site; the
  Supervisor module is agnostic to who drives it.

## Why advisory-only

Supervisor emits `supervisor_retry` / `supervisor_terminate` /
`supervisor_escalate` claims into the rowset. The behaviour MAY act
on them. The required tape invariant is the *converse*:

> If a downstream effect cites a supervisor recommendation as its
> trigger, its lineage MUST carry the supervisor claim's
> `idempotency_key` / `target_effect_id`.

The forward direction ("every `supervisor_retry` produces an effect")
is intentionally not required. This keeps Supervisor pure-observation
and lets behaviours decide locally what to do.

In ReActAgent v0.1:
- `supervisor_terminate` → early `FinalResult(stop_reason="supervisor_terminate")`.
- `supervisor_escalate`  → early `FinalResult(stop_reason="supervisor_escalate", metadata={"supervisor_payload": ...})`.
- `supervisor_retry`     → recorded but **not** acted on. ReAct already
  re-feeds `is_error` tool results to the LLM in the next iteration;
  retry tactics are most useful in retry-aware behaviours that don't
  yet exist.

## Snapshot semantics within a tick

Within a single `tick`, all predicates observe the same frozen
`(view, ctx)`. The retry-cap tally is captured once at tick start.
Predicates registered later in the same tick see neither earlier
predicates' emissions nor each other's.

Implementation: phase 1 evaluates every predicate against the snapshot
without folding; phase 2 folds the resulting batch. The retry-cap
in-tick counter (`in_tick_retries`) sits between phases so that two
predicates targeting the same effect cannot both bypass the cap.

## Atomicity caveat

`RowSet.fold` is per-claim. A crash mid-batch leaves a partial set of
supervisor claims folded. **B1 (durable storage) collapses this to an
atomic write**; until then, the partial-batch case is acceptable
because every supervisor_* claim is independently meaningful — there
are no cross-claim invariants within a tick's emissions.

If a downstream tool ever needs "all-or-nothing per tick", the right
fix is in B1, not in Supervisor.

## Replay posture (forward-looking)

Supervisor SHOULD NOT be ticked during replay. The recorded
`supervisor_*` claims live in the source store and are not re-derived.
A future "supervisor replay cursor" — parallel to `ReplayCursor`
(LLM) and `ToolReplayer` (tool) — MAY mirror them into a target
store. That is out of scope for B3.

## Schema evolution hook

Each supervisor_* claim carries `F_SUP_SCHEMA_VERSION` in `fields`,
anticipating A2 (Claim Schema Evolution). When A2 lands, this field
becomes the canonical entry point for upcasters. Until then it is
purely informational.

## Tactic batches

**First batch (B3)**: `retry / terminate / escalate_human` — covers
the "agent is stuck / agent should stop / agent needs a human" axis.

**Second batch (deferred)**: `degrade / circuit_break / compensate` —
defer until concrete use cases land. Adding them later is a non-
breaking change: a new `Tactic` enum member, a new dataclass, a new
tag, and a `_action_to_claim` branch.
