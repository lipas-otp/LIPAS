"""Demonstrate ToolHarness replay (P3.2 / RFC-001).

Three phases:

  Phase 1 — live:
    Run two PURE tool calls against an empty store. The harness folds
    effect_intent + effect_result + resource_spent for each.

  Phase 2 — STRICT_TAPE replay:
    Build a ToolReplayer over phase 1's EffectView and attach it to a
    NEW ToolHarness pointing at a fresh store. The replayer's matrix
    fires "substitute" for each call: no live execution, the recorded
    output is mirrored verbatim into the new store, plus a
    replay_decision audit claim per call.

  Phase 3 — STRICT_TAPE miss:
    Issue a call whose (tool_name, arguments) tuple was never recorded.
    STRICT_TAPE raises ReplayMissing immediately; only the
    replay_decision claim (operation="fail") is folded — no intent /
    result / spend.

Sanity checks at the end:
  - phase 2 store contains intent + result + spend + decision claims
    for every replayed call (no live wall_seconds spend);
  - phase 2 outputs equal phase 1 outputs byte-for-byte;
  - phase 3 raises ReplayMissing.
"""
from __future__ import annotations

import asyncio

from lipas.calculus import make_default_registry
from lipas.replay_tools import (
    ReplayMissing,
    ReplayMode,
    TAG_REPLAY_DECISION,
    ToolReplayer,
)
from lipas.rows import RowSet
from lipas.rows.capability import CapabilityRow
from lipas.rows.effect import EffectRow
from lipas.rows.history import HistoryRow
from lipas.store import ClaimStore
from lipas.tool_harness import ToolHarness
from lipas.tools import SideEffectClass, ToolRegistry, tool


# ── Tools (PURE) ──────────────────────────────────────────────────────

@tool(side_effect=SideEffectClass.PURE)
def add(a: float, b: float) -> float:
    """Return the sum of two numbers."""
    return a + b


@tool(side_effect=SideEffectClass.PURE)
def multiply(a: float, b: float) -> float:
    """Return the product of two numbers."""
    return a * b


# ── Substrate factory ─────────────────────────────────────────────────

def fresh_substrate() -> RowSet:
    registry = make_default_registry()
    store = ClaimStore(registry=registry)
    return RowSet(store, rows=[
        HistoryRow(),
        CapabilityRow(budgets={
            "tool_calls":   100.0,
            "wall_seconds": 60.0,
        }),
        EffectRow(),
    ])


def claim_summary(store: ClaimStore) -> dict[str, int]:
    """Count claims by tag for one-line printing."""
    counts: dict[str, int] = {}
    for c in store:
        counts[c.tag] = counts.get(c.tag, 0) + 1
    return dict(sorted(counts.items()))


# ── Run ───────────────────────────────────────────────────────────────

async def main() -> None:
    tools = ToolRegistry([add, multiply])

    # ── Phase 1: live ───────────────────────────────────────────
    rowset_a = fresh_substrate()
    harness_live = ToolHarness(tools=tools, rowset=rowset_a)

    live_a = await harness_live.call(
        tool_name="add", arguments={"a": 12, "b": 7},
    )
    live_b = await harness_live.call(
        tool_name="multiply", arguments={"a": 19, "b": 3},
    )

    print(f"[live]    add(12,7)         -> {live_a['content']!r}")
    print(f"[live]    multiply(19,3)    -> {live_b['content']!r}")
    print(f"[live]    claims by tag     :  {claim_summary(rowset_a.store)}")

    # ── Phase 2: STRICT_TAPE replay (matches recorded calls) ────
    eff      = next(r for r in rowset_a.rows if isinstance(r, EffectRow))
    view_a   = eff.project(rowset_a.store)
    replayer = ToolReplayer(view=view_a, mode=ReplayMode.STRICT_TAPE)

    rowset_b = fresh_substrate()
    harness_replay = ToolHarness(
        tools=tools,
        rowset=rowset_b,
        tool_replayer=replayer,
    )

    rep_a = await harness_replay.call(
        tool_name="add", arguments={"a": 12, "b": 7},
    )
    rep_b = await harness_replay.call(
        tool_name="multiply", arguments={"a": 19, "b": 3},
    )

    print(f"\n[replay]  add(12,7)         -> {rep_a['content']!r}")
    print(f"[replay]  multiply(19,3)    -> {rep_b['content']!r}")
    print(f"[replay]  claims by tag     :  {claim_summary(rowset_b.store)}")

    # ── Phase 3: STRICT_TAPE miss ──────────────────────────────
    try:
        await harness_replay.call(
            tool_name="add", arguments={"a": 99, "b": 99},
        )
    except ReplayMissing as e:
        print(f"\n[replay]  unrecorded call    -> ReplayMissing: {e}")
    else:
        raise AssertionError("expected ReplayMissing on unrecorded call")

    # ── Verification ────────────────────────────────────────────
    assert rep_a["content"] == live_a["content"], \
        "replay must reproduce add() output verbatim"
    assert rep_b["content"] == live_b["content"], \
        "replay must reproduce multiply() output verbatim"

    # The replay store should have:
    #   - 2 effect_intent + 2 effect_result + 2*N resource_spent
    #     (mirrored from recorded calls; wall_seconds=0 each)
    #   - 1 session-init replay_decision + 3 per-call replay_decision
    #     (substitute, substitute, fail)
    decision_claims = [
        c for c in rowset_b.store if c.tag == TAG_REPLAY_DECISION
    ]
    operations = [c.fields.get("operation") for c in decision_claims]
    print(f"\n[replay]  decision ops      :  {operations}")
    assert "session_init" in operations
    assert operations.count("substitute") == 2
    assert operations.count("fail") == 1

    # No wall_seconds was spent on the replayed PURE calls — the
    # mirror folds wall_seconds=0.0 which is filtered by the
    # `amount <= 0` guard in _fold_spend, so wall_seconds spend is
    # absent entirely from the replay store.
    cap = next(r for r in rowset_b.rows if isinstance(r, CapabilityRow))
    proj = cap.project(rowset_b.store)
    print(f"[replay]  tool_calls spent  :  {proj['tool_calls']['spent']}")
    print(f"[replay]  wall_seconds spent:  {proj['wall_seconds']['spent']}")
    assert proj["tool_calls"]["spent"] == 2.0, \
        "every replayed call should still charge tool_calls"
    assert proj["wall_seconds"]["spent"] == 0.0, \
        "replayed PURE calls should not charge wall_seconds"

    print("\nOK: replay reproduced live outputs, audit trail captured "
          "every decision, no wall time was spent.")


if __name__ == "__main__":
    asyncio.run(main())
