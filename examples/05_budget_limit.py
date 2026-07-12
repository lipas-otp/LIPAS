"""Lesson 05 — reject an oversized request before it reaches a model.

Run::

    python -m examples.05_budget_limit

This needs the Ollama extra installed, but it does **not** need an Ollama
daemon or model: the budget is rejected during local pre-flight.  Use this
when a request has a hard spend or output limit.  The resulting rejection is
recorded in the same session as successful work.
"""
from __future__ import annotations

from pathlib import Path

from lipas import Agent


def build_agent(*, session: str | Path = "runs/05-budget-limit.db") -> Agent:
    return Agent.ollama(
        instructions="Answer concisely.",
        session=session,
        # This request could consume 500 tokens, but policy permits only 50.
        # LIPAS rejects it before talking to the local model.
        max_tokens=500,
        budgets={"tokens_out": 50},
    )


def main() -> None:
    with build_agent() as agent:
        result = agent.ask("Write a detailed essay about resilient systems.")

    print("stop reason:", result.stop_reason)
    print("recorded rejection:", result.error)
    print("no model request was sent; inspect with:")
    print("  python -m lipas.cli effects runs/05-budget-limit.db")


if __name__ == "__main__":
    main()
