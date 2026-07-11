"""A durable two-worker handoff with ordinary async Python functions.

Run: ``python examples/10_agentcell_mailbox.py``

No model or provider is required. The example shows the runtime boundary that
multi-agent applications build on: stable message ids, leased delivery, ack on
success, and an inspectable SQLite mailbox.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from lipas import AgentCell, AgentOrchestrator, Mailbox


async def researcher(prompt: str) -> dict[str, str]:
    """This could instead call a separately configured ``Agent``."""
    return {"finding": f"researched: {prompt}"}


async def main() -> None:
    path = Path("runs/example-team.db")
    path.parent.mkdir(exist_ok=True)
    mailbox = Mailbox(str(path))
    team = AgentOrchestrator(mailbox)

    cell = AgentCell("researcher", researcher)
    team.register(cell.name, cell.handle)

    try:
        result = await team.handoff(
            sender="planner",
            recipient="researcher",
            payload={"prompt": "release risks for LIPAS 0.9"},
            message_id="release-risks-001",
        )
        message = mailbox.get("release-risks-001")
        print("result:", result)
        print("mailbox status:", message.status if message else "missing")
        print("delivery attempts:", message.attempts if message else "?")
    finally:
        mailbox.close()


if __name__ == "__main__":
    asyncio.run(main())
