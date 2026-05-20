"""Tests for lipas.testing.deterministic_fold."""
from __future__ import annotations

import time

import pytest

from lipas.calculus import Claim, make_default_registry
from lipas.store import ClaimStore
from lipas.testing.deterministic_fold import (
    deterministic_fold,
    StrategyContractViolation,
)


# ── helpers ────────────────────────────────────────────────────

def _fold_two(store: ClaimStore, x1, x2) -> None:
    store.fold(Claim(tag="t", fields={"x": x1}, source="test"))
    store.fold(Claim(tag="t", fields={"x": x2}, source="test"))


# ── tests ──────────────────────────────────────────────────────

def test_clean_fold_passes():
    """A fold using only built-in pure strategies must pass.

    The default merge for unregistered fields is last-write-wins
    (per merge() in calculus.py); we assert the second value survives.
    """
    store = ClaimStore()
    with deterministic_fold():
        _fold_two(store, 1, 2)
    assert store.merged.fields.get("x") == 2


def test_deterministic_fold_is_a_noop_outside_violations():
    """Folds under deterministic_fold produce the same merged state
    as without it, when no forbidden API is used."""
    store_a = ClaimStore()
    store_b = ClaimStore()

    with deterministic_fold():
        _fold_two(store_a, 1, 2)
    _fold_two(store_b, 1, 2)

    assert store_a.merged.fields == store_b.merged.fields


def test_violation_is_detected_time_time():
    """deterministic_fold catches time.time() inside a strategy."""
    registry = make_default_registry()

    def impure(a, b, ctx):
        return time.time()

    registry.register("impure_field", impure)
    store = ClaimStore(registry=registry)

    store.fold(Claim(
        tag="t", fields={"impure_field": 1}, source="test",
    ))

    with pytest.raises(StrategyContractViolation):
        with deterministic_fold():
            store.fold(Claim(
                tag="t", fields={"impure_field": 2}, source="test",
            ))


def test_violation_message_names_offending_api():
    """The violation should identify which forbidden API was hit."""
    registry = make_default_registry()

    def impure(a, b, ctx):
        return time.time()

    registry.register("impure_field", impure)
    store = ClaimStore(registry=registry)
    store.fold(Claim(
        tag="t", fields={"impure_field": 1}, source="test",
    ))

    with pytest.raises(StrategyContractViolation) as excinfo:
        with deterministic_fold():
            store.fold(Claim(
                tag="t", fields={"impure_field": 2}, source="test",
            ))

    msg = str(excinfo.value).lower()
    assert "time" in msg
