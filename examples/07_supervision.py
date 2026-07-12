"""Lesson 07 — record a Supervisor decision beside the Agent's effects.

Run::

    python -m examples.07_supervision

The small deterministic adapter makes this fully runnable without a provider.
The important idea is not the rule itself: a Supervisor reads the same effect
tape as the Agent and records a termination or escalation recommendation.
Use a Supervisor when a concrete policy must be inspectable, not hidden in a
prompt or an unrecorded ``if`` statement.
"""
from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import AsyncIterator, ClassVar

from lipas import Agent, project_supervisor
from lipas.adapter import Done, Reply, Request, ResourceEstimate, StreamEvent, Usage
from lipas.supervisor import Policy, PolicyRule, TerminateAction


class DemoAdapter:
    """Return one reply so the lesson exercises no network or model."""

    name: ClassVar[str] = "supervisor-demo"

    async def estimate_cost(self, request: Request) -> ResourceEstimate:
        return ResourceEstimate(
            model=request.model,
            input_tokens=0,
            max_output_tokens=request.max_tokens,
            max_cost_usd=Decimal("0"),
        )

    async def stream(self, request: Request) -> AsyncIterator[StreamEvent]:
        yield Done(Reply(
            content=({"type": "text", "text": "Draft reply ready."},),
            usage=Usage(input=1, output=1),
            stop_reason="end_turn",
            model=request.model,
        ))


def require_human_review(_view, _context) -> TerminateAction:
    """A real rule could examine spend, effect failures, or risk signals."""
    return TerminateAction(reason="demo policy requires review")


async def run_demo() -> None:
    policy = Policy.of(PolicyRule("require_human_review", require_human_review))
    with Agent(
        adapter=DemoAdapter(),
        model="supervisor-demo",
        supervisor_policy=policy,
    ) as agent:
        result = await agent("Draft a support reply.")
        projection = project_supervisor(agent.rowset.store)

    print("agent stop reason:", result.stop_reason)
    print("supervisor terminated:", projection.terminated)
    print("supervisor reason:", projection.terminate_reason)


def main() -> None:
    asyncio.run(run_demo())


if __name__ == "__main__":
    main()
