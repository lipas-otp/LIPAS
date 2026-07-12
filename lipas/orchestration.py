"""Named-agent handoffs with a durable at-least-once leased mailbox."""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Mapping

from .calculus import Claim
from .rows import RowSet

TAG_AGENT_HANDOFF = "agent_handoff"
TAG_AGENT_MAIL_CLAIM = "agent_mail_claim"
TAG_AGENT_MAIL_ACK = "agent_mail_ack"
TAG_AGENT_MAIL_RELEASED = "agent_mail_released"
TAG_AGENT_MAIL_RECOVERED = "agent_mail_recovered"

__all__ = [
    "MailboxMessage", "Mailbox", "AgentOrchestrator", "MailboxLeaseError",
    "TAG_AGENT_HANDOFF", "TAG_AGENT_MAIL_CLAIM", "TAG_AGENT_MAIL_ACK",
    "TAG_AGENT_MAIL_RELEASED", "TAG_AGENT_MAIL_RECOVERED",
]


class MailboxLeaseError(RuntimeError): pass


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
    def __init__(self, path: str = ":memory:", *, rowset: RowSet | None = None) -> None:
        self._conn = sqlite3.connect(path)
        self._conn.execute("""CREATE TABLE IF NOT EXISTS mailbox (
            id TEXT PRIMARY KEY, sender TEXT NOT NULL, recipient TEXT NOT NULL,
            payload TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('pending','leased','acknowledged')),
            attempts INTEGER NOT NULL DEFAULT 0, lease_token TEXT, lease_expires REAL,
            created_at REAL NOT NULL, acknowledged_at REAL)""")
        self._conn.commit(); self.rowset = rowset

    def close(self) -> None: self._conn.close()
    def get(self, message_id: str) -> MailboxMessage | None:
        row = self._conn.execute("SELECT id,sender,recipient,payload,status,attempts,lease_token FROM mailbox WHERE id=?", (message_id,)).fetchone()
        return None if row is None else MailboxMessage(row[0], row[1], row[2], json.loads(row[3]), row[4], row[5], row[6])

    def send(self, *, sender: str, recipient: str, payload: Mapping[str, Any], message_id: str | None = None) -> MailboxMessage:
        mid = message_id or f"msg_{uuid.uuid4().hex}"
        encoded = json.dumps(dict(payload), sort_keys=True, separators=(",", ":"))
        try:
            with self._conn:
                self._conn.execute("INSERT INTO mailbox(id,sender,recipient,payload,status,created_at) VALUES(?,?,?,?,'pending',?)", (mid, sender, recipient, encoded, time.time()))
        except sqlite3.IntegrityError:
            existing = self.get(mid)
            assert existing is not None
            if (existing.sender, existing.recipient, dict(existing.payload)) != (sender, recipient, dict(payload)):
                raise ValueError("message id was reused for a different handoff")
            return existing
        self._audit(TAG_AGENT_HANDOFF, {"message_id": mid, "sender": sender, "recipient": recipient, "payload": dict(payload)})
        return self.get(mid)  # type: ignore[return-value]

    def recover_expired(self, *, now: float | None = None) -> int:
        now = time.time() if now is None else now
        with self._conn:
            cur = self._conn.execute("UPDATE mailbox SET status='pending',lease_token=NULL,lease_expires=NULL WHERE status='leased' AND lease_expires<=?", (now,))
        if cur.rowcount: self._audit(TAG_AGENT_MAIL_RECOVERED, {"count": cur.rowcount})
        return cur.rowcount

    def claim(self, recipient: str, *, limit: int = 1, lease_seconds: float = 60.0) -> tuple[MailboxMessage, ...]:
        if limit < 1 or lease_seconds <= 0: raise ValueError("limit and lease_seconds must be positive")
        self.recover_expired()
        rows = self._conn.execute("SELECT id FROM mailbox WHERE recipient=? AND status='pending' ORDER BY created_at,id LIMIT ?", (recipient, limit)).fetchall()
        claimed = []
        for (mid,) in rows:
            token, expiry = uuid.uuid4().hex, time.time() + lease_seconds
            with self._conn:
                cur = self._conn.execute("UPDATE mailbox SET status='leased',attempts=attempts+1,lease_token=?,lease_expires=? WHERE id=? AND status='pending'", (token, expiry, mid))
            if cur.rowcount:
                message = self.get(mid)
                assert message is not None
                claimed.append(message)
                self._audit(TAG_AGENT_MAIL_CLAIM, {"message_id": mid, "recipient": recipient, "attempt": message.attempts})
        return tuple(claimed)

    def acknowledge(self, message_id: str, *, lease_token: str) -> None:
        with self._conn:
            cur = self._conn.execute("UPDATE mailbox SET status='acknowledged',acknowledged_at=?,lease_token=NULL,lease_expires=NULL WHERE id=? AND status='leased' AND lease_token=?", (time.time(), message_id, lease_token))
        if not cur.rowcount: raise MailboxLeaseError("message is not owned by this lease")
        self._audit(TAG_AGENT_MAIL_ACK, {"message_id": message_id})

    def release(self, message_id: str, *, lease_token: str) -> None:
        with self._conn:
            cur = self._conn.execute("UPDATE mailbox SET status='pending',lease_token=NULL,lease_expires=NULL WHERE id=? AND status='leased' AND lease_token=?", (message_id, lease_token))
        if not cur.rowcount: raise MailboxLeaseError("message is not owned by this lease")
        self._audit(TAG_AGENT_MAIL_RELEASED, {"message_id": message_id})

    def _audit(self, tag: str, fields: Mapping[str, Any]) -> None:
        if self.rowset is not None: self.rowset.fold(Claim(tag=tag, fields=dict(fields), source="orchestration.mailbox"))


AgentHandler = Callable[[MailboxMessage], Awaitable[Any]]


class AgentOrchestrator:
    def __init__(self, mailbox: Mailbox, *, lease_seconds: float = 60.0) -> None:
        self.mailbox, self.lease_seconds, self._agents = mailbox, lease_seconds, {}

    def register(self, name: str, handler: AgentHandler) -> None:
        if not name or name in self._agents: raise ValueError(f"invalid or duplicate agent name {name!r}")
        self._agents[name] = handler

    async def handoff(self, *, sender: str, recipient: str, payload: Mapping[str, Any], message_id: str | None = None) -> Any:
        if recipient not in self._agents: raise KeyError(f"unknown agent {recipient!r}")
        sent = self.mailbox.send(sender=sender, recipient=recipient, payload=payload, message_id=message_id)
        if sent.status == "acknowledged": raise MailboxLeaseError(f"message {sent.id!r} was already acknowledged")
        messages = self.mailbox.claim(recipient, limit=1, lease_seconds=self.lease_seconds)
        message = next((m for m in messages if m.id == sent.id), None)
        if message is None: raise MailboxLeaseError(f"message {sent.id!r} is currently leased by another member")
        try:
            result = await self._agents[recipient](message)
        except BaseException:
            self.mailbox.release(message.id, lease_token=message.lease_token or "")
            raise
        self.mailbox.acknowledge(message.id, lease_token=message.lease_token or "")
        return result
