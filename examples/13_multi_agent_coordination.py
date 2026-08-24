"""Lesson 13: coordinate durable Agent ownership without a graph database.

This example is provider-free. Run it twice: the second invocation replays the
same terminal handoffs from ExecutionStore instead of calling members again.
"""
from __future__ import annotations

import asyncio
from typing import Any

from lipas import AgentCoordinator


async def research(topic: str) -> dict[str, Any]:
    return {"topic": topic, "facts": ["leases fence owners", "results replay"]}


async def write_brief(research_result: dict[str, Any]) -> str:
    facts = "; ".join(research_result["facts"])
    return f"{research_result['topic']}: {facts}."


async def assess(payload: dict[str, str]) -> dict[str, str]:
    return {"lens": payload["lens"], "finding": f"checked {payload['topic']}"}


async def synthesize(payload: dict[str, Any]) -> str:
    findings = ", ".join(
        item["value"]["lens"] for item in payload["results"]
    )
    return f"Independent review complete: {findings}."


async def main() -> None:
    with AgentCoordinator.open("runs/13-multi-agent.db") as coordinator:
        coordinator.add("researcher", research)
        coordinator.add("writer", write_brief)
        coordinator.add("assessor", assess, redelivery_safe=True)
        coordinator.add("synthesizer", synthesize)

        brief = await coordinator.sequential(
            ["researcher", "writer"],
            "durable multi-Agent coordination",
            coordination_id="lesson-13-brief-v1",
        )
        print(brief.value)

        review = await coordinator.map_reduce(
            [
                ("assessor", {"lens": "safety", "topic": brief.value}),
                ("assessor", {"lens": "quality", "topic": brief.value}),
                ("assessor", {"lens": "operations", "topic": brief.value}),
            ],
            "synthesizer",
            coordination_id="lesson-13-review-v1",
            max_concurrency=2,
        )
        print(review.value)
        print(
            "replayed handoffs:",
            sum(outcome.replayed for outcome in (*brief.outcomes, *review.outcomes)),
        )


if __name__ == "__main__":
    asyncio.run(main())
