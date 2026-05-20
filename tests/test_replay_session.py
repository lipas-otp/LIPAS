"""Tests for lipas.session.replay()."""
from __future__ import annotations

import asyncio
import os
import tempfile

import pytest

from lipas.adapter import Reply, Usage
from lipas.calculus import Claim
from lipas.llm import LLM
from lipas.replay import ReplayExhausted
from lipas.replay_tools import ReplayMode
from lipas.session import open_session, replay
from lipas.testing.fake_adapter import FakeAdapter


# ── helpers ────────────────────────────────────────────────────

@pytest.fixture
def db_dir():
    with tempfile.TemporaryDirectory() as d:
        yield d


def _mk_reply(text: str, model: str = "fake") -> Reply:
    return Reply(
        content=({"type": "text", "text": text},),
        usage=Usage(input=10, output=5),
        stop_reason="end_turn",
        model=model,
        error_detail=None,
    )


def _record_one(db_path: str, text: str = "hello") -> None:
    """Record exactly one LLM call into db_path."""
    rowset = open_session(db_path)
    try:
        adapter = FakeAdapter.from_replies([_mk_reply(text)])
        llm = LLM(adapter=adapter, rowset=rowset, model="fake")
        reply = asyncio.run(llm([{"role": "user", "content": "hi"}]))
        assert reply.text == text
    finally:
        rowset.store.close()


# ── tests ──────────────────────────────────────────────────────

def test_replay_transcript_round_trip(db_dir):
    """Record one call → replay → same text, no real adapter contact."""
    db = os.path.join(db_dir, "session.db")
    _record_one(db, "hello")

    with replay(db) as r:
        assert r.replay_cursor is not None
        assert r.tool_replayer is not None

        llm = LLM(
            adapter       = r.stub_adapter(),
            rowset        = r.rowset,
            model         = "fake",
            replay_cursor = r.replay_cursor,
            tool_replayer = r.tool_replayer,
        )
        reply = asyncio.run(llm([{"role": "user", "content": "hi"}]))
        assert reply.text == "hello"


def test_replay_default_target_is_in_memory(db_dir):
    """Q6.c default — target_store is ClaimStore, not SqliteClaimStore."""
    db = os.path.join(db_dir, "session.db")
    _record_one(db)

    from lipas.store import ClaimStore
    from lipas.serialization.store_sqlite import SqliteClaimStore

    with replay(db) as r:
        assert isinstance(r.target_store, ClaimStore)
        assert not isinstance(r.target_store, SqliteClaimStore)


def test_replay_into_new_db_persists(db_dir):
    """Q6.b — into= writes to a separate SQLite file that survives close.

    We test the persistence contract directly by folding an explicit
    probe claim into the target rowset, then re-opening the file. We
    deliberately do NOT depend on whether transcript-replay LLM calls
    fold effect_* claims (an orthogonal concern owned by lipas.llm).
    """
    src = os.path.join(db_dir, "src.db")
    dst = os.path.join(db_dir, "dst.db")
    _record_one(src, "hi")

    with replay(src, into=dst) as r:
        from lipas.serialization.store_sqlite import SqliteClaimStore
        assert isinstance(r.target_store, SqliteClaimStore)

        # Persistence probe — explicit fold into the target.
        r.rowset.store.fold(Claim(
            tag="replay_probe", fields={"k": 1}, source="test",
        ))

    # File survives context exit; probe survives reopen.
    assert os.path.exists(dst)
    re_open = open_session(dst)
    try:
        tags = [c.tag for c in re_open.store.log]
        assert "replay_probe" in tags
    finally:
        re_open.store.close()


def test_replay_stub_adapter_raises_when_cursor_exhausted(db_dir):
    """Cursor exhaustion → second call hits stub → ReplayExhausted."""
    db = os.path.join(db_dir, "short.db")
    _record_one(db, "only one")

    with replay(db) as r:
        llm = LLM(
            adapter       = r.stub_adapter(),
            rowset        = r.rowset,
            model         = "fake",
            replay_cursor = r.replay_cursor,
            tool_replayer = r.tool_replayer,
        )
        # First call: served by cursor.
        reply = asyncio.run(llm([{"role": "user", "content": "1"}]))
        assert reply.text == "only one"

        # Second call: cursor exhausted → stub raises.
        with pytest.raises(ReplayExhausted):
            asyncio.run(llm([{"role": "user", "content": "2"}]))


def test_replay_re_execute_llm_uses_real_adapter(db_dir):
    """re_execute_llm=True: cursor is None, real adapter drives."""
    db = os.path.join(db_dir, "re_exec.db")
    _record_one(db, "recorded")

    with replay(db, re_execute_llm=True) as r:
        assert r.replay_cursor is None

        new_adapter = FakeAdapter.from_replies([_mk_reply("re-executed")])
        llm = LLM(
            adapter       = new_adapter,
            rowset        = r.rowset,
            model         = "fake",
            tool_replayer = r.tool_replayer,
        )
        reply = asyncio.run(llm([{"role": "user", "content": "x"}]))
        assert reply.text == "re-executed"
        assert new_adapter.calls_made == 1


@pytest.mark.filterwarnings(
    "ignore::lipas.replay_tools.LipasDangerousReplayWarning"
)
def test_replay_re_execute_side_effects_implies_live_reroute(db_dir):
    """Q4 ergonomic alias maps to LIVE_REROUTE + allow_external_write.

    The dangerous-replay warning is *expected* here — that's the
    whole point of the alias — so we filter it locally.
    """
    db = os.path.join(db_dir, "se.db")
    _record_one(db)

    with replay(db, re_execute_side_effects=True) as r:
        assert r.tool_replayer.mode is ReplayMode.LIVE_REROUTE
        assert r.tool_replayer.allow_external_write is True


def test_replay_re_execute_side_effects_conflicts_with_explicit_mode(db_dir):
    """Q4 alias + explicit mode= → ValueError."""
    db = os.path.join(db_dir, "se.db")
    _record_one(db)

    with pytest.raises(ValueError, match="mutually exclusive"):
        with replay(
            db,
            re_execute_side_effects=True,
            mode=ReplayMode.BEST_EFFORT,
        ):
            pass


def test_replay_closes_both_stores_on_exit(db_dir):
    """source_store and (sqlite-backed) target_store both closed on exit."""
    src = os.path.join(db_dir, "src.db")
    dst = os.path.join(db_dir, "dst.db")
    _record_one(src)

    with replay(src, into=dst) as r:
        s, t = r.source_store, r.target_store
        assert not s.closed
        from lipas.serialization.store_sqlite import SqliteClaimStore
        assert isinstance(t, SqliteClaimStore)
        assert not t.closed

    assert s.closed
    assert t.closed


def test_replay_closes_source_on_exception(db_dir):
    """Exception inside the with-block still closes source_store."""
    db = os.path.join(db_dir, "ex.db")
    _record_one(db)

    captured: list = []
    with pytest.raises(RuntimeError, match="boom"):
        with replay(db) as r:
            captured.append(r.source_store)
            raise RuntimeError("boom")

    assert captured[0].closed
