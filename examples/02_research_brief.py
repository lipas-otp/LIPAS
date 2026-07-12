"""Lesson 02 — turn source records into a careful research brief.

Run::

    python -m examples.02_research_brief

This is the right pattern for literature triage, policy research, or internal
knowledge review: a read-only search tool provides facts; a Skill supplies a
repeatable writing standard; the Agent synthesizes the answer.  Replace the
local ``PAPERS`` list with a real search API, but retain its read-only effect
declaration.
"""
from __future__ import annotations

import re
from pathlib import Path

from lipas import Agent, tool


PAPERS = [
    {
        "title": "Replayable Agent Systems",
        "year": 2025,
        "abstract": "Records tool effects and reuses them during safe replay.",
    },
    {
        "title": "Reliable Retrieval for Education",
        "year": 2024,
        "abstract": "Compares retrieval quality and teacher review workflows.",
    },
    {
        "title": "Human Approval for Automated Decisions",
        "year": 2023,
        "abstract": "Studies explicit escalation and reversible decisions.",
    },
]
SKILLS = Path(__file__).with_name("skills")


@tool(side_effect="read_only")
def search_papers(topic: str) -> list[dict[str, object]]:
    """Return matching papers; a production version would call a search API."""
    words = {word.lower() for word in re.findall(r"[A-Za-z]{3,}", topic)}
    matches = [
        paper for paper in PAPERS
        if words & set(f"{paper['title']} {paper['abstract']}".lower().split())
    ]
    return matches or PAPERS


def build_agent(*, session: str | Path = "runs/02-research-brief.db") -> Agent:
    return Agent.ollama(
        tools=[search_papers],
        # A Skill is guidance, never a permission grant. Tools remain the
        # only capability that can execute.
        skills=SKILLS / "research-brief",
        instructions="Prepare a short research brief for a non-specialist reader.",
        session=session,
        max_tokens=600,
        max_iterations=4,
        budgets={"tool_calls": 4, "tokens_out": 1_800},
    )


def main() -> None:
    with build_agent() as agent:
        print("agent> finding and reading local sources…", flush=True)
        result = agent.ask(
            "Find research relevant to reliable AI agents. Summarize the "
            "evidence and name one limitation."
        )

    if result.is_error:
        print("agent error:", result.error)
    elif result.text:
        print("agent>\n", result.text)
    else:
        print("agent stopped without a final answer:", result.stop_reason)
        print("inspect `python -m lipas.cli effects runs/02-research-brief.db`.")


if __name__ == "__main__":
    main()
