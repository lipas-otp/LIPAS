"""Named-agent handoffs with a durable, at-least-once mailbox.

The mailbox is intentionally separate from prompt history: a handoff is an
auditable command with a stable id, ownership, acknowledgement and payload.
Delivery is at-least-once; handlers must use the message id as their replay /
idempotency key when they trigger external work.
"""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Mapping

from .calculus import Claim
from .rows import RowSet

__all__ = ["MailboxMessage", "Mailbox", "AgentOrchestrator"]


@dataclass(frozen=True, slots=True)
class MailboxMessage:
    id: str
    sender: str
    recipient: str
    payload: Mapping[str, Any]
    status: str = "pending"


class Mailbox:
    def __init__(self, path: str = ":memory:", *, rowset: RowSet | None = None) -> None:
        self._conn = sqlite3.connect(path)
        self._conn.execute("CREATE TABLE IF NOT EXISTS mailbox (id TEXT PRIMARY KEY, sender TEXT NOT NULL, recipient TEXT NOT NULL, payload TEXT NOT NULL, status TEXT NOT NULL, created_at REAL NOT NULL, acknowledged_at REAL)")
        self._conn.commit()
        self.rowset = rowset

    def send(self, *, sender: str, recipient: str, payload: Mapping[str, Any], message_id: str | None = None) -> MailboxMessage:
        mid = message_id or f"msg_{uuid.uuid4().hex}"
        msg = MailboxMessage(mid, sender, recipient, dict(payload))
        self._conn.execute("INSERT OR IGNORE INTO mailbox VALUES (?, ?, ?, ?, 'pending', ?, NULL)", (mid, sender, recipient, json.dumps(payload, sort_keys=True), time.time()))
        self._conn.commit()
        self._audit("agent_handoff", {"message_id": mid, "sender": sender, "recipient": recipient, "payload": dict(payload)})
        return self.get(mid) or msg

    def get(self, message_id: str) -> MailboxMessage | None:
        row = self._conn.execute("SELECT id, sender, recipient, payload, status FROM mailbox WHERE id=?", (message_id,)).fetchone()
        return None if row is None else MailboxMessage(row[0], row[1], row[2], json.loads(row[3]), row[4])

    def receive(self, recipient: str, *, limit: int = 1) -> tuple[MailboxMessage, ...]:
        rows = self._conn.execute("SELECT id, sender, recipient, payload, status FROM mailbox WHERE recipient=? AND status='pending' ORDER BY created_at, id LIMIT ?", (recipient, limit)).fetchall()
        return tuple(MailboxMessage(r[0], r[1], r[2], json.loads(r[3]), r[4]) for r in rows)

    def acknowledge(self, message_id: str) -> None:
        self._conn.execute("UPDATE mailbox SET status='acknowledged', acknowledged_at=? WHERE id=?", (time.time(), message_id))
        self._conn.commit()
        self._audit("agent_mail_ack", {"message_id": message_id})

    def _audit(self, tag: str, fields: Mapping[str, Any]) -> None:
        if self.rowset is not None:
            self.rowset.fold(Claim(tag=tag, fields=dict(fields), source="orchestration.mailbox"))


AgentHandler = Callable[[MailboxMessage], Awaitable[Any]]


class AgentOrchestrator:
    """Routes named agents through ``Mailbox``; no hidden shared state."""
    def __init__(self, mailbox: Mailbox) -> None:
        self.mailbox = mailbox
        self._agents: dict[str, AgentHandler] = {}

    def register(self, name: str, handler: AgentHandler) -> None:
        if not name: raise ValueError("agent name must be non-empty")
        if name in self._agents: raise ValueError(f"agent {name!r} already registered")
        self._agents[name] = handler

    async def handoff(self, *, sender: str, recipient: str, payload: Mapping[str, Any], message_id: str | None = None) -> Any:
        if recipient not in self._agents: raise KeyError(f"unknown agent {recipient!r}")
        message = self.mailbox.send(sender=sender, recipient=recipient, payload=payload, message_id=message_id)
        result = await self._agents[recipient](message)
        self.mailbox.acknowledge(message.id)
        return result
