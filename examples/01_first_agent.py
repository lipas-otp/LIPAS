"""Lesson 01 — a useful Agent in one ordinary Python file.

Run from the repository root::

    ollama pull gemma4:12b
    python -m examples.01_first_agent

Use this shape for a small assistant with one coherent job.  Copy the file,
replace the local catalogue with your own read function, and change the
instructions.  Do *not* add a Team, Skill, or policy until the job needs one.

What to notice:

* ``@tool`` turns an ordinary function into the Agent's explicit capability.
* ``side_effect="read_only"`` says this function only observes data.
* ``session=`` makes the run inspectable with ``lipas trace`` afterwards.
"""
from __future__ import annotations

from pathlib import Path

from lipas import Agent, tool


@tool(side_effect="read_only")
def lookup_book(topic: str) -> str:
    """Return a book from a deliberately tiny local catalogue."""
    catalogue = {
        "replay": "Designing Data-Intensive Applications",
        "agents": "Building LLM-Powered Applications",
    }
    return catalogue.get(topic.lower(), "No catalogue entry found.")


def build_agent(*, session: str | Path = "runs/01-first-agent.db") -> Agent:
    """Keep construction separate so a CLI or test can reuse this Agent."""
    return Agent.ollama(
        tools=[lookup_book],
        instructions="Use lookup_book for book recommendations; be concise.",
        session=session,
        max_tokens=400,
        max_iterations=3,
        budgets={"tool_calls": 3, "tokens_out": 1_200},
    )


def main() -> None:
    with build_agent() as agent:
        print("agent> thinking with the local model…", flush=True)
        result = agent.ask("Recommend a book about replay.")

    if result.is_error:
        print("agent error:", result.error)
        print("check `ollama ps` and `ollama run gemma4:12b \"hello\"`.")
    elif result.text:
        print("agent>", result.text)
    else:
        print("agent stopped without a final answer:", result.stop_reason)


if __name__ == "__main__":
    main()
