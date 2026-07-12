"""Demonstrate Supervisor (B3, first batch).

Three tactics fire across a run:

  - retry         : recorded as an advisory claim only.
  - terminate     : ReActAgent honors → early FinalResult.
  - escalate_human: ReActAgent honors → early FinalResult with
                    payload in metadata.

This example does NOT call a real LLM. We construct a minimal
rowset, seed it with a synthetic orphan effect_intent, then drive
the Supervisor directly to show the claim shapes.
"""
from __future__ import annotations

from lipas.calculus import Claim, make_default_registry
from lipas.effect import EffectKind
from lipas.rows import RowSet
from lipas.rows.capability import CapabilityRow
from lipas.rows.effect import (
    EffectRow, F_EFFECT_ID, F_KIND, F_MODEL, F_REQUEST,
    TAG_EFFECT_INTENT,
)
from lipas.rows.history import HistoryRow
from lipas.store import ClaimStore
from lipas.supervisor import (
    EscalateAction,
    Policy,
    PolicyRule,
    RetryAction,
    Supervisor,
    TAG_SUPERVISOR_ESCALATE,
    TAG_SUPERVISOR_RETRY,
    TAG_SUPERVISOR_TERMINATE,
    TerminateAction,
)


def fresh_substrate() -> RowSet:
    registry = make_default_registry()
    store = ClaimStore(registry=registry)
    return RowSet(store, rows=[
        HistoryRow(),
        CapabilityRow(budgets={"tool_calls": 100.0, "wall_seconds": 60.0}),
        EffectRow(),
    ])


def seed_orphan_intent(rs: RowSet, effect_id: str) -> None:
    """Fold an effect_intent with no terminal claim → orphan."""
    rs.fold(Claim(
        tag=TAG_EFFECT_INTENT,
        fields={
            F_EFFECT_ID: effect_id,
            F_KIND:      EffectKind.LLM_CALL.value,
            F_MODEL:     "demo-model",
            F_REQUEST:   {"messages": []},
        },
        source="example.seed",
    ))


def view_and_ctx(rs: RowSet):
    eff = next(r for r in rs.rows if isinstance(r, EffectRow))
    return eff.project(rs.store), rs.store.ctx


def claim_summary(rs: RowSet) -> dict[str, int]:
    counts: dict[str, int] = {}
    for c in rs.store:
        counts[c.tag] = counts.get(c.tag, 0) + 1
    return dict(sorted(counts.items()))


# ── Predicates ────────────────────────────────────────────────────────


TARGET = "call_aaaaaaaaaaaa"


def retry_orphans(view, ctx):
    """If we have any orphan, recommend retrying TARGET (max 2)."""
    if any(eid == TARGET for eid in view.orphans):
        return RetryAction(
            target_effect_id=TARGET,
            max_attempts=2,
            reason="orphan intent observed",
        )
    return None


def terminate_after_three_orphans(view, ctx):
    """If too many orphans accumulate, terminate the run."""
    if len(view.orphans) >= 3:
        return TerminateAction(
            reason=f"orphan count {len(view.orphans)} ≥ 3",
        )
    return None


def escalate_on_terminate(view, ctx):
    """Demo: independent rule that escalates on the same condition.
    Both terminate and escalate fire on the same tick — first
    terminate-or-escalate wins for the agent loop (see ReActAgent),
    but BOTH claims are folded into the audit trail."""
    if len(view.orphans) >= 3:
        return EscalateAction(
            reason="manual review required",
            payload={"channel": "ops-on-call", "severity": "P3"},
        )
    return None


# ── Run ───────────────────────────────────────────────────────────────


def main() -> None:
    rs = fresh_substrate()
    seed_orphan_intent(rs, TARGET)

    sup = Supervisor(
        policy=Policy.of(
            PolicyRule("retry_orphans",          retry_orphans),
            PolicyRule("terminate_three",        terminate_after_three_orphans),
            PolicyRule("escalate_three",         escalate_on_terminate),
        ),
        rowset=rs,
        session_id="example-08",
    )

    # Tick 1 — only the orphan is present.  retry_orphans fires
    # (attempt 1).  terminate / escalate predicates see 1 orphan, do
    # not fire.
    print("── tick 1 ──")
    view, ctx = view_and_ctx(rs)
    emitted = sup.tick(view, ctx)
    print(f"  emitted: {[c.tag for c in emitted]}")
    print(f"  store  : {claim_summary(rs)}")

    # Tick 2 — same state.  retry_orphans fires again (attempt 2 == cap).
    print("\n── tick 2 ──")
    view, ctx = view_and_ctx(rs)
    emitted = sup.tick(view, ctx)
    print(f"  emitted: {[c.tag for c in emitted]}")
    print(f"  store  : {claim_summary(rs)}")

    # Tick 3 — same state.  Cap reached, retry skipped silently.
    print("\n── tick 3 (cap reached) ──")
    view, ctx = view_and_ctx(rs)
    emitted = sup.tick(view, ctx)
    print(f"  emitted: {[c.tag for c in emitted]}  (none — cap)")
    print(f"  store  : {claim_summary(rs)}")

    # Tick 4 — seed two more orphans → orphan count == 3 → both
    # terminate and escalate fire.
    seed_orphan_intent(rs, "call_bbbbbbbbbbbb")
    seed_orphan_intent(rs, "call_cccccccccccc")

    print("\n── tick 4 (3 orphans) ──")
    view, ctx = view_and_ctx(rs)
    emitted = sup.tick(view, ctx)
    print(f"  emitted: {[c.tag for c in emitted]}")
    print(f"  store  : {claim_summary(rs)}")

    # Final tally.
    print("\n── final ──")
    counts = claim_summary(rs)
    print(f"  retries     : {counts.get(TAG_SUPERVISOR_RETRY, 0)}")
    print(f"  terminates  : {counts.get(TAG_SUPERVISOR_TERMINATE, 0)}")
    print(f"  escalates   : {counts.get(TAG_SUPERVISOR_ESCALATE, 0)}")
    print(f"  total store : {len(rs.store)}")


if __name__ == "__main__":
    main()
