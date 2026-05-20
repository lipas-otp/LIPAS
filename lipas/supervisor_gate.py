"""
LIPAS · supervisor_gate.py — sketch-style surface over Supervisor.

HARD RULE: this file is SURFACE. supervisor.py MUST NOT import from it.

Usage (H1 — active tick inside should_continue):

    gate = SupervisorGate(supervisor, rowset)
    for _ in range(20):
        reply = await llm(messages, tools=[...])
        for c in reply.tool_calls:
            await c.invoke()
        if not gate.should_continue():
            break

Per-call policy:
  - Project EffectRow via row.project(store) — the same projection
    Supervisor consumes inside Belief.
  - Read BeliefContext from store.ctx (ClaimStore guarantees this is
    a non-None instance).
  - tick(view, ctx) — fold-into-store is the supervisor's business,
    not ours.
  - HALT iff any returned claim is supervisor_terminate or
    supervisor_escalate. supervisor_retry is informational here.
"""
from __future__ import annotations

from dataclasses import dataclass

from lipas.calculus import BeliefContext
from lipas.rows import RowSet
from lipas.rows.effect import EffectRow
from lipas.supervisor import (
    Supervisor,
    TAG_SUPERVISOR_ESCALATE,
    TAG_SUPERVISOR_TERMINATE,
)

__all__ = ["SupervisorGate"]


_HALT_TAGS = frozenset({
    TAG_SUPERVISOR_TERMINATE,
    TAG_SUPERVISOR_ESCALATE,
})


@dataclass
class SupervisorGate:
    supervisor: Supervisor
    rowset:     RowSet

    def should_continue(self) -> bool:
        """Run one supervisor tick; return False iff terminate or
        escalate was emitted on this tick.

        Idempotency note: tick() reads its own prior decisions out
        of the store (e.g. retry-cap counters), so calling
        should_continue() twice in a row without other folds in
        between is well-defined but MAY fire predicates that are
        non-cap-bounded (terminate-on-condition will fire every tick
        as long as the condition holds). That is a Supervisor-level
        concern, not a gate concern.
        """
        view = self._effect_view()
        ctx  = self._belief_ctx()
        emitted = self.supervisor.tick(view, ctx)
        return not any(c.tag in _HALT_TAGS for c in emitted)

    # ── internals ──────────────────────────────────────────────

    def _effect_view(self):
        """Locate the EffectRow in the rowset and project it.
        Raises if no EffectRow is wired — the rowset is misconfigured
        for supervisor use."""
        for row in self.rowset.rows:
            if isinstance(row, EffectRow):
                return row.project(self.rowset.store)
        raise RuntimeError(
            "SupervisorGate: rowset has no EffectRow; supervisor "
            "predicates cannot run without one. Add EffectRow() to "
            "the RowSet."
        )

    def _belief_ctx(self) -> BeliefContext:
        return self.rowset.store.ctx
