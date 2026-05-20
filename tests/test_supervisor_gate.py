"""
Tests for lipas/supervisor_gate.py — halt semantics + EffectRow lookup.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from lipas.calculus import BeliefContext, Claim
from lipas.rows.effect import EffectRow
from lipas.supervisor import (
    TAG_SUPERVISOR_ESCALATE,
    TAG_SUPERVISOR_RETRY,
    TAG_SUPERVISOR_TERMINATE,
)
from lipas.supervisor_gate import SupervisorGate


# =====================================================================
# Fakes
# =====================================================================

@dataclass
class FakeStore:
    ctx: BeliefContext = field(default_factory=BeliefContext)


@dataclass
class FakeRowSet:
    rows: list[Any]
    store: FakeStore = field(default_factory=FakeStore)


class FakeEffectRow(EffectRow):
    """Real subclass so isinstance(row, EffectRow) succeeds."""
    def __init__(self):
        pass

    def project(self, store):
        return {"projected_from": id(store)}


@dataclass
class FakeSupervisor:
    to_emit: list[Claim] = field(default_factory=list)
    seen_views: list[Any] = field(default_factory=list)
    seen_ctxs:  list[Any] = field(default_factory=list)

    def tick(self, view, ctx):
        self.seen_views.append(view)
        self.seen_ctxs.append(ctx)
        return list(self.to_emit)


def _claim(tag: str) -> Claim:
    # Claim is a dataclass; only .tag is read by the gate.
    # Fill the rest with whatever your minimal-construct convention is.
    return Claim(tag=tag, kind="supervisor", source="test", seq=-1, fields={})


# =====================================================================
# Tests
# =====================================================================

def _gate_with(*emit_tags: str) -> tuple[SupervisorGate, FakeSupervisor]:
    sup = FakeSupervisor(to_emit=[_claim(t) for t in emit_tags])
    rs  = FakeRowSet(rows=[FakeEffectRow()])
    return SupervisorGate(supervisor=sup, rowset=rs), sup


def test_no_emit_continues():
    gate, _ = _gate_with()
    assert gate.should_continue() is True


def test_retry_alone_continues():
    gate, _ = _gate_with(TAG_SUPERVISOR_RETRY)
    assert gate.should_continue() is True


def test_terminate_halts():
    gate, _ = _gate_with(TAG_SUPERVISOR_TERMINATE)
    assert gate.should_continue() is False


def test_escalate_halts():
    gate, _ = _gate_with(TAG_SUPERVISOR_ESCALATE)
    assert gate.should_continue() is False


def test_retry_plus_terminate_halts():
    gate, _ = _gate_with(TAG_SUPERVISOR_RETRY, TAG_SUPERVISOR_TERMINATE)
    assert gate.should_continue() is False


def test_tick_receives_projected_view_and_store_ctx():
    gate, sup = _gate_with()
    gate.should_continue()
    assert sup.seen_views == [{"projected_from": id(gate.rowset.store)}]
    assert sup.seen_ctxs  == [gate.rowset.store.ctx]
    assert isinstance(sup.seen_ctxs[0], BeliefContext)


def test_missing_effect_row_raises():
    sup  = FakeSupervisor()
    rs   = FakeRowSet(rows=[])  # no EffectRow
    gate = SupervisorGate(supervisor=sup, rowset=rs)
    with pytest.raises(RuntimeError, match="no EffectRow"):
        gate.should_continue()
