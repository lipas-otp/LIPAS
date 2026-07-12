"""Use a Supervisor policy from the ordinary high-level Agent API.

Run from the repository root: ``python -m examples.12_supervised_agent``.
It uses the documented local Ollama default; use ``Agent(adapter=...)`` for a
different provider.
"""
from __future__ import annotations

import asyncio

from lipas import Agent, project_supervisor
from lipas.supervisor import Policy, PolicyRule, TerminateAction


def require_review(_view, _ctx):
    return TerminateAction(reason="demo policy requires review")


async def main() -> None:
    policy = Policy.of(PolicyRule("require_review", require_review))
    agent = Agent.ollama(
        instructions="Draft a concise support reply.",
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
