"""Lesson 04 — produce an operations brief from several bounded data sources.

Run::

    python -m examples.04_daily_brief

Use this pattern for daily business summaries, incident briefings, classroom
reports, or personal dashboards.  One Agent is still enough: all information
has one owner, one answer, and the same read-only authority.  Add a Team only
when a task needs a separate owner or recovery boundary.
"""
from __future__ import annotations

from pathlib import Path

from lipas import Agent, tool


SKILLS = Path(__file__).with_name("skills")


@tool(side_effect="read_only")
def sales_snapshot(day: str) -> dict[str, object]:
    """Return an intentionally local stand-in for a warehouse query."""
    return {
        "day": day,
        "orders": 128,
        "revenue_usd": 8420,
        "change_vs_yesterday_pct": -8.5,
    }


@tool(side_effect="read_only")
def open_incidents() -> list[dict[str, str]]:
    """Return active operational risks without mutating any incident system."""
    return [
        {"severity": "high", "summary": "Checkout latency above target."},
        {"severity": "low", "summary": "One dashboard refresh is delayed."},
    ]


def build_agent(*, session: str | Path = "runs/04-daily-brief.db") -> Agent:
    return Agent.ollama(
        tools=[sales_snapshot, open_incidents],
        skills=SKILLS / "daily-brief",
        instructions="Write a factual operations brief for a busy manager.",
        session=session,
        max_tokens=600,
        max_iterations=4,
        budgets={"tool_calls": 4, "tokens_out": 1_800},
    )


def main() -> None:
    with build_agent() as agent:
        print("agent> collecting today's metrics and incidents…", flush=True)
        result = agent.ask("Create today's operations brief and recommend one next action.")

    if result.is_error:
        print("agent error:", result.error)
    elif result.text:
        print("agent>\n", result.text)
    else:
        print("agent stopped without a final answer:", result.stop_reason)


if __name__ == "__main__":
    main()
