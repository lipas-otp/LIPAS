"""Tests for lipas.session.open_session."""
from __future__ import annotations

import os
import tempfile

import pytest

from lipas.calculus import Claim
from lipas.exceptions import ClaimIdConflict
from lipas.rows.capability import CapabilityRow
from lipas.rows.base import InvariantViolation
from lipas.rows.effect import EffectRow
from lipas.rows.history import HistoryRow
from lipas.serialization.store_sqlite import SqliteClaimStore
from lipas.session import open_session


@pytest.fixture
def db_path():
    with tempfile.TemporaryDirectory() as d:
        yield os.path.join(d, "test.db")


def test_open_session_creates_sqlite_backed_rowset(db_path):
    rowset = open_session(db_path)
    try:
        assert isinstance(rowset.store, SqliteClaimStore)
        # Default triple wired.
        row_types = {type(r) for r in rowset.rows}
        assert EffectRow    in row_types
        assert HistoryRow   in row_types
        assert CapabilityRow in row_types
    finally:
        rowset.store.close()


def test_open_session_persists_across_reopen(db_path):
    rowset = open_session(db_path)
    try:
        rowset.store.fold(Claim(
            tag="custom_tag",
            fields={"x": 1},
            source="test",
        ))
        assert len(rowset.store) == 1
    finally:
        rowset.store.close()

    # Reopen — log should survive.
    rowset2 = open_session(db_path)
    try:
        assert len(rowset2.store) == 1
        assert rowset2.store.log[0].tag == "custom_tag"
    finally:
        rowset2.store.close()


def test_open_session_custom_rows(db_path):
    rowset = open_session(db_path, rows=[HistoryRow()])
    try:
        assert len(list(rowset.rows)) == 1
        assert isinstance(list(rowset.rows)[0], HistoryRow)
    finally:
        rowset.store.close()


def test_open_session_in_memory():
    rowset = open_session(":memory:")
    try:
        rowset.store.fold(Claim(
            tag="t", fields={"x": 1}, source="test",
        ))
        assert len(rowset.store) == 1
    finally:
        rowset.store.close()


def test_open_session_creates_missing_parent_directories(tmp_path):
    path = tmp_path / "new-project" / "runs" / "agent.db"
    rowset = open_session(path)
    try:
        assert path.is_file()
    finally:
        rowset.store.close()


@pytest.mark.parametrize("budgets", [
    {"tokens_out": -1},
    {"tokens_out": float("nan")},
    {"tokens_out": float("inf")},
    {"tokens_out": True},
    {"": 1},
])
def test_capability_row_rejects_invalid_budget_limits(budgets):
    with pytest.raises(ValueError):
        CapabilityRow(budgets=budgets)  # type: ignore[arg-type]


@pytest.mark.parametrize("amount", [True, float("nan"), float("inf"), -1])
def test_capability_row_rejects_invalid_spend_amounts(amount):
    from lipas.store import ClaimStore
    from lipas.rows import RowSet

    rowset = RowSet(ClaimStore(), [CapabilityRow(budgets={"tokens_out": 100})])
    claim = Claim(
        tag="resource_spent",
        fields={"bucket": "tokens_out", "amount": amount},
        source="test",
    )
    with pytest.raises(InvariantViolation, match="invalid bucket or amount"):
        rowset.fold(claim)


def test_claim_store_deduplicates_same_claim_id():
    from lipas.store import ClaimStore

    store = ClaimStore()
    claim = Claim(tag="observation", fields={"_history": [{"step": 1}]}, claim_id="once")
    store.fold(claim)
    store.fold(claim)

    assert len(store) == 1
    assert store.seq == 1


def test_claim_store_rejects_conflicting_claim_id():
    from lipas.store import ClaimStore

    store = ClaimStore()
    store.fold(Claim(tag="observation", fields={"x": 1}, claim_id="once"))

    with pytest.raises(ClaimIdConflict, match="reused"):
        store.fold(Claim(tag="observation", fields={"x": 2}, claim_id="once"))


def test_sqlite_session_deduplicates_same_claim_id(db_path):
    rowset = open_session(db_path)
    claim = Claim(tag="observation", fields={"_history": [{"step": 1}]}, claim_id="once")
    try:
        rowset.fold(claim)
        rowset.fold(claim)
        assert len(rowset.store) == 1
        assert rowset.store.seq == 1
    finally:
        rowset.store.close()


def test_sqlite_session_deduplicates_claim_id_after_reopen(db_path):
    claim = Claim(tag="observation", fields={"_history": [{"step": 1}]}, claim_id="once")
    first = open_session(db_path)
    try:
        first.fold(claim)
    finally:
        first.store.close()

    reopened = open_session(db_path)
    try:
        reopened.fold(claim)
        assert len(reopened.store) == 1
        assert reopened.store.seq == 1
    finally:
        reopened.store.close()
