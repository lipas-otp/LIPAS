"""A supervised Agent using the current high-level API.

Run after ``ollama serve`` and ``ollama pull gemma4:12b``:
    python -m examples.09_loop_with_supervisor

The policy is intentionally non-blocking. It shows that supervision observes
the ordinary Agent effect tape without requiring a separate loop or gate.
"""
from __future__ import annotations

import asyncio
import os
from lipas import Agent, project_supervisor
from lipas.adapter import OllamaAdapter
from lipas.supervisor import (
    Policy,
    PolicyRule,
    RetryAction,
)
from lipas.tools import tool, SideEffectClass

MODEL = os.environ.get("LIPAS_OLLAMA_MODEL", "gemma4:12b")


# ── 1. Tool ─────────────────────────────────────────────────────────

@tool(side_effect=SideEffectClass.READ_ONLY)
def search(query: str) -> str:
    """Search the web for `query` and return a short summary."""
    return f"(fake) top result for: {query}"


def observe_only(_view, _ctx):
    """Recommend retry only for a genuinely interrupted effect."""
    if not _view.orphans:
        return None
    return RetryAction(
        target_effect_id=_view.orphans[0],
        max_attempts=1,
        reason="demonstration policy",
    )


async def run(user_question: str) -> str:
    policy = Policy.of(PolicyRule("observe_only", observe_only))
    agent = Agent(
        adapter=OllamaAdapter(),
        model=MODEL,
        instructions="Use search for factual questions and answer concisely.",
        tools=[search],
        supervisor_policy=policy,
    )
    try:
        result = await agent(user_question)
        projection = project_supervisor(agent.rowset.store)
        print("stop reason:", result.stop_reason)
        print("answer:", result.text)
        print("recorded retry recommendations:", len(projection.retries))
        return result.text
    finally:
        agent.close()


if __name__ == "__main__":
    print(asyncio.run(run("Why is the sky blue?")))
