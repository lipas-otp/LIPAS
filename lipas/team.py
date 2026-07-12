"""The small ergonomic entry point for a group of Agents or functions.

``Team`` owns no new scheduling semantics. It is a plain-Python facade over
``Mailbox`` and ``AgentOrchestrator`` for applications that do not need to
wire those two objects separately.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from .orchestration import AgentOrchestrator, Mailbox
from .rows import RowSet
from .rows.capability import CapabilityRow
from .rows.effect import EffectRow
from .rows.history import HistoryRow
from .session import open_session
from .store import ClaimStore

__all__ = ["Team"]


@dataclass
class Team:
    """A named collection of ordinary Python assistants.

    Example::

        with Team.open("runs/team.db") as team:
            team.add("research", researcher)
            answer = team.ask_sync("research", "check release risks")

    ``ask`` / ``ask_sync`` use a durable mailbox handoff. Pass ``message_id=``
    whenever the caller needs a stable idempotency/replay key across restarts.
    """
    mailbox: Mailbox
    rowset: RowSet
    lease_seconds: float = 60.0
    _orchestrator: AgentOrchestrator = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._orchestrator = AgentOrchestrator(self.mailbox, lease_seconds=self.lease_seconds)

    @classmethod
    def open(cls, path: str = ":memory:", *, audit_path: str | None = None,
             lease_seconds: float = 60.0) -> "Team":
        """Open a Team and its durable audit session.

        ``path`` is the mailbox database. Unless ``audit_path`` is supplied,
        a sibling ``<path>.claims.db`` records handoff/lease/ack transitions.
        """
        if path == ":memory:":
            rowset = RowSet(ClaimStore(), [HistoryRow(), CapabilityRow(), EffectRow()])
        else:
            rowset = open_session(audit_path or f"{path}.claims.db")
        return cls(Mailbox(path, rowset=rowset), rowset=rowset, lease_seconds=lease_seconds)

    def add(self, name: str, handler: Any) -> "Team":
        """Add a named team member.

        ``handler`` may be an ``Agent`` or any async callable accepting one
        prompt. It is adapted internally to the mailbox protocol; callers do
        not need a second public abstraction.
        """
        if not name or not callable(handler):
            raise TypeError("team.add(name, handler) requires a non-empty name and async callable")

        async def receive(message):
            prompt = message.payload.get("prompt", message.payload)
            # An Agent retains its normal public call surface. At a mailbox
            # boundary we additionally seed its state with the stable message
            # id, making every LLM/tool intent traceable to this handoff.
            from .agent import Agent
            if isinstance(handler, Agent):
                from .behaviour import AgentState
                return await handler.run(
                    prompt, state=AgentState(metadata={"caused_by": message.id}),
                )
            return await handler(prompt)

        self._orchestrator.register(name, receive)
        return self

    async def ask(
        self,
        recipient: str,
        prompt: Any,
        *,
        sender: str = "user",
        message_id: str | None = None,
    ) -> Any:
        return await self._orchestrator.handoff(
            sender=sender,
            recipient=recipient,
            payload={"prompt": prompt},
            message_id=message_id,
        )

    def ask_sync(
        self,
        recipient: str,
        prompt: Any,
        *,
        sender: str = "user",
        message_id: str | None = None,
    ) -> Any:
        """Run one durable handoff from a normal synchronous script.

        Async applications should use ``await team.ask(...)``. The explicit
        name avoids nesting an event loop in notebooks or web servers.
        """
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(
                self.ask(
                    recipient, prompt, sender=sender, message_id=message_id,
                )
            )
        raise RuntimeError(
            "Team.ask_sync() cannot run inside an active event loop; "
            "use `await team.ask(...)` instead"
        )

    def close(self) -> None:
        self.mailbox.close()
        close = getattr(self.rowset.store, "close", None)
        if callable(close):
            close()

    def __enter__(self) -> "Team":
        """Support ``with Team.open(...) as team`` in normal scripts."""
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()
