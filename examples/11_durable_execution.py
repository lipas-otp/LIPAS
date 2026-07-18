"""Lesson 11 — suspend a durable Agent for approval, then resume it.

Run::

    python -m examples.11_durable_execution

This provider-free lesson shows the two records used by durable execution:
``ExecutionStore`` owns Task/Run/checkpoint/interrupt state, while the Agent's
SQLite session owns Claims and Effects. A write is checkpointed before an
approval interrupt, then the same run resumes without repeating its prompt or
completed effects.
"""
from __future__ import annotations

import asyncio
from decimal import Decimal
from pathlib import Path
from typing import AsyncIterator

from lipas import (
    Agent,
    ExecutionStore,
    RunSuspended,
    tool,
    writes_require_approval,
)
from lipas.adapter import (
    Done,
    Reply,
    Request,
    ResourceEstimate,
    StreamEvent,
    Usage,
)


class DemoAdapter:
    """Return one tool request and then one final answer, without a provider."""

    name = "durable-demo"

    def __init__(self) -> None:
        self._replies = iter((
            Reply(
                content=({
                    "type": "tool_use",
                    "id": "provider-write-note",
                    "name": "write_note",
                    "input": {"text": "approved note"},
                },),
                usage=Usage(input=1, output=1),
                stop_reason="tool_use",
                model=self.name,
            ),
            Reply(
                content=({"type": "text", "text": "The note was saved."},),
                usage=Usage(input=1, output=1),
                stop_reason="end_turn",
                model=self.name,
            ),
        ))

    async def estimate_cost(self, request: Request) -> ResourceEstimate:
        return ResourceEstimate(
            model=request.model,
            input_tokens=0,
            max_output_tokens=request.max_tokens,
            max_cost_usd=Decimal("0"),
        )

    async def stream(self, request: Request) -> AsyncIterator[StreamEvent]:
        yield Done(next(self._replies))


saved_notes: set[str] = set()


@tool(side_effect="idempotent_write")
def write_note(text: str) -> str:
    """Save one note after the durable approval boundary allows it."""
    # A set makes the declared idempotency concrete: repeating the same
    # logical write has the same final state.
    saved_notes.add(text)
    return text


async def run_demo(root: Path = Path("runs")) -> None:
    root.mkdir(parents=True, exist_ok=True)
    with Agent(
        adapter=DemoAdapter(),
        model="durable-demo",
        tools=[write_note],
        session_path=root / "11-claims.db",
    ) as agent:
        # ExecutionStore remains authoritative for control state. Passing the
        # Agent RowSet also mirrors its transitions into the Claim evidence
        # tape through a crash-repairable transaction outbox.
        with ExecutionStore(
            root / "11-execution.db",
            rowset=agent.rowset,
        ) as executions:
            task = executions.create_task(
                "write one approved note",
                Path.cwd(),
            )
            run = executions.create_run(task.id)
            try:
                await agent.run_durable(
                    "Write the note and confirm it.",
                    execution_store=executions,
                    run_id=run.id,
                    approval_policy=writes_require_approval,
                )
            except RunSuspended as suspended:
                print("run state before approval:", executions.get_run(run.id).state.value)
                print("approval request:", dict(suspended.interrupt.request))
                executions.resolve_interrupt(
                    suspended.interrupt.id,
                    allow=True,
                    response={"approved_by": "lesson-user"},
                )
                result = await agent.resume_durable(
                    execution_store=executions,
                    run_id=run.id,
                    approval_policy=writes_require_approval,
                )
            else:  # pragma: no cover - the policy above always suspends the write
                raise AssertionError("the write should require approval")

            finished = executions.get_run(run.id)

    print("saved notes:", sorted(saved_notes))
    print("agent result:", result.text)
    print("final run state:", finished.state.value if finished else "missing")


def main() -> None:
    saved_notes.clear()
    asyncio.run(run_demo())


if __name__ == "__main__":
    main()
