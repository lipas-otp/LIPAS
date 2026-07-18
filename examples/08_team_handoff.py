"""Lesson 08 — hand work to a separately owned, durable Team member.

Run::

    python -m examples.08_team_handoff

Choose a Team only when a task needs its own owner, restart boundary, budget,
or audit trail.  Several tools or several steps inside one answer still call
for one Agent, not a Team.  A Team member may be a full Agent or, as here, an
ordinary async Python function.
"""
from __future__ import annotations

from uuid import uuid4

from lipas import Team


async def researcher(prompt: str) -> dict[str, str]:
    """Replace this function with an Agent when the member needs a model."""
    return {"finding": f"researched: {prompt}"}


def main() -> None:
    # ``Team.open`` creates both durable mailbox and claim-session parents.
    with Team.open("runs/08-team-handoff.db") as team:
        team.add("researcher", researcher)
        message_id = f"release-risks-{uuid4().hex}"
        result = team.ask_sync(
            "researcher",
            "release risks for the current LIPAS source",
            sender="planner",
            message_id=message_id,
        )
        message = team.mailbox.get(message_id)

    print("message id:", message_id)
    print("result:", result)
    print("mailbox status:", message.status if message else "missing")
    print("delivery attempts:", message.attempts if message else "?")


if __name__ == "__main__":
    main()
