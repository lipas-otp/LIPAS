"""Shared SQLite policy for LIPAS durable stores.

SQLite is the default local storage engine, not an incidental implementation
detail. Every long-lived store must therefore agree on locking, durability,
and failure behaviour. This module deliberately stays small: it centralises
connection policy without creating another authority or generic database ORM.
"""
from __future__ import annotations

import contextlib
import sqlite3
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Iterator


DEFAULT_BUSY_TIMEOUT_MS = 5_000
DEFAULT_WAL_AUTOCHECKPOINT_PAGES = 1_000
DEFAULT_AUDIT_REPAIR_BATCH_SIZE = 256


class SQLiteFailureKind(str, Enum):
    """Operational failure classes callers can expose deliberately."""

    BUSY = "busy"
    CONSTRAINT = "constraint"
    READ_ONLY = "read_only"
    DISK_FULL = "disk_full"
    CORRUPTION = "corruption"
    OTHER = "other"


@dataclass(frozen=True, slots=True)
class SQLitePolicy:
    """Connection policy shared by normal LIPAS stores."""

    busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS
    wal_autocheckpoint_pages: int = DEFAULT_WAL_AUTOCHECKPOINT_PAGES
    # Durable state defaults to surviving an OS crash or power loss. A caller
    # may explicitly choose NORMAL when throughput matters more than the most
    # recent committed transaction.
    synchronous: str = "FULL"

    def __post_init__(self) -> None:
        if self.busy_timeout_ms < 0:
            raise ValueError("busy_timeout_ms must be non-negative")
        if self.wal_autocheckpoint_pages < 1:
            raise ValueError("wal_autocheckpoint_pages must be positive")
        if self.synchronous not in {"OFF", "NORMAL", "FULL", "EXTRA"}:
            raise ValueError("unsupported SQLite synchronous policy")


DEFAULT_SQLITE_POLICY = SQLitePolicy()


def ensure_sqlite_parent(path: str | Path) -> None:
    """Create the parent of a normal SQLite path when it is missing."""
    if isinstance(path, str) and (
        path == ":memory:" or path.startswith("file:")
    ):
        return
    Path(path).expanduser().parent.mkdir(parents=True, exist_ok=True)


def connect_sqlite(
    path: str | Path = ":memory:",
    *,
    policy: SQLitePolicy = DEFAULT_SQLITE_POLICY,
    check_same_thread: bool = True,
    row_factory: type[sqlite3.Row] | None = None,
) -> sqlite3.Connection:
    """Open and configure one normal LIPAS SQLite connection.

    WAL lets readers continue while SQLite's single writer commits. It does
    not pretend SQLite has multiple simultaneous writers: LIPAS keeps write
    transactions short and uses the busy timeout as bounded backpressure.
    """
    ensure_sqlite_parent(path)
    is_uri = isinstance(path, str) and path.startswith("file:")
    connection = sqlite3.connect(
        str(path),
        timeout=policy.busy_timeout_ms / 1_000,
        check_same_thread=check_same_thread,
        uri=is_uri,
    )
    try:
        if row_factory is not None:
            connection.row_factory = row_factory
        # LIPAS owns its schemas; schema objects never need permission to call
        # application functions or virtual tables with connection privileges.
        connection.execute("PRAGMA trusted_schema = OFF")
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(f"PRAGMA busy_timeout = {policy.busy_timeout_ms}")
        read_only_uri = is_uri and "mode=ro" in str(path)
        if read_only_uri:
            connection.execute("PRAGMA query_only = ON")
        if path != ":memory:" and not read_only_uri:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute(f"PRAGMA synchronous = {policy.synchronous}")
            connection.execute(
                "PRAGMA wal_autocheckpoint = "
                f"{policy.wal_autocheckpoint_pages}",
            )
        return connection
    except BaseException:
        connection.close()
        raise


def classify_sqlite_failure(error: sqlite3.Error) -> SQLiteFailureKind:
    """Classify a SQLite error without hiding its original exception."""
    code = getattr(error, "sqlite_errorcode", None)
    primary = code & 0xFF if isinstance(code, int) else None
    if primary in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}:
        return SQLiteFailureKind.BUSY
    if primary == sqlite3.SQLITE_CONSTRAINT:
        return SQLiteFailureKind.CONSTRAINT
    if primary == sqlite3.SQLITE_READONLY:
        return SQLiteFailureKind.READ_ONLY
    if primary == sqlite3.SQLITE_FULL:
        return SQLiteFailureKind.DISK_FULL
    if primary in {sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB}:
        return SQLiteFailureKind.CORRUPTION
    message = str(error).lower()
    if "locked" in message or "busy" in message:
        return SQLiteFailureKind.BUSY
    if "readonly" in message or "read-only" in message:
        return SQLiteFailureKind.READ_ONLY
    if "full" in message:
        return SQLiteFailureKind.DISK_FULL
    if "malformed" in message or "not a database" in message:
        return SQLiteFailureKind.CORRUPTION
    return SQLiteFailureKind.OTHER


@contextlib.contextmanager
def immediate_transaction(
    connection: sqlite3.Connection,
    *,
    begin_attempts: int = 1,
    retry_delay_s: float = 0.01,
) -> Iterator[None]:
    """Run a short write transaction and acquire writer ownership up front.

    Only ``BEGIN IMMEDIATE`` is retried. The body is never replayed, which
    prevents a database helper from duplicating an external side effect hidden
    in caller code.
    """
    if begin_attempts < 1:
        raise ValueError("begin_attempts must be positive")
    if retry_delay_s < 0:
        raise ValueError("retry_delay_s must be non-negative")
    for attempt in range(begin_attempts):
        try:
            connection.execute("BEGIN IMMEDIATE")
            break
        except sqlite3.OperationalError as exc:
            if (
                classify_sqlite_failure(exc) is not SQLiteFailureKind.BUSY
                or attempt + 1 == begin_attempts
            ):
                raise
            time.sleep(retry_delay_s * (2**attempt))
    try:
        yield
    except BaseException:
        try:
            connection.rollback()
        except BaseException:
            # Preserve the body failure; the connection remains suspect and
            # its owner can close it during normal error cleanup.
            pass
        raise
    else:
        try:
            connection.commit()
        except BaseException:
            try:
                connection.rollback()
            except BaseException:
                pass
            raise


def wal_checkpoint(
    connection: sqlite3.Connection,
    *,
    truncate: bool = False,
) -> tuple[int, int, int]:
    """Checkpoint WAL pages and return ``busy, log, checkpointed`` counts."""
    mode = "TRUNCATE" if truncate else "PASSIVE"
    row = connection.execute(f"PRAGMA wal_checkpoint({mode})").fetchone()
    if row is None:
        return (0, 0, 0)
    return int(row[0]), int(row[1]), int(row[2])
