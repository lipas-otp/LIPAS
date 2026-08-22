"""Durable, recovery-oriented protocol for provider-idempotent operations.

This is intentionally *not* called exactly-once.  A key is durably prepared
before a provider is contacted.  If the process dies or a call raises, the
operation becomes ``uncertain`` and cannot be resent until reconciliation.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, TypeVar

from .calculus import Claim
from .rows import RowSet
from .serialization.store_sqlite import ensure_sqlite_parent

TAG_OPERATION_PREPARED = "operation_prepared"
TAG_OPERATION_UNCERTAIN = "operation_uncertain"
TAG_OPERATION_SUCCEEDED = "operation_succeeded"
TAG_OPERATION_FAILED = "operation_failed"
OPERATION_SCHEMA_VERSION = 1

__all__ = [
    "Operation", "OperationJournal", "IdempotentProvider", "PendingOperation",
    "OperationStateError",
    "OperationSchemaVersionMismatch", "OPERATION_SCHEMA_VERSION",
    "TAG_OPERATION_PREPARED", "TAG_OPERATION_UNCERTAIN",
    "TAG_OPERATION_SUCCEEDED", "TAG_OPERATION_FAILED",
]
T_co = TypeVar("T_co", covariant=True)
T = TypeVar("T")


class IdempotentProvider(Protocol[T_co]):
    def __call__(self, *, idempotency_key: str) -> T_co: ...


class PendingOperation(RuntimeError):
    """A provider may have accepted an earlier attempt; reconcile it first."""


class OperationStateError(RuntimeError):
    """A terminal operation cannot be rewritten to a different outcome."""


class OperationSchemaVersionMismatch(OperationStateError):
    """An operation journal uses an incompatible durable schema."""


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
    def __init__(self, path: str | Path = ":memory:", *, rowset: RowSet | None = None) -> None:
        ensure_sqlite_parent(path)
        self._path = path
        self._conn = sqlite3.connect(path)
        self.rowset = rowset
        self._closed = False
        self._audit_cursor = 0
        try:
            self._init_schema()
            with self._conn:
                self._seed_legacy_audit_events()
            self.repair_audit()
        except BaseException:
            self._conn.close()
            self._closed = True
            raise

    def _init_schema(self) -> None:
        had_schema = self._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='operations'",
        ).fetchone() is not None
        with self._conn:
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS operation_meta "
                "(key TEXT PRIMARY KEY,value TEXT NOT NULL)",
            )
        row = self._conn.execute(
            "SELECT value FROM operation_meta WHERE key='schema_version'",
        ).fetchone()
        if row is None:
            with self._conn:
                self._conn.executemany(
                    "INSERT INTO operation_meta(key,value) VALUES(?,?)",
                    (
                        ("schema_version", str(OPERATION_SCHEMA_VERSION)),
                        ("created_at", repr(time.time())),
                        ("adopted_legacy_schema", "1" if had_schema else "0"),
                    ),
                )
        else:
            try:
                existing = int(row[0])
            except (TypeError, ValueError) as exc:
                raise OperationSchemaVersionMismatch(
                    f"operation schema version is not an int: {row[0]!r}",
                ) from exc
            if existing != OPERATION_SCHEMA_VERSION:
                raise OperationSchemaVersionMismatch(
                    f"operation journal at {self._path!r} is schema version "
                    f"{existing}; this LIPAS release supports "
                    f"{OPERATION_SCHEMA_VERSION}. No automatic migration is available.",
                )
        with self._conn:
            self._conn.execute("""CREATE TABLE IF NOT EXISTS operations (
                key TEXT PRIMARY KEY, kind TEXT NOT NULL, request_json TEXT NOT NULL,
                state TEXT NOT NULL CHECK(state IN ('pending','succeeded','failed','uncertain')),
                result_json TEXT, provider_reference TEXT, error_json TEXT,
                effect_id TEXT,
                created_at REAL NOT NULL, updated_at REAL NOT NULL)""")
            self._conn.execute("""CREATE TABLE IF NOT EXISTS operation_audit_events (
                claim_id TEXT PRIMARY KEY, tag TEXT NOT NULL,
                fields_json TEXT NOT NULL, created_at REAL NOT NULL)""")
            # Pre-0.10 development journals predate Effect linkage.
            columns = {
                column[1]
                for column in self._conn.execute("PRAGMA table_info(operations)")
            }
            if "effect_id" not in columns:
                self._conn.execute("ALTER TABLE operations ADD COLUMN effect_id TEXT")

    @property
    def schema_version(self) -> int:
        return OPERATION_SCHEMA_VERSION

    def close(self) -> None:
        """Close the journal connection. Safe to call more than once."""
        if self._closed:
            return
        self._conn.close()
        self._closed = True
    def __enter__(self) -> "OperationJournal": return self
    def __exit__(self, *_: Any) -> None: self.close()

    def get(self, key: str) -> Operation | None:
        row = self._conn.execute("SELECT key,kind,request_json,state,result_json,provider_reference,error_json,effect_id FROM operations WHERE key=?", (key,)).fetchone()
        if row is None:
            return None
        return Operation(row[0], row[1], json.loads(row[2]), row[3], json.loads(row[4]) if row[4] else None, row[5], json.loads(row[6]) if row[6] else None, row[7])

    def prepare(self, *, key: str, kind: str, request: Mapping[str, Any], effect_id: str | None = None) -> Operation:
        if not isinstance(key, str) or not key:
            raise ValueError("idempotency key must be a non-empty string")
        if not isinstance(kind, str) or not kind:
            raise ValueError("operation kind must be a non-empty string")
        if not isinstance(request, Mapping):
            raise TypeError("operation request must be a mapping")
        if effect_id is not None and (not isinstance(effect_id, str) or not effect_id):
            raise ValueError("effect_id must be a non-empty string or None")
        payload = json.dumps(dict(request), sort_keys=True, separators=(",", ":"))
        now = time.time()
        try:
            with self._conn:
                self._conn.execute("INSERT INTO operations(key,kind,request_json,state,effect_id,created_at,updated_at) VALUES(?,?,?,'pending',?,?,?)", (key, kind, payload, effect_id, now, now))
                self._record_audit_event(
                    TAG_OPERATION_PREPARED,
                    Operation(key, kind, dict(request), "pending", effect_id=effect_id),
                )
        except sqlite3.IntegrityError:
            existing = self.get(key)
            assert existing is not None
            if existing.kind != kind or dict(existing.request) != dict(request) or (effect_id is not None and existing.effect_id != effect_id):
                raise ValueError(
                    "idempotency key was reused for a different operation",
                ) from None
            self.repair_audit()
            return existing
        settled_operation = self.get(key)
        assert settled_operation is not None
        self.repair_audit()
        return settled_operation

    def _transition(self, key: str, state: str, *, result: Any = None, provider_reference: str | None = None, error: Mapping[str, Any] | None = None) -> Operation:
        """Move a non-terminal operation once, without rewriting history.

        A journal entry is a recovery record, not a mutable status row.  In
        particular, a later or stale reconciliation must never turn a known
        success into a failure.  The conditional SQL update also prevents two
        processes from both treating the same pending row as theirs to settle.
        """
        current = self.get(key)
        if current is None:
            raise KeyError(key)

        new_error = dict(error) if error else None
        same_outcome = (
            current.state == state
            and current.result == result
            and current.provider_reference == provider_reference
            and (dict(current.error) if current.error else None) == new_error
        )
        if same_outcome:
            self.repair_audit()
            return current
        if current.state in {"succeeded", "failed"}:
            raise OperationStateError(
                f"operation {key!r} is terminal ({current.state}) and cannot be rewritten"
            )

        allowed_from = ("pending",) if state == "uncertain" else ("pending", "uncertain")
        if current.state not in allowed_from:
            raise OperationStateError(
                f"operation {key!r} cannot transition from {current.state!r} to {state!r}"
            )
        placeholders = ",".join("?" for _ in allowed_from)
        with self._conn:
            cursor = self._conn.execute(
                "UPDATE operations SET state=?,result_json=?,provider_reference=?,"
                f"error_json=?,updated_at=? WHERE key=? AND state IN ({placeholders})",
                (
                    state,
                    json.dumps(result, sort_keys=True) if result is not None else None,
                    provider_reference,
                    json.dumps(new_error, sort_keys=True) if new_error else None,
                    time.time(),
                    key,
                    *allowed_from,
                ),
            )
            if cursor.rowcount == 1:
                transitioned_operation = Operation(
                    current.key,
                    current.kind,
                    current.request,
                    state,
                    result,
                    provider_reference,
                    new_error,
                    current.effect_id,
                )
                self._record_audit_event(
                    {
                        "succeeded": TAG_OPERATION_SUCCEEDED,
                        "failed": TAG_OPERATION_FAILED,
                        "uncertain": TAG_OPERATION_UNCERTAIN,
                    }.get(state, TAG_OPERATION_PREPARED),
                    transitioned_operation,
                )
        if cursor.rowcount != 1:
            latest = self.get(key)
            assert latest is not None
            latest_matches = (
                latest.state == state
                and latest.result == result
                and latest.provider_reference == provider_reference
                and (dict(latest.error) if latest.error else None) == new_error
            )
            if latest_matches:
                self.repair_audit()
                return latest
            raise OperationStateError(
                f"operation {key!r} changed concurrently; its current state is {latest.state!r}"
            )
        committed_operation = self.get(key)
        assert committed_operation is not None
        self.repair_audit()
        return committed_operation

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
        if op.state == "succeeded":
            return op
        if existed and op.state == "failed":
            raise PendingOperation(f"operation {key!r} is failed after reconciliation; use a new key for an intentional retry")
        if existed and op.state in {"pending", "uncertain"}:
            raise PendingOperation(f"operation {key!r} is {op.state}; reconcile provider state before retrying")
        try:
            result = provider(idempotency_key=key)
            reference = provider_reference(result) if provider_reference else None
            return self.settle(key, result=result, provider_reference=reference)
        except BaseException as exc:
            # The provider may already have accepted the operation even when
            # result parsing, provider-reference extraction, or journal
            # serialization fails. Preserve the original error, but turn an
            # still-pending row into uncertainty so no caller can resend it.
            self._mark_uncertain_after_submission(key, exc)
            raise

    def _mark_uncertain_after_submission(self, key: str, exc: BaseException) -> None:
        current = self.get(key)
        if current is None or current.state != "pending":
            return
        try:
            self.mark_uncertain(
                key,
                error={"type": type(exc).__name__, "message": str(exc)},
            )
        except OperationStateError:
            # A concurrent reconciler/settler won the race. Its durable state
            # is more authoritative than this process's local exception.
            pass
        except BaseException:
            # A mirrored Claim is deliberately a second transaction. If that
            # write failed after the uncertain transition committed, preserve
            # the provider exception; the durable outbox will repair the Claim.
            latest = self.get(key)
            if latest is None or latest.state != "uncertain":
                raise

    def pending(self) -> tuple[Operation, ...]:
        return tuple(op for (key,) in self._conn.execute("SELECT key FROM operations WHERE state IN ('pending','uncertain') ORDER BY created_at") if (op := self.get(key)) is not None)

    def reconcile(self, key: str, lookup: Callable[[str], tuple[bool, Any, str | None]]) -> Operation:
        """Use provider lookup ``(found, result, reference)`` to settle a row.

        If the provider proves no operation exists, the row becomes ``failed``;
        only application code may choose a new idempotency key and resubmit.
        """
        op = self.get(key)
        if op is None:
            raise KeyError(key)
        # Reconciliation is idempotent after a known outcome.  Do not invoke a
        # possibly stale provider lookup or rewrite a terminal journal row.
        if op.state in {"succeeded", "failed"}:
            self.repair_audit()
            return op
        found, result, reference = lookup(key)
        if found:
            return self.settle(key, result=result, provider_reference=reference)
        return self.fail(key, error={"type": "provider_not_found", "message": "reconciliation found no provider operation"})

    @staticmethod
    def _audit_claim_id(key: str, tag: str) -> str:
        identity = f"operation\0{key}\0{tag}".encode("utf-8")
        return f"operation_audit_{hashlib.sha256(identity).hexdigest()}"

    @staticmethod
    def _audit_fields(operation: Operation) -> dict[str, Any]:
        return {
            "operation_key": operation.key,
            "operation_kind": operation.kind,
            "state": operation.state,
            "effect_id": operation.effect_id,
            "provider_reference": operation.provider_reference,
            "error": dict(operation.error) if operation.error else None,
        }

    def _record_audit_event(self, tag: str, operation: Operation) -> None:
        """Record a Claim-shaped event inside the journal transaction."""
        self._conn.execute(
            "INSERT OR IGNORE INTO operation_audit_events"
            "(claim_id,tag,fields_json,created_at) VALUES(?,?,?,?)",
            (
                self._audit_claim_id(operation.key, tag),
                tag,
                json.dumps(
                    self._audit_fields(operation),
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                time.time(),
            ),
        )

    def _seed_legacy_audit_events(self) -> None:
        """Make pre-outbox journals repairable from their current truth.

        A legacy terminal row cannot reveal whether it was once uncertain, so
        migration reconstructs the prepared event and the current state only.
        New transitions retain their complete event sequence in the outbox.
        """
        for (key,) in self._conn.execute(
            "SELECT key FROM operations ORDER BY created_at,key",
        ):
            operation = self.get(key)
            assert operation is not None
            prepared = Operation(
                operation.key,
                operation.kind,
                operation.request,
                "pending",
                effect_id=operation.effect_id,
            )
            self._record_audit_event(TAG_OPERATION_PREPARED, prepared)
            if operation.state != "pending":
                self._record_audit_event(
                    {
                        "succeeded": TAG_OPERATION_SUCCEEDED,
                        "failed": TAG_OPERATION_FAILED,
                        "uncertain": TAG_OPERATION_UNCERTAIN,
                    }[operation.state],
                    operation,
                )

    def repair_audit(self) -> int:
        """Idempotently mirror every durable outbox event into Claims.

        The journal database remains authoritative. A failure here never rolls
        back an already committed operation transition; reopening or calling
        this method again resumes from the stable Claim ids.
        """
        if self.rowset is None:
            return 0
        events = self._conn.execute(
            "SELECT rowid,claim_id,tag,fields_json "
            "FROM operation_audit_events WHERE rowid>? ORDER BY rowid",
            (self._audit_cursor,),
        ).fetchall()
        if not events:
            return 0
        existing = list(self.rowset.store)
        known = {claim.claim_id for claim in existing}
        mirrored_payloads = {
            (
                claim.tag,
                json.dumps(
                    claim.fields, sort_keys=True, separators=(",", ":"),
                ),
            )
            for claim in existing
            if claim.source == "operations.journal"
        }
        repaired = 0
        for rowid, claim_id, tag, fields_json in events:
            claim = Claim(
                tag=tag,
                fields=json.loads(fields_json),
                source="operations.journal",
                claim_id=claim_id,
            )
            signature = (tag, fields_json)
            if claim_id not in known and signature in mirrored_payloads:
                # Journals created before the outbox used random Claim ids.
                # Treat an exact legacy mirror as present instead of duplicating
                # its logical event during migration.
                self._audit_cursor = rowid
                continue
            self.rowset.fold(claim)
            if claim_id not in known:
                repaired += 1
                known.add(claim_id)
                mirrored_payloads.add(signature)
            self._audit_cursor = rowid
        return repaired
