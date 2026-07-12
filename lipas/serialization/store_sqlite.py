"""SQLite-backed ClaimStore.

A drop-in replacement for the in-memory ``ClaimStore`` whose log
persists to a SQLite database. The interface is identical from the
RowSet's perspective: ``fold``, ``merged``, ``log``, ``filter``, the
properties, and iteration.

Current guarantees
------------------
- Single-process, single-writer. No file locking, no PRAGMA tuning.
- Append-only ``claims`` table; reopening the file replays every row
  through ``merge`` to reconstruct the materialized state from scratch.
- ``meta`` table tracks ``lipas_schema_version`` (currently 1),
  ``store_id``, ``created_at``. Schema-version mismatch raises at
  open time — automatic schema migration is intentionally not provided.

Current limits
--------------
- Concurrent writers: opening the same file from two processes races.
- Snapshots / log compaction: open is O(N) in claim count.
- Strategy-registry persistence: the registry is *code*, not data.
  Callers MUST reconstruct an equivalent registry on reopen — same
  rows, same ordering of ``register_strategies`` calls. This is
  reopening with a different registry MAY produce a different ``merged``
  claim. Use the same registry for recording and inspection.
- BeliefContext persistence: also code-side. Same caveat.

Drop-in caveat
--------------
``__init__`` takes a ``path`` parameter that the in-memory
``ClaimStore`` does not. RowSet doesn't care (it only consumes the
duck-typed protocol). Code that constructs the store directly needs
to know which class it's using.

Connection lifecycle
--------------------
The connection is owned by the store. ``close()`` is provided for
explicit shutdown; relying on GC works for short-lived processes
but not recommended for long-running services.
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Iterator

from ..calculus import (
    BOTTOM, BeliefContext, Claim, StrategyRegistry,
    make_default_registry, merge,
)
from ..exceptions import ClaimIdConflict, LipasError
from ..store import _same_claim_payload
from .codec import (CodecRegistry, decode, encode, make_default_codec_registry)


# Bumped only on incompatible schema change.
SCHEMA_VERSION = 1


def ensure_sqlite_parent(path: str | Path) -> None:
    """Create a normal SQLite path's parent directory when it is missing.

    SQLite creates a database file but not its containing directory. High-level
    LIPAS paths such as ``runs/agent.db`` should work in a new project, while
    ``:memory:`` and SQLite URI paths remain entirely caller-controlled.
    """
    # SQLite accepts ``Path`` instances as well as strings.  Treat URI paths
    # specially: their location and creation semantics belong to SQLite, not
    # to this small convenience helper.
    if isinstance(path, str) and (path == ":memory:" or path.startswith("file:")):
        return
    Path(path).expanduser().parent.mkdir(parents=True, exist_ok=True)


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS claims (
    seq             INTEGER PRIMARY KEY,        -- == Claim.seq, fold order
    claim_id        TEXT    NOT NULL UNIQUE,
    tag             TEXT    NOT NULL,
    kind            TEXT,                       -- nullable, == Claim.kind
    priority        INTEGER NOT NULL,
    schema_version  INTEGER NOT NULL,
    source          TEXT    NOT NULL DEFAULT '',
    fields_json     TEXT    NOT NULL,           -- tagged-JSON via codec
    ts              REAL    NOT NULL            -- audit clock; non-fold input
);

CREATE INDEX IF NOT EXISTS idx_claims_tag ON claims(tag);
"""


class SchemaVersionMismatch(LipasError):
    """SQLite store on disk uses a schema version this runtime doesn't speak."""


class SqliteClaimStore:
    """SQLite-backed ClaimStore. Same surface as ``ClaimStore``."""

    # Mirrors ClaimStore's __slots__ shape, plus persistence members.
    __slots__ = (
        "_path", "_conn",
        "_registry", "_ctx", "_codecs",
        "_merged", "_log", "_seq", "_by_tag", "_by_id",
        "_closed",
    )

    # ── construction ──────────────────────────────────────────

    def __init__(
        self,
        path: str | Path,
        *,
        registry:       StrategyRegistry | None = None,
        ctx:            BeliefContext     | None = None,
        codec_registry: CodecRegistry     | None = None,
    ) -> None:
        """Open or create the SQLite store at *path*.

        Use ``":memory:"`` for an ephemeral in-process database
        (useful for tests; behaves identically to disk-backed but
        does not survive process exit).

        Raises
        ------
        SchemaVersionMismatch
            File on disk was written by a different lipas runtime.
        UnserializableClaim
            On reload, a recorded claim contains a tag with no codec.
        """
        self._path     = path
        self._registry = registry or make_default_registry()
        self._ctx      = ctx or BeliefContext()
        self._codecs   = codec_registry or make_default_codec_registry()

        # In-memory mirror of the on-disk log. Identical to ClaimStore's
        # private state — RowSet's filter/merged/log all read from here,
        # not from SQL. The DB is the durable source of truth; this is
        # the hot path.
        self._merged: Claim = BOTTOM
        self._log:    list[Claim] = []
        self._seq:    int = 0
        self._by_tag: dict[str, list[int]] = {}
        self._by_id:  dict[str, Claim] = {}

        ensure_sqlite_parent(path)
        # check_same_thread=True (the default) is intentional — single-process
        # writer ownership is part of the contract; sharing across threads
        # without serialization would silently corrupt the journal.
        self._conn = sqlite3.connect(path)
        self._closed = False

        try:
            self._init_schema()
            self._load_existing()
        except BaseException:
            self._conn.close()
            self._closed = True
            raise

    # ── schema bootstrap ──────────────────────────────────────

    def _init_schema(self) -> None:
        with self._conn:
            self._conn.executescript(_SCHEMA_SQL)

        cur = self._conn.execute(
            "SELECT value FROM meta WHERE key = 'lipas_schema_version'"
        )
        row = cur.fetchone()

        if row is None:
            # Fresh store. Stamp meta.
            with self._conn:
                self._conn.executemany(
                    "INSERT INTO meta (key, value) VALUES (?, ?)",
                    [
                        ("lipas_schema_version", str(SCHEMA_VERSION)),
                        ("store_id",   uuid.uuid4().hex),
                        ("created_at", repr(time.time())),
                    ],
                )
            return

        try:
            existing = int(row[0])
        except (TypeError, ValueError):
            raise SchemaVersionMismatch(
                f"meta.lipas_schema_version is not an int: {row[0]!r}"
            )
        if existing != SCHEMA_VERSION:
            raise SchemaVersionMismatch(
                f"sqlite store at {self._path!r} is schema version "
                f"{existing}; this lipas runtime supports {SCHEMA_VERSION}. "
                "No automatic migration is available."
            )

    def _load_existing(self) -> None:
        """Replay every row through ``merge`` to rebuild merged state."""
        cur = self._conn.execute(
            "SELECT seq, claim_id, tag, kind, priority, source, "
            "       fields_json, schema_version "
            "FROM claims ORDER BY seq ASC"
        )
        last_seq = -1
        for (seq, claim_id, tag, kind, priority,
             source, fields_json, schema_v) in cur:

            # Per-row version check mirrors the meta-level version. A future
            # migration layer may dispatch persisted-shape upgrades here.
            if schema_v != SCHEMA_VERSION:
                raise SchemaVersionMismatch(
                    f"claim seq={seq} written under schema {schema_v}; "
                    f"runtime is {SCHEMA_VERSION}"
                )

            encoded_fields = json.loads(fields_json)
            fields = decode(encoded_fields, self._codecs)
            if not isinstance(fields, dict):
                raise LipasError(
                    f"claim seq={seq}: decoded fields is not a dict, "
                    f"got {type(fields).__name__}"
                )

            claim = Claim(
                tag=tag,
                fields=fields,
                kind=kind,
                priority=priority,
                source=source or "",
                claim_id=claim_id,
                seq=seq,
            )

            idx = len(self._log)
            self._log.append(claim)
            self._by_tag.setdefault(tag, []).append(idx)
            self._by_id[claim.claim_id] = claim
            self._merged = merge(self._merged, claim, self._ctx, self._registry)
            last_seq = seq

        # Next-fold seq picks up where the on-disk log left off.
        # Note Claim.seq is logical (per-store monotone); not gap-free
        # if a future migration ever rewrites history (none planned).
        self._seq = last_seq + 1 if last_seq >= 0 else 0

    # ── writes ────────────────────────────────────────────────

    def fold(self, claim: Claim) -> Claim:
        """Append a claim; return the updated merged state.

        Mirrors ``ClaimStore.fold`` byte-for-byte on the in-memory
        side. Persists to SQLite inside the same call — no separate
        flush. Single-writer assumption: no other process should be
        writing to this file concurrently.
        """
        if self._closed:
            raise LipasError("SqliteClaimStore is closed")

        existing = self._by_id.get(claim.claim_id)
        if existing is not None:
            if _same_claim_payload(existing, claim):
                return self._merged
            raise ClaimIdConflict(
                f"claim_id={claim.claim_id!r} was reused for a different claim"
            )

        if claim.seq < 0:
            claim = replace(claim, seq=self._seq)

        # Encode + persist FIRST. If serialization fails we must not
        # corrupt the in-memory log — a half-folded state would break
        # equality between the in-memory mirror and the on-disk log.
        encoded = encode(claim.fields, self._codecs)
        # sort_keys for deterministic on-disk bytes; ensure_ascii=False
        # because lipas messages are routinely non-ASCII (CJK content).
        fields_json = json.dumps(
            encoded,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        )

        try:
            with self._conn:
                self._conn.execute(
                    "INSERT INTO claims "
                    "(seq, claim_id, tag, kind, priority, "
                    " schema_version, source, fields_json, ts) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        claim.seq, claim.claim_id, claim.tag, claim.kind,
                        int(claim.priority), SCHEMA_VERSION,
                        claim.source or "",
                        fields_json, time.time(),
                    ),
                )
        except sqlite3.IntegrityError as e:
            raise LipasError(
                f"sqlite fold failed for claim_id={claim.claim_id!r} "
                f"seq={claim.seq}: {e}"
            ) from e

        # Persist succeeded → mirror in memory.
        idx = len(self._log)
        self._log.append(claim)
        self._by_tag.setdefault(claim.tag, []).append(idx)
        self._by_id[claim.claim_id] = claim
        self._seq += 1
        self._merged = merge(self._merged, claim, self._ctx, self._registry)
        return self._merged

    # ── reads (identical surface to ClaimStore) ───────────────

    @property
    def merged(self) -> Claim:
        return self._merged

    @property
    def log(self) -> tuple[Claim, ...]:
        return tuple(self._log)

    @property
    def registry(self) -> StrategyRegistry:
        return self._registry

    @property
    def ctx(self) -> BeliefContext:
        return self._ctx

    @property
    def seq(self) -> int:
        return self._seq

    @property
    def path(self) -> str:
        return self._path

    @property
    def closed(self) -> bool:
        return self._closed

    def __len__(self) -> int:
        return len(self._log)

    def __iter__(self) -> Iterator[Claim]:
        return iter(self._log)

    def filter(
        self, *,
        tag:    str | None = None,
        kind:   str | None = None,
        source: str | None = None,
    ) -> list[Claim]:
        # Identical algorithm to ClaimStore.filter: index-by-tag for
        # the common single-arg path, fall back to linear scan.
        if tag is not None and kind is None and source is None:
            return [self._log[i] for i in self._by_tag.get(tag, ())]
        out: list[Claim] = []
        for c in self._log:
            if tag    is not None and c.tag    != tag:    continue
            if kind   is not None and c.kind   != kind:   continue
            if source is not None and c.source != source: continue
            out.append(c)
        return out

    # ── lifecycle ─────────────────────────────────────────────

    def close(self) -> None:
        """Close the underlying connection. Idempotent."""
        if self._closed:
            return
        self._conn.close()
        self._closed = True

    def __enter__(self) -> "SqliteClaimStore":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    def __del__(self) -> None:
        # Best-effort. CPython GC ordering can mean self._conn is
        # already torn down; suppress.
        try:
            self.close()
        except Exception:
            pass

    def __repr__(self) -> str:
        state = "closed" if self._closed else "open"
        return (
            f"SqliteClaimStore(path={self._path!r}, "
            f"size={len(self._log)}, "
            f"tags={sorted(self._by_tag)}, "
            f"state={state})"
        )
