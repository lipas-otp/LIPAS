"""Optimistic durable storage for high-level conversational state."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Protocol, runtime_checkable

from .behaviour import AgentState
from .serialization import decode, encode, make_default_codec_registry
from .sqlite_storage import connect_sqlite, immediate_transaction

__all__ = [
    "SessionConflictError",
    "SessionSnapshot",
    "SessionStore",
    "SQLiteSessionStore",
]

_SCHEMA_VERSION = 1
_CODECS = make_default_codec_registry()
_SCHEMA = """
CREATE TABLE IF NOT EXISTS lipas_conversation_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS lipas_conversations (
    id TEXT PRIMARY KEY,
    version INTEGER NOT NULL,
    state_json TEXT NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_lipas_conversations_updated
ON lipas_conversations(updated_at DESC, id);
"""


class SessionConflictError(RuntimeError):
    """A Session snapshot changed after the caller loaded it."""


@dataclass(frozen=True, slots=True)
class SessionSnapshot:
    session_id: str
    state: AgentState
    version: int
    updated_at: float

    def __post_init__(self) -> None:
        _session_id(self.session_id)
        if not isinstance(self.state, AgentState):
            raise TypeError("SessionSnapshot.state must be AgentState")
        if (
            isinstance(self.version, bool)
            or not isinstance(self.version, int)
            or self.version < 1
        ):
            raise ValueError("SessionSnapshot.version must be a positive int")


@runtime_checkable
class SessionStore(Protocol):
    def load(self, session_id: str) -> SessionSnapshot | None: ...

    def save(
        self,
        session_id: str,
        state: AgentState,
        *,
        expected_version: int,
    ) -> SessionSnapshot: ...


class SQLiteSessionStore:
    """Thread-safe SQLite snapshots with compare-and-swap writes."""

    def __init__(self, path: str | Path = ":memory:") -> None:
        if not isinstance(path, (str, Path)):
            raise TypeError("SQLiteSessionStore path must be a string or Path")
        self.path = path
        self._lock = threading.RLock()
        self._conn = connect_sqlite(
            path, check_same_thread=False, row_factory=sqlite3.Row,
        )
        self._closed = False
        try:
            self._init_schema()
        except BaseException:
            self._conn.close()
            self._closed = True
            raise

    def load(self, session_id: str) -> SessionSnapshot | None:
        session_id = _session_id(session_id)
        with self._lock:
            self._ensure_open()
            row = self._conn.execute(
                "SELECT id,version,state_json,updated_at "
                "FROM lipas_conversations WHERE id=?",
                (session_id,),
            ).fetchone()
        return None if row is None else self._snapshot(row)

    def save(
        self,
        session_id: str,
        state: AgentState,
        *,
        expected_version: int,
    ) -> SessionSnapshot:
        session_id = _session_id(session_id)
        if not isinstance(state, AgentState):
            raise TypeError("SessionStore state must be an AgentState")
        if (
            isinstance(expected_version, bool)
            or not isinstance(expected_version, int)
            or expected_version < 0
        ):
            raise ValueError("expected_version must be a non-negative int")
        state_json = _state_json(state)
        version = expected_version + 1
        updated_at = time.time()
        with self._lock, self._transaction():
            if expected_version == 0:
                cursor = self._conn.execute(
                    "INSERT OR IGNORE INTO lipas_conversations"
                    "(id,version,state_json,updated_at) VALUES(?,?,?,?)",
                    (session_id, version, state_json, updated_at),
                )
            else:
                cursor = self._conn.execute(
                    "UPDATE lipas_conversations SET version=?,state_json=?,updated_at=? "
                    "WHERE id=? AND version=?",
                    (version, state_json, updated_at, session_id, expected_version),
                )
            if cursor.rowcount != 1:
                current = self._conn.execute(
                    "SELECT version FROM lipas_conversations WHERE id=?",
                    (session_id,),
                ).fetchone()
                actual = None if current is None else int(current[0])
                raise SessionConflictError(
                    f"session {session_id!r} is version {actual!r}, "
                    f"expected {expected_version}",
                )
        return SessionSnapshot(session_id, state, version, updated_at)

    def list(self, *, limit: int = 100) -> tuple[SessionSnapshot, ...]:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("SessionStore limit must be a positive int")
        with self._lock:
            self._ensure_open()
            rows = self._conn.execute(
                "SELECT id,version,state_json,updated_at FROM lipas_conversations "
                "ORDER BY updated_at DESC,id LIMIT ?",
                (limit,),
            ).fetchall()
        return tuple(self._snapshot(row) for row in rows)

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._conn.close()
                self._closed = True

    def __enter__(self) -> "SQLiteSessionStore":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    def _init_schema(self) -> None:
        with self._conn:
            self._conn.executescript(_SCHEMA)
            row = self._conn.execute(
                "SELECT value FROM lipas_conversation_meta "
                "WHERE key='schema_version'",
            ).fetchone()
            if row is None:
                self._conn.execute(
                    "INSERT INTO lipas_conversation_meta(key,value) VALUES(?,?)",
                    ("schema_version", str(_SCHEMA_VERSION)),
                )
            elif row[0] != str(_SCHEMA_VERSION):
                raise RuntimeError(
                    "SQLiteSessionStore schema version mismatch: "
                    f"database={row[0]!r}, runtime={_SCHEMA_VERSION}",
                )

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        self._ensure_open()
        with immediate_transaction(self._conn):
            yield

    @staticmethod
    def _snapshot(row: sqlite3.Row) -> SessionSnapshot:
        return SessionSnapshot(
            session_id=str(row[0]),
            state=_state_from_json(str(row[2])),
            version=int(row[1]),
            updated_at=float(row[3]),
        )

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("SQLiteSessionStore is closed")


def _session_id(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("session_id must be a non-empty string")
    return value.strip()


def _state_json(state: AgentState) -> str:
    payload = {
        "messages": list(state.messages),
        "iteration": state.iteration,
        "metadata": dict(state.metadata),
    }
    return json.dumps(
        encode(payload, _CODECS), ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False,
    )


def _state_from_json(value: str) -> AgentState:
    def reject_constant(raw: str) -> None:
        raise ValueError(f"non-JSON numeric constant {raw!r}")

    payload = decode(json.loads(value, parse_constant=reject_constant), _CODECS)
    if not isinstance(payload, Mapping):
        raise TypeError("stored Session state must be a mapping")
    messages = payload.get("messages", ())
    metadata = payload.get("metadata", {})
    if not isinstance(messages, (list, tuple)):
        raise TypeError("stored Session messages must be a list")
    if not isinstance(metadata, Mapping):
        raise TypeError("stored Session metadata must be a mapping")
    return AgentState(
        messages=tuple(messages),
        iteration=payload.get("iteration", 0),
        metadata=dict(metadata),
    )
