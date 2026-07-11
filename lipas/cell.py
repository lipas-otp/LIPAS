"""Small, ordinary-Python composition for supervised mailbox workers.

``AgentCell`` is deliberately not a workflow DSL.  It adapts an async Python
callable (including ``Agent``) into a named mailbox recipient, preserving the
message id in result metadata and optionally ticking an existing Supervisor.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Mapping, Protocol

from .behaviour import FinalResult
from .orchestration import MailboxMessage
from .supervisor_gate import SupervisorGate

__all__ = ["AgentCell", "CellHandler"]


class CellHandler(Protocol):
    def __call__(self, prompt: Any) -> Awaitable[Any]: ...


@dataclass
class AgentCell:
    """Name an ordinary async worker and give it a mailbox-safe handler.

    ``prompt`` defaults to the complete message payload when no ``"prompt"``
    key is supplied. This keeps simple scripts simple while allowing callers
    to use structured handoff payloads. The receiving code, not LIPAS, owns
    domain-specific routing and result serialization.
    """
    name: str
    worker: CellHandler
    supervisor_gate: SupervisorGate | None = None

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("cell name must be non-empty")

    async def handle(self, message: MailboxMessage) -> Any:
        prompt = message.payload.get("prompt", message.payload)
        result = await self.worker(prompt)
        # Supervisor recommendations remain advisory. A terminating recommendation
        # is surfaced to the caller rather than silently discarding a completed
        # worker result or inventing a new workflow control plane.
        if self.supervisor_gate is not None and not self.supervisor_gate.should_continue():
            if isinstance(result, FinalResult):
                return FinalResult(
                    text=result.text, state=result.state, stop_reason="supervisor_halt",
                    error=result.error, metadata={**result.metadata, "message_id": message.id},
                )
            return {"result": result, "message_id": message.id, "supervisor_halt": True}
        if isinstance(result, FinalResult):
            return FinalResult(
                text=result.text, state=result.state, stop_reason=result.stop_reason,
                error=result.error, metadata={**result.metadata, "message_id": message.id},
            )
        return result
