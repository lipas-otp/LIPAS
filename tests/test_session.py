"""Tests for lipas.session.open_session."""
from __future__ import annotations

import asyncio
import os
import tempfile

import pytest

from lipas.calculus import Claim
from lipas.agent import Agent
from lipas.exceptions import ClaimIdConflict
from lipas.rows.capability import CapabilityRow
from lipas.rows.base import InvariantViolation
from lipas.rows.effect import EffectRow
from lipas.adapter import Reply, Request, Usage
from lipas.effect import (
    EffectKind,
    F_ATTEMPTS,
    F_DECLARED_SIDE_EFFECT,
    F_EFFECT_ID,
    F_KIND,
    F_MODEL,
    F_OUTPUT,
    F_REPLY,
    F_SIDE_EFFECT,
    F_SPEND,
    F_TOOL_NAME,
    F_TOTAL_USAGE,
    TAG_EFFECT_INTENT,
    TAG_EFFECT_RESULT,
)
from lipas.rows.history import HistoryRow
from lipas.serialization.store_sqlite import SqliteClaimStore
from lipas.session import open_session
from tests.fake_adapter import FakeAdapter


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


def test_agent_session_restores_complete_message_history_across_reopen(tmp_path):
    path = tmp_path / "chat.db"

    first_adapter = FakeAdapter.echoing()
    first_agent = Agent(
        adapter=first_adapter,
        model="fake",
        session_path=path,
    )
    try:
        first = first_agent.session(session_id="chat")
        asyncio.run(first.run("first turn"))
    finally:
        first_agent.close()

    second_adapter = FakeAdapter.echoing()
    second_agent = Agent(
        adapter=second_adapter,
        model="fake",
        session_path=path,
    )
    try:
        second = second_agent.session(session_id="chat")
        asyncio.run(second.run("second turn"))
        messages = second_adapter.seen_requests[0].messages
        assert [message["role"] for message in messages] == [
            "user", "assistant", "user",
        ]
        assert messages[0]["content"] == "first turn"
        assert "echo: first turn" in str(messages[1]["content"])
        assert messages[2]["content"] == "second turn"
    finally:
        second_agent.close()


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


def test_claim_copy_helpers_preserve_source_provenance():
    claim = Claim(
        tag="observation",
        fields={"x": 1},
        source="agent.react",
        claim_id="source-1",
    )

    assert claim.with_field("y", 2).source == "agent.react"
    assert claim.with_fields({"z": 3}).source == "agent.react"


def test_claim_store_snapshots_admitted_claims_and_read_results():
    from lipas.store import ClaimStore

    store = ClaimStore()
    claim = Claim(
        tag="observation",
        fields={"nested": {"values": [1]}},
        claim_id="immutable",
        seq=999,
    )
    store.fold(claim)
    claim.fields["nested"]["values"].append(2)
    exposed = store.log[0]
    exposed.fields["nested"]["values"].append(3)

    assert store.log[0].fields == {"nested": {"values": [1]}}
    assert store.log[0].seq == 999
    claim.tag = "rewritten"
    assert store.log[0].tag == "observation"


def test_sqlite_store_memory_mirror_cannot_be_mutated_by_caller(db_path):
    store = SqliteClaimStore(db_path)
    claim = Claim(
        tag="observation",
        fields={"nested": {"values": [1]}},
        claim_id="immutable",
    )
    try:
        store.fold(claim)
        claim.fields["nested"]["values"].append(2)
        store.filter(tag="observation")[0].fields["nested"]["values"].append(3)
        assert store.log[0].fields == {"nested": {"values": [1]}}
    finally:
        store.close()

    reopened = SqliteClaimStore(db_path)
    try:
        assert reopened.log[0].fields == {"nested": {"values": [1]}}
    finally:
        reopened.close()


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


def test_effect_row_rejects_invalid_retry_usage_before_recovery_reads_it():
    from lipas.rows import RowSet
    from lipas.store import ClaimStore

    rowset = RowSet(ClaimStore(), [EffectRow()])
    request = Request("fake", [{"role": "user", "content": "hi"}], 10)
    reply = Reply((), Usage(), "end_turn", "fake")
    rowset.fold(Claim(
        tag=TAG_EFFECT_INTENT,
        fields={
            F_EFFECT_ID: "call_abcdef012345",
            F_KIND: EffectKind.LLM_CALL.value,
            F_MODEL: "fake",
            "request": request,
        },
    ))

    with pytest.raises(InvariantViolation, match="total_usage.*Usage"):
        rowset.fold(Claim(
            tag=TAG_EFFECT_RESULT,
            fields={
                F_EFFECT_ID: "call_abcdef012345",
                F_KIND: EffectKind.LLM_CALL.value,
                "status": "ok",
                F_ATTEMPTS: 1,
                F_REPLY: reply,
                F_TOTAL_USAGE: {"input": 1},
            },
        ))


def test_effect_row_rejects_invalid_tool_spend_before_recovery_reads_it():
    from lipas.rows import RowSet
    from lipas.store import ClaimStore

    rowset = RowSet(ClaimStore(), [EffectRow()])
    rowset.fold(Claim(
        tag=TAG_EFFECT_INTENT,
        fields={
            F_EFFECT_ID: "tool_abcdef012345",
            F_KIND: EffectKind.TOOL_CALL.value,
            F_TOOL_NAME: "write_note",
            "arguments": {},
            F_DECLARED_SIDE_EFFECT: "idempotent_write",
        },
    ))

    with pytest.raises(InvariantViolation, match="invalid spend entry"):
        rowset.fold(Claim(
            tag=TAG_EFFECT_RESULT,
            fields={
                F_EFFECT_ID: "tool_abcdef012345",
                F_KIND: EffectKind.TOOL_CALL.value,
                "status": "ok",
                F_ATTEMPTS: 1,
                F_OUTPUT: "saved",
                F_SIDE_EFFECT: "idempotent_write",
                F_SPEND: {"tool_calls": float("nan")},
            },
        ))
