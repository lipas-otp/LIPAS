"""LIPAS · Session — top-level convenience over SqliteClaimStore.

Two helpers:

  open_session(path, ...) -> RowSet
      Open or create a SQLite-backed RowSet wired with the standard
      row triple (Effect / History / Capability). Caller plugs the
      RowSet into LLM(...).

  replay(source_path, ...) -> contextmanager yielding ReplaySession
      Open a recorded session for re-execution. Default writes new
      claims to an in-memory store (Q6.c); pass into="other.db" to
      persist (Q6.b). The yielded ReplaySession exposes the
      replay_cursor / tool_replayer / target rowset to plug into LLM.

      A ReplaySession.stub_adapter() is provided so callers running
      pure-transcript replay (re_execute_llm=False) don't have to
      pass a live LLMAdapter; the stub raises if the cursor is
      somehow bypassed (drift signal, not silent fall-through).

Q1 / Q3 / Q4 defaults
---------------------
re_execute_llm          = False   (Q1.a — transcript replay)
strict_match            = True    (Q3 — fail loud on drift)
re_execute_side_effects = False   (Q4 — STRICT_TAPE substitution)

Override at the call site if you know what you're doing.
"""
from __future__ import annotations

import contextlib
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import AsyncIterator, ClassVar

from lipas.adapter import Request, ResourceEstimate, StreamEvent
from lipas.adapter.protocol import LLMAdapter
from lipas.calculus import StrategyRegistry
from lipas.replay import ReplayCursor, ReplayExhausted
from lipas.replay_tools import ReplayMode, ToolReplayer
from lipas.rows import RowSet
from lipas.rows.base import Row
from lipas.rows.capability import CapabilityRow
from lipas.rows.effect import EffectRow
from lipas.rows.history import HistoryRow
from lipas.serialization.store_sqlite import SqliteClaimStore
from lipas.store import ClaimStore

__all__ = [
    "open_session",
    "replay",
    "ReplaySession",
    "ReplayStubAdapter",
]


def _default_rows() -> list[Row]:
    """Standard row triple. Fresh instances per call (Row objects are
    cheap and stateless w.r.t. the store)."""
    return [EffectRow(), HistoryRow(), CapabilityRow()]


# =====================================================================
# open_session
# =====================================================================

def open_session(
    path: str,
    *,
    rows: Sequence[Row] | None = None,
    registry: StrategyRegistry | None = None,
) -> RowSet:
    """Open or create a SQLite-backed RowSet.

    Default rows: ``[EffectRow(), HistoryRow(), CapabilityRow()]``.

    Caller is responsible for closing via ``rowset.store.close()``
    when done. (The store is also a context manager, but RowSet is
    not — wrap the store directly if you want auto-close.)

    Examples
    --------
        rowset = open_session("session.db")
        try:
            llm = LLM(adapter=adapter, rowset=rowset, model="...", tools=[...])
            # ... agent loop ...
        finally:
            rowset.store.close()

    Or, equivalently:

        with SqliteClaimStore("session.db") as store:
            rowset = RowSet(store, [EffectRow(), HistoryRow(), CapabilityRow()])
            llm = LLM(adapter=adapter, rowset=rowset, ...)
            ...

    Parameters
    ----------
    path:
        SQLite path. Use ``":memory:"`` for an ephemeral store
        (useful in tests; behaves identically to disk-backed but
        does not survive process exit).
    rows:
        Override the default row set. Pass an empty list if you
        truly want a bare store (not recommended — replay and
        budget gating both depend on EffectRow / CapabilityRow).
    registry:
        Override the default StrategyRegistry. Most callers should
        leave this as None.
    """
    store = SqliteClaimStore(path, registry=registry)
    chosen = list(rows) if rows is not None else _default_rows()
    return RowSet(store, chosen)


# =====================================================================
# Replay stub adapter
# =====================================================================

@dataclass
class ReplayStubAdapter:
    """LLMAdapter that fails any call.

    Plugs into ``LLM(adapter=...)`` when running transcript replay
    (replay_cursor set, re_execute_llm=False). The cursor handles
    every recorded call without touching the adapter; if the agent
    code somehow makes an extra LLM call the cursor doesn't have a
    record for, this stub raises rather than silently degrading
    into a live call.

    This is intentional: cursor exhaustion is a drift signal —
    "the agent code today is making more LLM calls than the recorded
    run did" — and silently falling through to a real adapter would
    both bill real money and corrupt the replay invariant.
    """

    name: ClassVar[str] = "replay-stub"

    async def estimate_cost(self, request: Request) -> ResourceEstimate:
        raise ReplayExhausted(
            "ReplayStubAdapter.estimate_cost called: replay cursor was "
            "exhausted (or never engaged) and the agent attempted a live "
            "LLM call. Either the agent code drifted from the recorded "
            "run, or replay was misconfigured. Pass a real LLMAdapter "
            "to LLM(...) if you intend re-execution."
        )

    async def stream(
        self, request: Request,
    ) -> AsyncIterator[StreamEvent]:
        raise ReplayExhausted(
            "ReplayStubAdapter.stream called — see estimate_cost "
            "docstring for explanation."
        )
        # Unreachable; the bare yield below makes this an async
        # generator function so the protocol's AsyncIterator return
        # type holds at the type-checker level.
        yield  # pragma: no cover


# =====================================================================
# ReplaySession
# =====================================================================

@dataclass
class ReplaySession:
    """Active replay context.

    Plug ``replay_cursor`` / ``tool_replayer`` / ``rowset`` directly
    into ``LLM(...)`` to drive the re-run.

    Fields
    ------
    rowset:
        The TARGET rowset. New folds (effect_intent / effect_result /
        replay_decision / spend) land here.
    replay_cursor:
        Pass to ``LLM(replay_cursor=...)``. ``None`` when
        re_execute_llm was True — in that case you must pass a real
        adapter.
    tool_replayer:
        Pass to ``LLM(tool_replayer=...)``. Always present (cheap
        to construct, harmless if there are no tools).
    source_store:
        The opened source SQLite store. Exposed so callers can run
        ad-hoc queries via ``source_store.filter(tag=...)``. Closed
        on context exit.
    target_store:
        Same object as ``rowset.store``. Exposed for symmetry; also
        closed on context exit (only if SQLite-backed).
    """

    rowset:        RowSet
    replay_cursor: ReplayCursor | None
    tool_replayer: ToolReplayer
    source_store:  SqliteClaimStore
    target_store:  ClaimStore | SqliteClaimStore

    def stub_adapter(self) -> LLMAdapter:
        """Return a ReplayStubAdapter for transcript-replay use.

        Calling this only makes sense when ``replay_cursor`` is set
        (transcript replay). For re-execution you should pass a
        real adapter to ``LLM(...)`` instead.
        """
        return ReplayStubAdapter()


# =====================================================================
# replay()
# =====================================================================

@contextlib.contextmanager
def replay(
    source_path: str,
    *,
    into:                    str | None = None,
    mode:                    ReplayMode = ReplayMode.STRICT_TAPE,
    re_execute_llm:          bool       = False,
    re_execute_side_effects: bool       = False,
    rows:                    Sequence[Row] | None = None,
    strict_match:            bool       = True,
    allow_external_write:    bool       = False,
    allow_class_downgrade:   bool       = False,
) -> Iterator[ReplaySession]:
    """Open a recorded session for re-execution.

    Defaults (Q1 / Q3 / Q4):
      re_execute_llm          = False   — transcript replay
      strict_match            = True    — fail loud on drift
      re_execute_side_effects = False   — STRICT_TAPE substitution

    Parameters
    ----------
    source_path:
        Path to the recorded ``.db``. Opened, read, closed — never
        written to by replay() itself.
    into:
        Path to write new claims into. ``None`` (default) means an
        in-memory ClaimStore — Q6.c, "look once, throw away". Pass
        a path to create a NEW SqliteClaimStore there — Q6.b,
        "compare old vs new audit trails side by side".
    mode:
        ReplayMode for the ToolReplayer. STRICT_TAPE is the default
        (substitute when found, fail when not). BEST_EFFORT
        substitutes when found, re-executes when not. LIVE_REROUTE
        re-executes against live systems with class-aware refusal.
    re_execute_llm:
        If True, do NOT build a ReplayCursor. The caller passes a
        real LLMAdapter and the LLM is genuinely re-run against the
        same prompts. Useful for "would a different prompt produce
        a different answer against the same recorded tools?"
    re_execute_side_effects:
        Ergonomic alias for ``mode=LIVE_REROUTE,
        allow_external_write=True``. Mutually exclusive with
        passing a non-default ``mode=``.
    rows:
        Row set for the TARGET store. Default is the standard triple.
    strict_match:
        Forwarded to ReplayCursor. True = compare model + system on
        each call; raise ReplayMismatch on drift.
    allow_external_write, allow_class_downgrade:
        Forwarded to ToolReplayer. See replay_tools docstrings.

    Yields
    ------
    ReplaySession with target rowset + cursor + replayer wired.

    On exit, both source and target stores are closed.
    """
    # Q4 ergonomic alias.
    if re_execute_side_effects:
        if mode is not ReplayMode.STRICT_TAPE:
            raise ValueError(
                "re_execute_side_effects=True is mutually exclusive "
                "with explicit mode=; pick one."
            )
        mode = ReplayMode.LIVE_REROUTE
        allow_external_write = True

    source_store: SqliteClaimStore | None = None
    target_store: ClaimStore | SqliteClaimStore | None = None

    try:
        source_store = SqliteClaimStore(source_path)

        # Project effect view from source. Fresh EffectRow is fine
        # — project() reads the log, doesn't depend on prior
        # registration.
        view = EffectRow().project(source_store)

        # Cursor (transcript) or None (re-execute).
        cursor = (
            None if re_execute_llm
            else ReplayCursor.from_view(view, strict_match=strict_match)
        )

        # ToolReplayer. Pass an explicit frozen_max_seq derived from
        # the source store's overall log length, NOT from the auto-
        # capture (which only walks tool nodes and would fail with
        # ReplayConfigError on LLM-only sessions under STRICT_TAPE).
        replayer = ToolReplayer(
            view=view,
            mode=mode,
            allow_external_write=allow_external_write,
            allow_class_downgrade=allow_class_downgrade,
            frozen_max_seq=source_store.seq,
        )

        # Target store.
        if into is None:
            target_store = ClaimStore()
        else:
            target_store = SqliteClaimStore(into)

        target_rows = list(rows) if rows is not None else _default_rows()
        target_rowset = RowSet(target_store, target_rows)

        yield ReplaySession(
            rowset=target_rowset,
            replay_cursor=cursor,
            tool_replayer=replayer,
            source_store=source_store,
            target_store=target_store,
        )

    finally:
        # Always close both. Suppress on close to avoid masking
        # whatever exception is unwinding (the original exception
        # is the interesting one).
        if source_store is not None:
            try:
                source_store.close()
            except Exception:
                pass
        if isinstance(target_store, SqliteClaimStore):
            try:
                target_store.close()
            except Exception:
                pass
