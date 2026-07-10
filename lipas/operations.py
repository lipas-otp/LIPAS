"""Durable protocol for externally idempotent operations.

This module deliberately does *not* promise exactly-once delivery.  It makes
the only honest contract explicit: a stable caller key is persisted before a
provider call, forwarded to providers that support idempotency, and any crash
between submission and acknowledgement is recoverable as ``pending``.
"""
from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Protocol, TypeVar

__all__ = ["Operation", "OperationJournal", "IdempotentProvider", "PendingOperation"]

T = TypeVar("T")


class IdempotentProvider(Protocol):
    """A provider invocation which accepts the durable operation key."""
    def __call__(self, *, idempotency_key: str) -> T: ...


class PendingOperation(RuntimeError):
    """An earlier attempt may have reached the provider; reconcile it first."""


@dataclass(frozen=True, slots=True)
class Operation:
    key: str
    kind: str
    request: Mapping[str, Any]
    state: str                       # pending | succeeded | failed | uncertain
    result: Any | None = None
    provider_reference: str | None = None


class OperationJournal:
    """SQLite journal with an atomic ``prepare → settle`` state machine.

    ``execute`` never repeats an uncertain operation.  Call ``reconcile``
    with a provider lookup function after a crash, then either obtain the
    settled operation or explicitly compensate/mark it failed in application
    code.  The key is supplied by the caller, rather than derived from mutable
    arguments, so retries across processes use the same provider key.
    """
    def __init__(self, path: str = ":memory:") -> None:
        self._conn = sqlite3.connect(path)
        self._conn.execute("""CREATE TABLE IF NOT EXISTS operations (
            key TEXT PRIMARY KEY, kind TEXT NOT NULL, request_json TEXT NOT NULL,
            state TEXT NOT NULL, result_json TEXT, provider_reference TEXT,
            created_at REAL NOT NULL, updated_at REAL NOT NULL)""")
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def get(self, key: str) -> Operation | None:
        row = self._conn.execute("SELECT key, kind, request_json, state, result_json, provider_reference FROM operations WHERE key=?", (key,)).fetchone()
        if row is None: return None
        return Operation(row[0], row[1], json.loads(row[2]), row[3], json.loads(row[4]) if row[4] is not None else None, row[5])

    def prepare(self, *, key: str, kind: str, request: Mapping[str, Any]) -> Operation:
        if not key: raise ValueError("idempotency key must be non-empty")
        existing = self.get(key)
        if existing is not None:
            if existing.kind != kind or dict(existing.request) != dict(request):
                raise ValueError("idempotency key was reused for a different operation")
            return existing
        now = time.time()
        self._conn.execute("INSERT INTO operations VALUES (?, ?, ?, 'pending', NULL, NULL, ?, ?)", (key, kind, json.dumps(request, sort_keys=True), now, now))
        self._conn.commit()
        return self.get(key)  # type: ignore[return-value]

    def settle(self, key: str, *, result: Any, provider_reference: str | None = None) -> Operation:
        self._conn.execute("UPDATE operations SET state='succeeded', result_json=?, provider_reference=?, updated_at=? WHERE key=?", (json.dumps(result, sort_keys=True), provider_reference, time.time(), key))
        self._conn.commit()
        return self.get(key)  # type: ignore[return-value]

    def mark_uncertain(self, key: str) -> Operation:
        self._conn.execute("UPDATE operations SET state='uncertain', updated_at=? WHERE key=?", (time.time(), key))
        self._conn.commit()
        return self.get(key)  # type: ignore[return-value]

    def execute(self, *, key: str, kind: str, request: Mapping[str, Any], provider: IdempotentProvider[T], provider_reference: Callable[[T], str | None] | None = None) -> Operation:
        existed = self.get(key) is not None
        op = self.prepare(key=key, kind=kind, request=request)
        if op.state == "succeeded": return op
        if existed:
            raise PendingOperation(f"operation {key!r} is {op.state}; reconcile provider state before retrying")
        try:
            result = provider(idempotency_key=key)
        except BaseException:
            self.mark_uncertain(key)
            raise
        return self.settle(key, result=result, provider_reference=provider_reference(result) if provider_reference else None)

    def pending(self) -> tuple[Operation, ...]:
        return tuple(self.get(row[0]) for row in self._conn.execute("SELECT key FROM operations WHERE state IN ('pending','uncertain')") if self.get(row[0]) is not None)  # type: ignore[misc]

    def reconcile(self, key: str, lookup: Callable[[str], tuple[bool, Any, str | None]]) -> Operation:
        """Apply provider lookup ``(found, result, provider_reference)``."""
        op = self.get(key)
        if op is None: raise KeyError(key)
        found, result, reference = lookup(key)
        return self.settle(key, result=result, provider_reference=reference) if found else self.mark_uncertain(key)
