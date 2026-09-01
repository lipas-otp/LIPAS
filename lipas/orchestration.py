"""Named-agent handoffs with a durable at-least-once leased mailbox."""
from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import time
import uuid
from dataclasses import dataclass
from itertools import chain
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping

from .calculus import Claim
from .rows import RowSet
from .sqlite_storage import (
    DEFAULT_AUDIT_REPAIR_BATCH_SIZE,
    connect_sqlite,
    ensure_sqlite_parent,
    immediate_transaction,
)

TAG_AGENT_HANDOFF = "agent_handoff"
TAG_AGENT_MAIL_CLAIM = "agent_mail_claim"
TAG_AGENT_MAIL_ACK = "agent_mail_ack"
TAG_AGENT_MAIL_RELEASED = "agent_mail_released"
TAG_AGENT_MAIL_RECOVERED = "agent_mail_recovered"
MAILBOX_SCHEMA_VERSION = 1

__all__ = [
    "MailboxMessage", "Mailbox", "AgentOrchestrator", "MailboxLeaseError",
    "MailboxSchemaVersionMismatch", "MAILBOX_SCHEMA_VERSION",
    "TAG_AGENT_HANDOFF", "TAG_AGENT_MAIL_CLAIM", "TAG_AGENT_MAIL_ACK",
    "TAG_AGENT_MAIL_RELEASED", "TAG_AGENT_MAIL_RECOVERED",
]


class MailboxLeaseError(RuntimeError):
    pass


class MailboxSchemaVersionMismatch(RuntimeError):
    """A mailbox database uses an incompatible durable schema."""


@dataclass(frozen=True, slots=True)
class MailboxMessage:
    id: str
    sender: str
    recipient: str
    payload: Mapping[str, Any]
    status: str = "pending"  # pending | leased | acknowledged
    attempts: int = 0
    lease_token: str | None = None


class Mailbox:
    """SQLite mailbox with explicit claim/ack lease ownership.

    A member crash leaves a lease behind. ``recover_expired`` makes it pending
    again, which gives at-least-once delivery without pretending duplicates are
    impossible.  Handlers must use ``message.id`` as their operation key.
    """
    def __init__(self, path: str | Path = ":memory:", *, rowset: RowSet | None = None) -> None:
        ensure_sqlite_parent(path)
        self._path = path
        self._conn = connect_sqlite(path)
        self.rowset = rowset
        self._closed = False
        self._audit_cursor = 0
        self._mirrored_payloads: set[tuple[str, str]] | None = None
        try:
            self._init_schema()
            with immediate_transaction(self._conn):
                self._seed_legacy_audit_events_once()
            self._repair_audit_batch()
        except BaseException:
            self._conn.close()
            self._closed = True
            raise

    def _init_schema(self) -> None:
        had_schema = self._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='mailbox'",
        ).fetchone() is not None
        with immediate_transaction(self._conn):
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS mailbox_meta "
                "(key TEXT PRIMARY KEY,value TEXT NOT NULL)",
            )
        row = self._conn.execute(
            "SELECT value FROM mailbox_meta WHERE key='schema_version'",
        ).fetchone()
        if row is None:
            # A second process may be opening this file at the same time.  The
            # metadata stamp is idempotent bootstrap state, not an ownership
            # claim; INSERT OR IGNORE lets both processes converge on the
            # same schema instead of surfacing a spurious UNIQUE violation.
            with immediate_transaction(self._conn):
                self._conn.executemany(
                    "INSERT OR IGNORE INTO mailbox_meta(key,value) VALUES(?,?)",
                    (
                        ("schema_version", str(MAILBOX_SCHEMA_VERSION)),
                        ("created_at", repr(time.time())),
                        ("adopted_legacy_schema", "1" if had_schema else "0"),
                    ),
                )
            row = self._conn.execute(
                "SELECT value FROM mailbox_meta WHERE key='schema_version'",
            ).fetchone()
            if row is None:
                raise MailboxSchemaVersionMismatch(
                    "mailbox schema version is missing after bootstrap",
                )
            try:
                existing = int(row[0])
            except (TypeError, ValueError, OverflowError) as exc:
                raise MailboxSchemaVersionMismatch(
                    f"mailbox schema version is not an int: {row[0]!r}",
                ) from exc
            if existing != MAILBOX_SCHEMA_VERSION:
                raise MailboxSchemaVersionMismatch(
                    f"mailbox at {self._path!r} is schema version {existing}; "
                    f"this LIPAS release supports {MAILBOX_SCHEMA_VERSION}. "
                    "No automatic migration is available.",
                )
        else:
            try:
                existing = int(row[0])
            except (TypeError, ValueError) as exc:
                raise MailboxSchemaVersionMismatch(
                    f"mailbox schema version is not an int: {row[0]!r}",
                ) from exc
            if existing != MAILBOX_SCHEMA_VERSION:
                raise MailboxSchemaVersionMismatch(
                    f"mailbox at {self._path!r} is schema version {existing}; "
                    f"this LIPAS release supports {MAILBOX_SCHEMA_VERSION}. "
                    "No automatic migration is available.",
                )
        with immediate_transaction(self._conn):
            self._conn.execute("""CREATE TABLE IF NOT EXISTS mailbox (
                id TEXT PRIMARY KEY, sender TEXT NOT NULL, recipient TEXT NOT NULL,
                payload TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('pending','leased','acknowledged')),
                attempts INTEGER NOT NULL DEFAULT 0, lease_token TEXT, lease_expires REAL,
                created_at REAL NOT NULL, acknowledged_at REAL)""")
            self._conn.execute("""CREATE TABLE IF NOT EXISTS mailbox_audit_events (
                claim_id TEXT PRIMARY KEY, tag TEXT NOT NULL,
                fields_json TEXT NOT NULL, created_at REAL NOT NULL)""")

    @property
    def schema_version(self) -> int:
        return MAILBOX_SCHEMA_VERSION

    def close(self) -> None:
        """Close the mailbox connection. Safe to call more than once."""
        if self._closed:
            return
        self._conn.close()
        self._closed = True

    def __enter__(self) -> "Mailbox":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    def get(self, message_id: str) -> MailboxMessage | None:
        self._ensure_open()
        if not isinstance(message_id, str) or not message_id.strip():
            raise ValueError("message_id must be a non-empty string")
        message_id = message_id.strip()
        row = self._conn.execute("SELECT id,sender,recipient,payload,status,attempts,lease_token FROM mailbox WHERE id=?", (message_id,)).fetchone()
        if row is None:
            return None
        payload = _strict_json_loads(row[3], "mailbox payload")
        if not isinstance(payload, Mapping):
            raise ValueError("mailbox payload must be a JSON object")
        return MailboxMessage(row[0], row[1], row[2], payload, row[4], row[5], row[6])

    def send(self, *, sender: str, recipient: str, payload: Mapping[str, Any], message_id: str | None = None) -> MailboxMessage:
        self._ensure_open()
        if not isinstance(sender, str) or not sender.strip():
            raise ValueError("sender must be a non-empty string")
        if not isinstance(recipient, str) or not recipient.strip():
            raise ValueError("recipient must be a non-empty string")
        sender = sender.strip()
        recipient = recipient.strip()
        if not isinstance(payload, Mapping):
            raise TypeError("mailbox payload must be a mapping")
        if message_id is not None and (
            not isinstance(message_id, str) or not message_id.strip()
        ):
            raise ValueError("message_id must be a non-empty string or None")
        mid = message_id.strip() if isinstance(message_id, str) else f"msg_{uuid.uuid4().hex}"
        normalized_payload = _strict_json_copy(dict(payload), "mailbox payload")
        if not isinstance(normalized_payload, Mapping):
            raise ValueError("mailbox payload must be a JSON object")
        encoded = json.dumps(
            normalized_payload, sort_keys=True, separators=(",", ":"),
            ensure_ascii=False, allow_nan=False,
        )
        try:
            with immediate_transaction(self._conn):
                self._conn.execute("INSERT INTO mailbox(id,sender,recipient,payload,status,created_at) VALUES(?,?,?,?,'pending',?)", (mid, sender, recipient, encoded, time.time()))
                self._record_audit_event(
                    f"handoff:{mid}",
                    TAG_AGENT_HANDOFF,
                    {
                        "message_id": mid,
                        "sender": sender,
                        "recipient": recipient,
                        "payload": normalized_payload,
                    },
                )
        except sqlite3.IntegrityError:
            existing = self.get(mid)
            assert existing is not None
            if (existing.sender, existing.recipient, dict(existing.payload)) != (sender, recipient, dict(normalized_payload)):
                raise ValueError(
                    "message id was reused for a different handoff",
                ) from None
            self._repair_audit_batch()
            return existing
        self._repair_audit_batch()
        return self.get(mid)  # type: ignore[return-value]

    def recover_expired(self, *, now: float | None = None) -> int:
        self._ensure_open()
        if now is None:
            now = time.time()
        elif (
            isinstance(now, bool)
            or not isinstance(now, (int, float))
            or _finite_float(now) is None
        ):
            raise ValueError("now must be a finite number")
        else:
            now = float(now)
        recovered = 0
        with immediate_transaction(self._conn):
            rows = self._conn.execute(
                "SELECT id,recipient,attempts FROM mailbox "
                "WHERE status='leased' AND lease_expires<=? ORDER BY id",
                (now,),
            ).fetchall()
            for mid, recipient, attempt in rows:
                cur = self._conn.execute(
                    "UPDATE mailbox SET status='pending',lease_token=NULL,"
                    "lease_expires=NULL WHERE id=? AND status='leased' "
                    "AND lease_expires<=?",
                    (mid, now),
                )
                if cur.rowcount:
                    recovered += 1
                    self._record_audit_event(
                        f"recovered:{mid}:{attempt}",
                        TAG_AGENT_MAIL_RECOVERED,
                        {
                            "message_id": mid,
                            "recipient": recipient,
                            "attempt": attempt,
                        },
                    )
        self._repair_audit_batch()
        return recovered

    def claim(
        self,
        recipient: str,
        *,
        limit: int = 1,
        lease_seconds: float = 60.0,
        message_id: str | None = None,
    ) -> tuple[MailboxMessage, ...]:
        self._ensure_open()
        if not isinstance(recipient, str) or not recipient.strip():
            raise ValueError("recipient must be a non-empty string")
        recipient = recipient.strip()
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("limit must be a positive int")
        if (
            isinstance(lease_seconds, bool)
            or not isinstance(lease_seconds, (int, float))
            or _finite_float(lease_seconds) is None
            or lease_seconds <= 0
        ):
            raise ValueError("lease_seconds must be a finite positive number")
        lease_seconds = float(lease_seconds)
        if message_id is not None and (
            not isinstance(message_id, str) or not message_id.strip()
        ):
            raise ValueError("message_id must be a non-empty string or None")
        if isinstance(message_id, str):
            message_id = message_id.strip()
        self.recover_expired()
        if message_id is None:
            rows = self._conn.execute(
                "SELECT id FROM mailbox WHERE recipient=? AND status='pending' "
                "ORDER BY created_at,id LIMIT ?",
                (recipient, limit),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT id FROM mailbox WHERE id=? AND recipient=? "
                "AND status='pending'",
                (message_id, recipient),
            ).fetchall()
        claimed = []
        for (mid,) in rows:
            token, expiry = uuid.uuid4().hex, time.time() + lease_seconds
            message: MailboxMessage | None = None
            with immediate_transaction(self._conn):
                cur = self._conn.execute("UPDATE mailbox SET status='leased',attempts=attempts+1,lease_token=?,lease_expires=? WHERE id=? AND status='pending'", (token, expiry, mid))
                if cur.rowcount:
                    message = self.get(mid)
                    assert message is not None
                    self._record_audit_event(
                        f"claim:{mid}:{message.attempts}",
                        TAG_AGENT_MAIL_CLAIM,
                        {
                            "message_id": mid,
                            "recipient": recipient,
                            "attempt": message.attempts,
                        },
                    )
            if message is not None:
                claimed.append(message)
        self._repair_audit_batch()
        return tuple(claimed)

    def acknowledge(self, message_id: str, *, lease_token: str) -> None:
        self._ensure_open()
        if not isinstance(message_id, str) or not message_id.strip():
            raise ValueError("message_id must be a non-empty string")
        if not isinstance(lease_token, str) or not lease_token.strip():
            raise ValueError("lease_token must be a non-empty string")
        message_id = message_id.strip()
        lease_token = lease_token.strip()
        now = time.time()
        with immediate_transaction(self._conn):
            cur = self._conn.execute(
                "UPDATE mailbox SET status='acknowledged',acknowledged_at=?,"
                "lease_token=NULL,lease_expires=NULL WHERE id=? AND status='leased' "
                "AND lease_token=? AND lease_expires>?",
                (now, message_id, lease_token, now),
            )
            if cur.rowcount:
                self._record_audit_event(
                    f"ack:{message_id}",
                    TAG_AGENT_MAIL_ACK,
                    {"message_id": message_id},
                )
        if not cur.rowcount:
            raise MailboxLeaseError("message is not owned by an active lease")
        self._repair_audit_batch()

    def release(self, message_id: str, *, lease_token: str) -> None:
        self._ensure_open()
        if not isinstance(message_id, str) or not message_id.strip():
            raise ValueError("message_id must be a non-empty string")
        if not isinstance(lease_token, str) or not lease_token.strip():
            raise ValueError("lease_token must be a non-empty string")
        message_id = message_id.strip()
        lease_token = lease_token.strip()
        now = time.time()
        with immediate_transaction(self._conn):
            row = self._conn.execute(
                "SELECT attempts FROM mailbox WHERE id=? AND status='leased' "
                "AND lease_token=? AND lease_expires>?",
                (message_id, lease_token, now),
            ).fetchone()
            cur = self._conn.execute(
                "UPDATE mailbox SET status='pending',lease_token=NULL,"
                "lease_expires=NULL WHERE id=? AND status='leased' "
                "AND lease_token=? AND lease_expires>?",
                (message_id, lease_token, now),
            )
            if cur.rowcount:
                assert row is not None
                self._record_audit_event(
                    f"released:{message_id}:{row[0]}",
                    TAG_AGENT_MAIL_RELEASED,
                    {"message_id": message_id, "attempt": row[0]},
                )
        if not cur.rowcount:
            raise MailboxLeaseError("message is not owned by this lease")
        self._repair_audit_batch()

    def _ensure_open(self) -> None:
        if self._closed:
            # Preserve sqlite3's established closed-connection contract used
            # by the lower-level stores and existing callers.
            raise sqlite3.ProgrammingError("Cannot operate on a closed Mailbox")

    @staticmethod
    def _audit_claim_id(identity: str) -> str:
        encoded = f"mailbox\0{identity}".encode("utf-8")
        return f"mailbox_audit_{hashlib.sha256(encoded).hexdigest()}"

    def _record_audit_event(
        self,
        identity: str,
        tag: str,
        fields: Mapping[str, Any],
    ) -> None:
        """Record one Claim-shaped event inside the mailbox transaction."""
        self._conn.execute(
            "INSERT OR IGNORE INTO mailbox_audit_events"
            "(claim_id,tag,fields_json,created_at) VALUES(?,?,?,?)",
            (
                self._audit_claim_id(identity),
                tag,
                json.dumps(
                    _strict_json_copy(dict(fields), "mailbox audit fields"),
                    sort_keys=True, separators=(",", ":"),
                    ensure_ascii=False, allow_nan=False,
                ),
                time.time(),
            ),
        )

    def _seed_legacy_audit_events(self) -> None:
        """Make pre-outbox mailbox rows repairable from current durable data."""
        rows = self._conn.execute(
            "SELECT id,sender,recipient,payload,status,attempts FROM mailbox "
            "ORDER BY created_at,id",
        ).fetchall()
        for mid, sender, recipient, payload_json, status, attempts in rows:
            self._record_audit_event(
                f"handoff:{mid}",
                TAG_AGENT_HANDOFF,
                {
                    "message_id": mid,
                    "sender": sender,
                    "recipient": recipient,
                    "payload": _strict_json_loads(payload_json, "mailbox payload"),
                },
            )
            for attempt in range(1, attempts + 1):
                self._record_audit_event(
                    f"claim:{mid}:{attempt}",
                    TAG_AGENT_MAIL_CLAIM,
                    {
                        "message_id": mid,
                        "recipient": recipient,
                        "attempt": attempt,
                    },
                )
            if status == "acknowledged":
                self._record_audit_event(
                    f"ack:{mid}",
                    TAG_AGENT_MAIL_ACK,
                    {"message_id": mid},
                )

    def _seed_legacy_audit_events_once(self) -> None:
        """Pay legacy reconstruction cost once, not on every mailbox open."""
        row = self._conn.execute(
            "SELECT value FROM mailbox_meta WHERE key='audit_seed_version'",
        ).fetchone()
        if row is not None and row[0] == "1":
            return
        self._seed_legacy_audit_events()
        self._conn.execute(
            "INSERT INTO mailbox_meta(key,value) VALUES('audit_seed_version','1') "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        )

    def _repair_audit_batch(self) -> int:
        """Keep a normal transition independent of historical backlog size."""
        return self.repair_audit(limit=DEFAULT_AUDIT_REPAIR_BATCH_SIZE)

    def repair_audit(self, *, limit: int | None = None) -> int:
        """Idempotently mirror durable mailbox events into the Claim tape."""
        self._ensure_open()
        if self.rowset is None:
            return 0
        if (
            limit is not None
            and (isinstance(limit, bool) or not isinstance(limit, int) or limit < 1)
        ):
            raise ValueError("limit must be a positive int or None")
        sql = (
            "SELECT rowid,claim_id,tag,fields_json "
            "FROM mailbox_audit_events WHERE rowid>? ORDER BY rowid"
        )
        params: tuple[int, ...] = (self._audit_cursor,)
        if limit is not None:
            sql += " LIMIT ?"
            params += (limit,)
        event_cursor = self._conn.execute(sql, params)
        first_event = event_cursor.fetchone()
        if first_event is None:
            return 0
        events = chain((first_event,), event_cursor)
        claim_store = self.rowset.store
        contains = getattr(claim_store, "contains_claim_id", None)
        known = (
            None
            if callable(contains)
            else {claim.claim_id for claim in claim_store}
        )
        if self._mirrored_payloads is None:
            existing = claim_store.filter(source="orchestration.mailbox")
            self._mirrored_payloads = {
                (
                    claim.tag,
                    json.dumps(
                        claim.fields, sort_keys=True, separators=(",", ":"),
                    ),
                )
                for claim in existing
            }
        mirrored_payloads = self._mirrored_payloads
        repaired = 0
        for rowid, claim_id, tag, fields_json in events:
            claim = Claim(
                tag=tag,
                fields=_strict_json_loads(fields_json, "mailbox audit fields"),
                source="orchestration.mailbox",
                claim_id=claim_id,
            )
            signature = (tag, fields_json)
            if callable(contains):
                was_known = bool(contains(claim_id))
            else:
                assert known is not None
                was_known = claim_id in known
            if not was_known and signature in mirrored_payloads:
                self._audit_cursor = rowid
                continue
            self.rowset.fold(claim)
            if not was_known:
                repaired += 1
                if known is not None:
                    known.add(claim_id)
                mirrored_payloads.add(signature)
            self._audit_cursor = rowid
        return repaired


def _strict_json_copy(value: Any, name: str) -> Any:
    """Detach mailbox data and reject coercive/non-finite JSON values."""
    _validate_json_shape(value, name)
    try:
        encoded = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        return json.loads(encoded)
    except (TypeError, ValueError, RecursionError) as exc:
        raise ValueError(f"{name} must be strict JSON") from exc


def _strict_json_loads(value: str, name: str) -> Any:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be strict JSON")
    try:
        parsed = json.loads(
            value,
            parse_constant=lambda raw: (_ for _ in ()).throw(
                ValueError(f"non-JSON numeric constant {raw!r}")
            ),
        )
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError(f"{name} must be strict JSON") from exc
    return _strict_json_copy(parsed, name)


def _validate_json_shape(
    value: Any,
    path: str,
    *,
    _active: set[int] | None = None,
) -> None:
    if value is None or isinstance(value, (bool, int, str)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{path} must contain finite numbers")
        return
    if not isinstance(value, (list, tuple, Mapping)):
        raise TypeError(f"{path} contains unsupported {type(value).__name__}")
    active = set() if _active is None else _active
    marker = id(value)
    if marker in active:
        raise ValueError(f"{path} must not contain reference cycles")
    active.add(marker)
    try:
        if isinstance(value, Mapping):
            for key, item in value.items():
                if not isinstance(key, str):
                    raise ValueError(f"{path} must use string object keys")
                _validate_json_shape(item, f"{path}.{key}", _active=active)
        else:
            for index, item in enumerate(value):
                _validate_json_shape(item, f"{path}[{index}]", _active=active)
    finally:
        active.remove(marker)


def _finite_float(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        converted = float(value)
    except (OverflowError, TypeError, ValueError):
        return None
    return converted if math.isfinite(converted) else None


AgentHandler = Callable[[MailboxMessage], Awaitable[Any]]


class AgentOrchestrator:
    def __init__(self, mailbox: Mailbox, *, lease_seconds: float = 60.0) -> None:
        if (
            isinstance(lease_seconds, bool)
            or not isinstance(lease_seconds, (int, float))
            or _finite_float(lease_seconds) is None
            or lease_seconds <= 0
        ):
            raise ValueError("lease_seconds must be a finite positive number")
        self.mailbox = mailbox
        self.lease_seconds = float(lease_seconds)
        self._agents: dict[str, AgentHandler] = {}

    def register(self, name: str, handler: AgentHandler) -> None:
        if not name or name in self._agents:
            raise ValueError(f"invalid or duplicate agent name {name!r}")
        self._agents[name] = handler

    async def handoff(self, *, sender: str, recipient: str, payload: Mapping[str, Any], message_id: str | None = None) -> Any:
        if recipient not in self._agents:
            raise KeyError(f"unknown agent {recipient!r}")
        sent = self.mailbox.send(sender=sender, recipient=recipient, payload=payload, message_id=message_id)
        if sent.status == "acknowledged":
            raise MailboxLeaseError(f"message {sent.id!r} was already acknowledged")
        messages = self.mailbox.claim(
            recipient,
            limit=1,
            lease_seconds=self.lease_seconds,
            message_id=sent.id,
        )
        message = next((m for m in messages if m.id == sent.id), None)
        if message is None:
            raise MailboxLeaseError(
                f"message {sent.id!r} is currently leased by another member"
            )
        try:
            result = await self._agents[recipient](message)
        except BaseException:
            # Preserve the handler failure. A concurrently recovered lease is
            # already safe to redeliver and must not mask that real exception.
            try:
                self.mailbox.release(message.id, lease_token=message.lease_token or "")
            except MailboxLeaseError:
                pass
            raise
        self.mailbox.acknowledge(message.id, lease_token=message.lease_token or "")
        return result
