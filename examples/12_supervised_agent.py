"""Use a Supervisor policy from the ordinary high-level Agent API.

Run: ``python examples/12_supervised_agent.py``

FakeAdapter keeps the example offline. Substitute OllamaAdapter or an OpenAI
adapter in a real application; supervision still observes the same claim tape.
"""
from __future__ import annotations

import asyncio

from lipas import Agent, project_supervisor
from lipas.supervisor import Policy, PolicyRule, TerminateAction
from lipas.testing.fake_adapter import FakeAdapter


def require_review(_view, _ctx):
    return TerminateAction(reason="demo policy requires review")


async def main() -> None:
    policy = Policy.of(PolicyRule("require_review", require_review))
    agent = Agent(
        adapter=FakeAdapter.echoing(),
        model="demo-model",
        supervisor_policy=policy,
    )
    try:
        result = await agent("Draft a support reply.")
        projection = project_supervisor(agent.rowset.store)
        print("agent stop reason:", result.stop_reason)
        print("supervisor terminated:", projection.terminated)
        print("supervisor reason:", projection.terminate_reason)
    finally:
        agent.close()


if __name__ == "__main__":
    asyncio.run(main())
