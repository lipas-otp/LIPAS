"""The small ergonomic entry point for a group of Agents or functions.

``Team`` owns no new scheduling semantics. It is a plain-Python facade over
``Mailbox`` and ``AgentOrchestrator`` for applications that do not need to
wire those two objects separately.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .orchestration import AgentOrchestrator, Mailbox

__all__ = ["Team"]


@dataclass
class Team:
    """A named collection of ordinary Python assistants.

    Example::

        team = Team.open("runs/team.db")
        team.add("research", researcher)
        answer = await team.ask("research", "check release risks")
        team.close()

    ``ask`` uses a durable mailbox handoff.  Pass ``message_id=`` whenever the
    caller needs a stable idempotency/replay key across process restarts.
    """
    mailbox: Mailbox
    lease_seconds: float = 60.0
    _orchestrator: AgentOrchestrator = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._orchestrator = AgentOrchestrator(self.mailbox, lease_seconds=self.lease_seconds)

    @classmethod
    def open(cls, path: str = ":memory:", *, lease_seconds: float = 60.0) -> "Team":
        return cls(Mailbox(path), lease_seconds=lease_seconds)

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

    def close(self) -> None:
        self.mailbox.close()
