"""A durable Team handoff with an ordinary async Python function.

Run: ``python examples/10_team_mailbox.py``

No model or provider is required. The example shows stable message ids, leased
delivery, acknowledgement on success, and an inspectable SQLite mailbox.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from lipas import Team


async def researcher(prompt: str) -> dict[str, str]:
    """This member could instead be a separately configured ``Agent``."""
    return {"finding": f"researched: {prompt}"}


async def main() -> None:
    path = Path("runs/example-team.db")
    path.parent.mkdir(exist_ok=True)
    team = Team.open(str(path))
    team.add("researcher", researcher)

    try:
        result = await team.ask(
            "researcher",
            "release risks for LIPAS 0.9",
            sender="planner",
            message_id="release-risks-001",
        )
        message = team.mailbox.get("release-risks-001")
        print("result:", result)
        print("mailbox status:", message.status if message else "missing")
        print("delivery attempts:", message.attempts if message else "?")
    finally:
        team.close()


if __name__ == "__main__":
    asyncio.run(main())
