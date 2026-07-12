"""Lesson 10 — use a Team for a real two-owner research review.

Run::

    python -m examples.10_research_review_team

This is a miniature version of a research-screening workflow.  The planner
hands a question to a researcher, then hands that evidence to a reviewer.  In
a production system either member can be an Agent; stable message ids remain
the idempotency and replay keys at each handoff.
"""
from __future__ import annotations

from uuid import uuid4

from lipas import Team


async def researcher(topic: str) -> dict[str, object]:
    """Collect source facts; replace with an Agent plus search tools if needed."""
    return {
        "topic": topic,
        "papers": [
            "Replayable Agent Systems (2025)",
            "Human Approval for Automated Decisions (2023)",
        ],
        "limitation": "This demo uses a local catalogue, not a live index.",
    }


async def reviewer(evidence: dict[str, object]) -> dict[str, object]:
    """Turn evidence into a decision without silently changing its source."""
    return {
        "recommendation": "shortlist for human reading",
        "evidence_count": len(evidence["papers"]),
        "caveat": evidence["limitation"],
    }


def main() -> None:
    with Team.open("runs/10-research-review-team.db") as team:
        team.add("researcher", researcher)
        team.add("reviewer", reviewer)

        research_id = f"screen-{uuid4().hex}"
        evidence = team.ask_sync(
            "researcher",
            "reliable AI agents for education",
            sender="planner",
            message_id=research_id,
        )
        review_id = f"review-{uuid4().hex}"
        decision = team.ask_sync(
            "reviewer",
            evidence,
            sender="planner",
            message_id=review_id,
        )

    print("research handoff:", research_id)
    print("evidence:", evidence)
    print("review handoff:", review_id)
    print("decision:", decision)


if __name__ == "__main__":
    main()
