"""SQLite-backed ClaimStore.

A drop-in replacement for the in-memory ``ClaimStore`` whose log
persists to a SQLite database. The interface is identical from the
RowSet's perspective: ``fold``, ``merged``, ``log``, ``filter``, the
properties, and iteration.

Current guarantees
------------------
- WAL-backed, bounded-contention SQLite writes with atomic sequence
  allocation. Multiple connections may append to one tape.
- Append-only ``claims`` table with claim-id idempotency.
- Versioned projection snapshots accelerate reopen. Snapshots are derived
  cache only; the Claim tape remains the source of truth and can rebuild them.
- ``meta`` table tracks ``lipas_schema_version`` (currently 1),
  ``store_id``, ``created_at``. Schema-version mismatch raises at
  open time — automatic schema migration is intentionally not provided.

Current limits
--------------
- SQLite still has one physical writer. High write contention is bounded, not
  converted into distributed multi-writer storage.
- The first open without a compatible projection snapshot remains O(N).
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

from copy import deepcopy
from dataclasses import dataclass, replace
import hashlib
import hmac
import json
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Iterator

from ..calculus import (
    BOTTOM, BeliefContext, Claim, StrategyRegistry,
    make_default_registry, merge,
)
from ..exceptions import ClaimIdConflict, LipasError
from ..sqlite_storage import connect_sqlite, ensure_sqlite_parent, immediate_transaction
from ..store import ClaimStore, _same_claim_payload
from .codec import (CodecRegistry, decode, encode, make_default_codec_registry)


# Bumped only on incompatible schema change.
SCHEMA_VERSION = 1
PROJECTION_SNAPSHOT_VERSION = 2
DEFAULT_SNAPSHOT_INTERVAL = 256


@dataclass(frozen=True, slots=True)
class ClaimPage:
    """One bounded, stable sequence page from a Claim tape."""

    claims: tuple[Claim, ...]
    next_cursor: int
    has_more: bool


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
CREATE INDEX IF NOT EXISTS idx_claims_tag_seq ON claims(tag, seq);
CREATE INDEX IF NOT EXISTS idx_claims_source_seq ON claims(source, seq);
CREATE INDEX IF NOT EXISTS idx_claims_kind_seq ON claims(kind, seq);

CREATE TABLE IF NOT EXISTS claim_projection_snapshots (
    projection_key  TEXT    NOT NULL,
    snapshot_seq    INTEGER NOT NULL,
    snapshot_version INTEGER NOT NULL,
    claim_id        TEXT    NOT NULL,
    tag             TEXT    NOT NULL,
    kind            TEXT,
    priority        INTEGER NOT NULL,
    source          TEXT    NOT NULL DEFAULT '',
    fields_json     TEXT    NOT NULL,
    payload_sha256  TEXT,
    created_at      REAL    NOT NULL,
    PRIMARY KEY (projection_key, snapshot_seq)
);
CREATE INDEX IF NOT EXISTS idx_claim_snapshots_latest
    ON claim_projection_snapshots(projection_key, snapshot_seq DESC);
"""


class SchemaVersionMismatch(LipasError):
    """SQLite store on disk uses a schema version this runtime doesn't speak."""


class SqliteClaimStore(ClaimStore):
    """SQLite-backed ClaimStore. Same surface as ``ClaimStore``."""

    # Mirrors ClaimStore's __slots__ shape, plus persistence members.
    __slots__ = (
        "_path", "_conn",
        "_registry", "_ctx", "_codecs",
        "_merged", "_log", "_seq", "_by_tag", "_by_id",
        "_projection_key", "_snapshot_interval", "_snapshot_seq",
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
        snapshot_interval: int = DEFAULT_SNAPSHOT_INTERVAL,
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
        if (
            isinstance(snapshot_interval, bool)
            or not isinstance(snapshot_interval, int)
            or snapshot_interval < 1
        ):
            raise ValueError("snapshot_interval must be a positive int")
        self._snapshot_interval = snapshot_interval
        self._projection_key = _projection_fingerprint(
            self._registry, self._ctx,
        )
        self._snapshot_seq = -1

        # Resident projection tail. Before the first snapshot this mirrors the
        # complete log; after a snapshot it contains only the replay delta.
        # Historical filter/page/log reads come from indexed SQL, while merged
        # stays incremental and bounded by the newest snapshot.
        self._merged: Claim = BOTTOM
        self._log:    list[Claim] = []
        self._seq:    int = 0
        self._by_tag: dict[str, list[int]] = {}
        self._by_id:  dict[str, Claim] = {}

        ensure_sqlite_parent(path)
        # One store instance remains thread-confined. Multiple instances and
        # processes coordinate through SQLite WAL and BEGIN IMMEDIATE.
        self._conn = connect_sqlite(path)
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
            # Projection snapshots are disposable cache, so adding integrity
            # metadata is safe without migrating the authoritative Claim tape.
            snapshot_columns = {
                str(row[1]) for row in self._conn.execute(
                    "PRAGMA table_info(claim_projection_snapshots)",
                )
            }
            if "payload_sha256" not in snapshot_columns:
                self._conn.execute(
                    "ALTER TABLE claim_projection_snapshots "
                    "ADD COLUMN payload_sha256 TEXT",
                )

        cur = self._conn.execute(
            "SELECT value FROM meta WHERE key = 'lipas_schema_version'"
        )
        row = cur.fetchone()

        if row is None:
            # Fresh store. Stamp meta. Multiple independent Store instances
            # may bootstrap the same file concurrently; idempotent inserts
            # avoid turning that harmless race into a UNIQUE violation.
            with self._conn:
                self._conn.executemany(
                    "INSERT OR IGNORE INTO meta (key, value) VALUES (?, ?)",
                    [
                        ("lipas_schema_version", str(SCHEMA_VERSION)),
                        ("store_id",   uuid.uuid4().hex),
                        ("created_at", repr(time.time())),
                    ],
                )
            row = self._conn.execute(
                "SELECT value FROM meta WHERE key = 'lipas_schema_version'",
            ).fetchone()
            if row is None:
                raise SchemaVersionMismatch(
                    "meta.lipas_schema_version is missing after bootstrap",
                )

        try:
            existing = int(row[0])
        except (TypeError, ValueError) as exc:
            raise SchemaVersionMismatch(
                f"meta.lipas_schema_version is not an int: {row[0]!r}"
            ) from exc
        if existing != SCHEMA_VERSION:
            raise SchemaVersionMismatch(
                f"sqlite store at {self._path!r} is schema version "
                f"{existing}; this lipas runtime supports {SCHEMA_VERSION}. "
                "No automatic migration is available."
            )

    def _load_existing(self) -> None:
        """Restore the newest compatible projection, then replay its delta."""
        snapshots = self._conn.execute(
            "SELECT snapshot_seq,claim_id,tag,kind,priority,source,fields_json,"
            "payload_sha256 "
            "FROM claim_projection_snapshots WHERE projection_key=? "
            "AND snapshot_version=? ORDER BY snapshot_seq DESC LIMIT 3",
            (self._projection_key, PROJECTION_SNAPSHOT_VERSION),
        ).fetchall()
        after_seq = -1
        for snapshot in snapshots:
            try:
                snapshot_seq = int(snapshot[0])
                checksum = snapshot[7]
                if (
                    snapshot_seq < 0
                    or not isinstance(checksum, str)
                    or not hmac.compare_digest(
                        checksum,
                        _snapshot_digest(*snapshot[:7]),
                    )
                    or self._conn.execute(
                        "SELECT 1 FROM claims WHERE seq=?", (snapshot_seq,),
                    ).fetchone() is None
                ):
                    continue
                projection = self._decode_claim((
                    snapshot_seq, snapshot[1], snapshot[2], snapshot[3],
                    snapshot[4], snapshot[5], snapshot[6], SCHEMA_VERSION,
                ))
            except Exception:
                # Snapshots are an acceleration structure, never authority.
                # Try an older one and finally rebuild from the Claim tape.
                continue
            self._merged = projection
            after_seq = snapshot_seq
            self._snapshot_seq = snapshot_seq
            break
        cur = self._conn.execute(
            "SELECT seq, claim_id, tag, kind, priority, source, "
            "       fields_json, schema_version "
            "FROM claims WHERE seq>? ORDER BY seq ASC",
            (after_seq,),
        )
        last_seq = after_seq
        for row in cur:
            claim = self._decode_claim(row)
            self._admit_to_projection(claim)
            last_seq = claim.seq
        self._seq = last_seq + 1

    def _decode_claim(self, row: tuple) -> Claim:
        seq, claim_id, tag, kind, priority, source, fields_json, schema_v = row
        if schema_v != SCHEMA_VERSION:
            raise SchemaVersionMismatch(
                f"claim seq={seq} written under schema {schema_v}; "
                f"runtime is {SCHEMA_VERSION}",
            )
        encoded_fields = json.loads(
            fields_json,
            parse_constant=lambda raw: (_ for _ in ()).throw(
                ValueError(f"non-JSON numeric constant {raw!r}")
            ),
        )
        fields = decode(encoded_fields, self._codecs)
        if not isinstance(fields, dict):
            raise LipasError(
                f"claim seq={seq}: decoded fields is not a dict, "
                f"got {type(fields).__name__}",
            )
        return Claim(
            tag=tag,
            fields=fields,
            kind=kind,
            priority=priority,
            source=source or "",
            claim_id=claim_id,
            seq=seq,
        )

    def _admit_to_projection(self, claim: Claim) -> None:
        idx = len(self._log)
        self._log.append(claim)
        self._by_tag.setdefault(claim.tag, []).append(idx)
        self._by_id[claim.claim_id] = claim
        self._merged = merge(self._merged, claim, self._ctx, self._registry)
        self._seq = max(self._seq, claim.seq + 1)

    def _refresh_existing(self) -> int:
        """Incrementally project rows committed by another connection."""
        added = 0
        rows = self._conn.execute(
            "SELECT seq,claim_id,tag,kind,priority,source,fields_json,"
            "schema_version FROM claims WHERE seq>=? ORDER BY seq",
            (self._seq,),
        )
        for row in rows:
            self._admit_to_projection(self._decode_claim(row))
            added += 1
        return added

    # ── writes ────────────────────────────────────────────────

    def fold(self, claim: Claim) -> Claim:
        """Append a claim; return the updated merged state.

        Sequence allocation and claim-id admission happen under
        ``BEGIN IMMEDIATE``. A second connection can race safely: identical
        delivery becomes a no-op and conflicting identity is rejected.
        """
        if self._closed:
            raise LipasError("SqliteClaimStore is closed")

        claim = deepcopy(claim)
        encoded = encode(claim.fields, self._codecs)
        fields_json = json.dumps(
            encoded,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        )

        admitted: Claim | None = None
        # Rebuild a stale projection outside writer ownership. Only the small
        # race delta must then be refreshed under BEGIN IMMEDIATE.
        self._refresh_existing()
        with immediate_transaction(self._conn):
            self._refresh_existing()
            existing = self._claim_by_id(claim.claim_id)
            if existing is not None:
                if _same_claim_payload(existing, claim):
                    return deepcopy(self._merged)
                raise ClaimIdConflict(
                    f"claim_id={claim.claim_id!r} was reused for a different claim",
                )
            if claim.seq < 0:
                claim = replace(claim, seq=self._seq)
            elif claim.seq != self._seq:
                raise LipasError(
                    f"claim seq={claim.seq} is not the next durable sequence "
                    f"{self._seq}",
                )
            try:
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
            except sqlite3.IntegrityError as exc:
                raise LipasError(
                    f"sqlite fold failed for claim_id={claim.claim_id!r} "
                    f"seq={claim.seq}: {exc}",
                ) from exc
            admitted = claim
        assert admitted is not None
        self._admit_to_projection(admitted)
        if admitted.seq - self._snapshot_seq >= self._snapshot_interval:
            self._try_snapshot()
        return deepcopy(self._merged)

    def _claim_by_id(self, claim_id: str) -> Claim | None:
        existing = self._by_id.get(claim_id)
        if existing is not None:
            return existing
        row = self._conn.execute(
            "SELECT seq,claim_id,tag,kind,priority,source,fields_json,"
            "schema_version FROM claims WHERE claim_id=?",
            (claim_id,),
        ).fetchone()
        return None if row is None else self._decode_claim(row)

    def _try_snapshot(self) -> None:
        try:
            self.checkpoint_projection()
        except Exception:
            # A snapshot is a disposable acceleration structure. A failure
            # must not turn an already committed Claim append into an apparent
            # failure that a caller might retry. Explicit checkpoint calls do
            # still surface errors to operators and tests.
            return

    # ── reads (identical surface to ClaimStore) ───────────────

    @property
    def merged(self) -> Claim:
        self.refresh()
        return deepcopy(self._merged)

    @property
    def log(self) -> tuple[Claim, ...]:
        claims: list[Claim] = []
        cursor = -1
        while True:
            page = self.read_page(after_seq=cursor, limit=1_000)
            claims.extend(page.claims)
            if not page.has_more:
                return tuple(claims)
            cursor = page.next_cursor

    @property
    def registry(self) -> StrategyRegistry:
        return self._registry

    @property
    def ctx(self) -> BeliefContext:
        return self._ctx

    @property
    def seq(self) -> int:
        self.refresh()
        return self._seq

    @property
    def path(self) -> str:
        return str(self._path)

    @property
    def store_id(self) -> str:
        row = self._conn.execute(
            "SELECT value FROM meta WHERE key='store_id'",
        ).fetchone()
        if row is None or not isinstance(row[0], str) or not row[0]:
            raise LipasError("SqliteClaimStore has no valid store_id")
        return row[0]

    @property
    def closed(self) -> bool:
        return self._closed

    def __len__(self) -> int:
        self._ensure_open()
        row = self._conn.execute("SELECT COUNT(*) FROM claims").fetchone()
        return 0 if row is None else int(row[0])

    def __iter__(self) -> Iterator[Claim]:
        return iter(self.log)

    def filter(
        self, *,
        tag:    str | None = None,
        kind:   str | None = None,
        source: str | None = None,
    ) -> list[Claim]:
        self._ensure_open()
        clauses: list[str] = []
        values: list[str] = []
        for column, value in (("tag", tag), ("kind", kind), ("source", source)):
            if value is not None:
                clauses.append(f"{column}=?")
                values.append(value)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        rows = self._conn.execute(
            "SELECT seq,claim_id,tag,kind,priority,source,fields_json,"
            f"schema_version FROM claims{where} ORDER BY seq",
            values,
        )
        return [self._decode_claim(row) for row in rows]

    def read_page(
        self,
        *,
        after_seq: int = -1,
        limit: int = 100,
        tag: str | None = None,
    ) -> ClaimPage:
        """Read a bounded page without retaining the complete tape in memory."""
        self._ensure_open()
        if isinstance(after_seq, bool) or not isinstance(after_seq, int):
            raise TypeError("after_seq must be an int")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("limit must be a positive int")
        sql = (
            "SELECT seq,claim_id,tag,kind,priority,source,fields_json,"
            "schema_version FROM claims WHERE seq>?"
        )
        values: list[object] = [after_seq]
        if tag is not None:
            sql += " AND tag=?"
            values.append(tag)
        sql += " ORDER BY seq LIMIT ?"
        values.append(limit + 1)
        rows = self._conn.execute(sql, values).fetchall()
        has_more = len(rows) > limit
        claims = tuple(self._decode_claim(row) for row in rows[:limit])
        next_cursor = claims[-1].seq if claims else after_seq
        return ClaimPage(claims, next_cursor, has_more)

    def contains_claim_id(self, claim_id: str) -> bool:
        """Check durable identity through the unique index."""
        self._ensure_open()
        return self._conn.execute(
            "SELECT 1 FROM claims WHERE claim_id=?", (claim_id,),
        ).fetchone() is not None

    def refresh(self) -> int:
        """Project Claims appended by another connection since last refresh."""
        self._ensure_open()
        return self._refresh_existing()

    @property
    def snapshot_seq(self) -> int:
        return self._snapshot_seq

    @property
    def resident_claim_count(self) -> int:
        """Claims replayed after the latest snapshot and retained in memory."""
        return len(self._log)

    def checkpoint_projection(self) -> int:
        """Persist the current deterministic projection as rebuildable cache."""
        self._ensure_open()
        self.refresh()
        current_key = _projection_fingerprint(self._registry, self._ctx)
        if current_key != self._projection_key:
            raise LipasError(
                "projection registry/context changed after open; refusing an "
                "unsafe snapshot",
            )
        snapshot_seq = self._seq - 1
        if snapshot_seq < 0 or snapshot_seq <= self._snapshot_seq:
            return self._snapshot_seq
        encoded = encode(self._merged.fields, self._codecs)
        fields_json = json.dumps(
            encoded, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
            allow_nan=False,
        )
        with immediate_transaction(self._conn):
            checksum = _snapshot_digest(
                snapshot_seq,
                self._merged.claim_id,
                self._merged.tag,
                self._merged.kind,
                int(self._merged.priority),
                self._merged.source or "",
                fields_json,
            )
            self._conn.execute(
                "INSERT INTO claim_projection_snapshots"
                "(projection_key,snapshot_seq,snapshot_version,claim_id,tag,kind,"
                "priority,source,fields_json,payload_sha256,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(projection_key,snapshot_seq) DO UPDATE SET "
                "snapshot_version=excluded.snapshot_version,"
                "claim_id=excluded.claim_id,tag=excluded.tag,kind=excluded.kind,"
                "priority=excluded.priority,source=excluded.source,"
                "fields_json=excluded.fields_json,"
                "payload_sha256=excluded.payload_sha256,"
                "created_at=excluded.created_at",
                (
                    self._projection_key, snapshot_seq,
                    PROJECTION_SNAPSHOT_VERSION, self._merged.claim_id,
                    self._merged.tag, self._merged.kind,
                    int(self._merged.priority), self._merged.source or "",
                    fields_json, checksum, time.time(),
                ),
            )
            self._conn.execute(
                "DELETE FROM claim_projection_snapshots WHERE projection_key=? "
                "AND snapshot_seq NOT IN (SELECT snapshot_seq FROM "
                "claim_projection_snapshots WHERE projection_key=? "
                "ORDER BY snapshot_seq DESC LIMIT 3)",
                (self._projection_key, self._projection_key),
            )
        self._snapshot_seq = snapshot_seq
        # A snapshot replaces the resident replay prefix. Indexed reads remain
        # available directly from SQLite, while memory stays proportional to
        # Claims appended after this checkpoint.
        self._log.clear()
        self._by_tag.clear()
        self._by_id.clear()
        return snapshot_seq

    def _ensure_open(self) -> None:
        if self._closed:
            raise LipasError("SqliteClaimStore is closed")

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
        size = "?" if self._closed else str(len(self))
        return (
            f"SqliteClaimStore(path={self._path!r}, "
            f"size={size}, "
            f"snapshot_seq={self._snapshot_seq}, "
            f"state={state})"
        )


def _projection_fingerprint(
    registry: StrategyRegistry,
    ctx: BeliefContext,
) -> str:
    """Identify code/configuration that determines the merged projection."""
    table = getattr(registry, "_table", {})
    default = getattr(registry, "_default", None)

    def identity(value: object) -> tuple[str, str, str]:
        code = getattr(value, "__code__", None)
        state = [repr(getattr(value, "__defaults__", None))]
        if code is not None:
            state.extend((code.co_code.hex(), repr(code.co_consts)))
        closure = getattr(value, "__closure__", None)
        if closure:
            for cell in closure:
                try:
                    contents = cell.cell_contents
                except ValueError:
                    state.append("<empty closure cell>")
                else:
                    state.append(repr(contents))
        code_digest = hashlib.sha256(
            "\0".join(state).encode("utf-8"),
        ).hexdigest()
        return (
            str(getattr(value, "__module__", "")),
            str(getattr(value, "__qualname__", repr(value))),
            code_digest,
        )

    payload = {
        "snapshot_version": PROJECTION_SNAPSHOT_VERSION,
        "default": identity(default),
        "strategies": [
            (name, identity(strategy))
            for name, strategy in sorted(table.items())
        ],
        "context": {
            "fail_counts": sorted(
                (repr(key), repr(value))
                for key, value in ctx.fail_counts.items()
            ),
            "caution_threshold": ctx.caution_threshold,
        },
    }
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), allow_nan=False,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _snapshot_digest(
    snapshot_seq: object,
    claim_id: object,
    tag: object,
    kind: object,
    priority: object,
    source: object,
    fields_json: object,
) -> str:
    """Hash every persisted projection field to detect torn/corrupt cache."""
    payload = json.dumps(
        [snapshot_seq, claim_id, tag, kind, priority, source, fields_json],
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
