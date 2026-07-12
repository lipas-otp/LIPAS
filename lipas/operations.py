"""Durable, recovery-oriented protocol for provider-idempotent operations.

This is intentionally *not* called exactly-once.  A key is durably prepared
before a provider is contacted.  If the process dies or a call raises, the
operation becomes ``uncertain`` and cannot be resent until reconciliation.
"""
from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Protocol, TypeVar

from .calculus import Claim
from .rows import RowSet

TAG_OPERATION_PREPARED = "operation_prepared"
TAG_OPERATION_UNCERTAIN = "operation_uncertain"
TAG_OPERATION_SUCCEEDED = "operation_succeeded"
TAG_OPERATION_FAILED = "operation_failed"

__all__ = [
    "Operation", "OperationJournal", "IdempotentProvider", "PendingOperation",
    "TAG_OPERATION_PREPARED", "TAG_OPERATION_UNCERTAIN",
    "TAG_OPERATION_SUCCEEDED", "TAG_OPERATION_FAILED",
]
T = TypeVar("T")


class IdempotentProvider(Protocol):
    def __call__(self, *, idempotency_key: str) -> T: ...


class PendingOperation(RuntimeError):
    """A provider may have accepted an earlier attempt; reconcile it first."""


@dataclass(frozen=True, slots=True)
class Operation:
    key: str
    kind: str
    request: Mapping[str, Any]
    state: str  # pending | succeeded | failed | uncertain
    result: Any | None = None
    provider_reference: str | None = None
    error: Mapping[str, Any] | None = None
    effect_id: str | None = None


class OperationJournal:
    """SQLite journal with atomic prepare/settle transitions.

    ``execute`` forwards the caller-supplied stable key to a provider.  It
    refuses to retry a pre-existing pending or uncertain row, because that is
    precisely the crash window in which an external effect is unknowable.
    """
    def __init__(self, path: str = ":memory:", *, rowset: RowSet | None = None) -> None:
        self._conn = sqlite3.connect(path)
        self._conn.execute("""CREATE TABLE IF NOT EXISTS operations (
            key TEXT PRIMARY KEY, kind TEXT NOT NULL, request_json TEXT NOT NULL,
            state TEXT NOT NULL CHECK(state IN ('pending','succeeded','failed','uncertain')),
            result_json TEXT, provider_reference TEXT, error_json TEXT,
            effect_id TEXT,
            created_at REAL NOT NULL, updated_at REAL NOT NULL)""")
        # Existing journals predate effect linkage. SQLite's ALTER is safe and
        # makes the audit feature an additive on-disk migration.
        columns = {row[1] for row in self._conn.execute("PRAGMA table_info(operations)")}
        if "effect_id" not in columns:
            self._conn.execute("ALTER TABLE operations ADD COLUMN effect_id TEXT")
        self._conn.commit(); self.rowset = rowset

    def close(self) -> None: self._conn.close()
    def __enter__(self) -> "OperationJournal": return self
    def __exit__(self, *_: Any) -> None: self.close()

    def get(self, key: str) -> Operation | None:
        row = self._conn.execute("SELECT key,kind,request_json,state,result_json,provider_reference,error_json,effect_id FROM operations WHERE key=?", (key,)).fetchone()
        if row is None: return None
        return Operation(row[0], row[1], json.loads(row[2]), row[3], json.loads(row[4]) if row[4] else None, row[5], json.loads(row[6]) if row[6] else None, row[7])

    def prepare(self, *, key: str, kind: str, request: Mapping[str, Any], effect_id: str | None = None) -> Operation:
        if not key or not kind: raise ValueError("idempotency key and operation kind must be non-empty")
        payload = json.dumps(dict(request), sort_keys=True, separators=(",", ":"))
        now = time.time()
        try:
            with self._conn:
                self._conn.execute("INSERT INTO operations(key,kind,request_json,state,effect_id,created_at,updated_at) VALUES(?,?,?,'pending',?,?,?)", (key, kind, payload, effect_id, now, now))
        except sqlite3.IntegrityError:
            existing = self.get(key)
            assert existing is not None
            if existing.kind != kind or dict(existing.request) != dict(request) or (effect_id is not None and existing.effect_id != effect_id):
                raise ValueError("idempotency key was reused for a different operation")
            return existing
        operation = self.get(key)
        assert operation is not None
        self._audit(TAG_OPERATION_PREPARED, operation)
        return operation

    def _transition(self, key: str, state: str, *, result: Any = None, provider_reference: str | None = None, error: Mapping[str, Any] | None = None) -> Operation:
        if self.get(key) is None: raise KeyError(key)
        with self._conn:
            self._conn.execute("UPDATE operations SET state=?,result_json=?,provider_reference=?,error_json=?,updated_at=? WHERE key=?", (state, json.dumps(result, sort_keys=True) if result is not None else None, provider_reference, json.dumps(dict(error), sort_keys=True) if error else None, time.time(), key))
        operation = self.get(key)
        assert operation is not None
        self._audit({"succeeded": TAG_OPERATION_SUCCEEDED, "failed": TAG_OPERATION_FAILED,
                     "uncertain": TAG_OPERATION_UNCERTAIN}.get(state, TAG_OPERATION_PREPARED), operation)
        return operation

    def settle(self, key: str, *, result: Any, provider_reference: str | None = None) -> Operation:
        return self._transition(key, "succeeded", result=result, provider_reference=provider_reference)

    def fail(self, key: str, *, error: Mapping[str, Any]) -> Operation:
        """Mark a reconciled, known-not-applied operation as failed."""
        return self._transition(key, "failed", error=error)

    def mark_uncertain(self, key: str, *, error: Mapping[str, Any] | None = None) -> Operation:
        return self._transition(key, "uncertain", error=error)

    def execute(self, *, key: str, kind: str, request: Mapping[str, Any], provider: IdempotentProvider[T], provider_reference: Callable[[T], str | None] | None = None, effect_id: str | None = None) -> Operation:
        existed = self.get(key) is not None
        op = self.prepare(key=key, kind=kind, request=request, effect_id=effect_id)
        if op.state == "succeeded": return op
        if existed and op.state == "failed":
            raise PendingOperation(f"operation {key!r} is failed after reconciliation; use a new key for an intentional retry")
        if existed and op.state in {"pending", "uncertain"}:
            raise PendingOperation(f"operation {key!r} is {op.state}; reconcile provider state before retrying")
        try:
            result = provider(idempotency_key=key)
        except BaseException as exc:
            self.mark_uncertain(key, error={"type": type(exc).__name__, "message": str(exc)})
            raise
        return self.settle(key, result=result, provider_reference=provider_reference(result) if provider_reference else None)

    def pending(self) -> tuple[Operation, ...]:
        return tuple(op for (key,) in self._conn.execute("SELECT key FROM operations WHERE state IN ('pending','uncertain') ORDER BY created_at") if (op := self.get(key)) is not None)

    def reconcile(self, key: str, lookup: Callable[[str], tuple[bool, Any, str | None]]) -> Operation:
        """Use provider lookup ``(found, result, reference)`` to settle a row.

        If the provider proves no operation exists, the row becomes ``failed``;
        only application code may choose a new idempotency key and resubmit.
        """
        op = self.get(key)
        if op is None: raise KeyError(key)
        found, result, reference = lookup(key)
        if found: return self.settle(key, result=result, provider_reference=reference)
        return self.fail(key, error={"type": "provider_not_found", "message": "reconciliation found no provider operation"})

    def _audit(self, tag: str, operation: Operation) -> None:
        if self.rowset is None:
            return
        self.rowset.fold(Claim(
            tag=tag,
            fields={
                "operation_key": operation.key,
                "operation_kind": operation.kind,
                "state": operation.state,
                "effect_id": operation.effect_id,
                "provider_reference": operation.provider_reference,
                "error": dict(operation.error) if operation.error else None,
            },
            source="operations.journal",
        ))
