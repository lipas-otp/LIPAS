"""Tests for lipas.session.open_session."""
from __future__ import annotations

import os
import tempfile

import pytest

from lipas.calculus import Claim
from lipas.rows.capability import CapabilityRow
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
