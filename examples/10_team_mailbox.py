"""A durable Team handoff with an ordinary async Python function.

Run from the repository root: ``python -m examples.10_team_mailbox``

No model or provider is required. The example shows stable message ids, leased
delivery, acknowledgement on success, and an inspectable SQLite mailbox.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from uuid import uuid4

from lipas import Team


async def researcher(prompt: str) -> dict[str, str]:
    """This member could instead be a separately configured ``Agent``."""
    return {"finding": f"researched: {prompt}"}


async def main() -> None:
    path = Path("runs/example-team.db")
    path.parent.mkdir(exist_ok=True)
    team = Team.open(str(path))
    team.add("researcher", researcher)
    # The caller owns this key. A production retry would retain it; this
    # repeatable demo creates a distinct handoff for each invocation.
    message_id = f"release-risks-{uuid4().hex}"

    try:
        result = await team.ask(
            "researcher",
            "release risks for LIPAS 0.9.6",
            sender="planner",
            message_id=message_id,
        )
        message = team.mailbox.get(message_id)
        print("message id:", message_id)
        print("result:", result)
        print("mailbox status:", message.status if message else "missing")
        print("delivery attempts:", message.attempts if message else "?")
    finally:
        team.close()


if __name__ == "__main__":
    asyncio.run(main())
