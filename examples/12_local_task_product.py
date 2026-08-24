"""Lesson 12 — deliver a verified local workspace change after interruption.

Run::

    python -m examples.12_local_task_product

This provider-free lesson exercises the first-party product path: a ChangeSet
keeps edits away from the original workspace, a command pauses for durable
approval, a newly opened Workbench and Agent resume the same Run, and the
verified diff is applied only after explicit delivery.

The lesson selects the explicit ``local`` command backend so it runs on hosts
without Bubblewrap. Use the default ``auto`` backend for real tasks; it fails
closed unless an OS isolation boundary is available.
"""
from __future__ import annotations

import asyncio
from decimal import Decimal
from pathlib import Path
from typing import AsyncIterator, Iterable

from lipas import Agent, RunSuspended, Workbench
from lipas.adapter import Done, Reply, Request, ResourceEstimate, StreamEvent, Usage


class DemoAdapter:
    """Return scripted tool requests without contacting a model provider."""

    name = "local-task-demo"

    def __init__(self, replies: Iterable[Reply]) -> None:
        self._replies = iter(replies)

    async def estimate_cost(self, request: Request) -> ResourceEstimate:
        return ResourceEstimate(
            model=request.model,
            input_tokens=0,
            max_output_tokens=request.max_tokens,
            max_cost_usd=Decimal("0"),
        )

    async def stream(self, request: Request) -> AsyncIterator[StreamEvent]:
        yield Done(next(self._replies))


def _tool_reply(name: str, arguments: dict[str, object], call_id: str) -> Reply:
    return Reply(
        content=({
            "type": "tool_use",
            "id": call_id,
            "name": name,
            "input": arguments,
        },),
        usage=Usage(input=1, output=1),
        stop_reason="tool_use",
        model=DemoAdapter.name,
    )


def _final_reply() -> Reply:
    return Reply(
        content=({
            "type": "text",
            "text": "The staged note was written and verification passed.",
        },),
        usage=Usage(input=1, output=1),
        stop_reason="end_turn",
        model=DemoAdapter.name,
    )


async def run_demo(root: Path = Path("runs")) -> None:
    workspace = root / "12-workspace"
    home = root / "12-workbench"
    workspace.mkdir(parents=True, exist_ok=True)
    original = workspace / "note.txt"
    original.write_text("before\n", encoding="utf-8")

    # Phase one writes only to the staged snapshot. The subsequent command is
    # checkpointed as an Interrupt before it can execute.
    with Workbench(home, sandbox="local") as workbench:
        task, run = workbench.create_task(
            "Update note.txt and verify the workspace.",
            workspace,
            isolate_changes=True,
        )
        claims_path = workbench.claims_path_for_run(run.id)
        with Agent(
            adapter=DemoAdapter((
                _tool_reply(
                    "write_workspace_file",
                    {"relative_path": "note.txt", "content": "after\n"},
                    "write-note",
                ),
                _tool_reply(
                    "run_workspace_command",
                    {"argv": ["python", "-m", "compileall", "."]},
                    "verify-workspace",
                ),
            )),
            model=DemoAdapter.name,
            tools=workbench.workspace_tools(task.id, run.id),
            session_path=claims_path,
        ) as agent:
            with workbench.execution_scope(agent.rowset, run_id=run.id) as execution:
                try:
                    await agent.run_durable(
                        task.goal,
                        execution_store=execution,
                        run_id=run.id,
                        approval_policy=workbench.approval_policy(task.id),
                    )
                except RunSuspended as suspended:
                    workbench.record_approval_required(suspended.interrupt)
                    approval_id = suspended.interrupt.id
                else:  # pragma: no cover - command always needs approval
                    raise AssertionError(
                        "verification command should require approval",
                    )

        change_set = workbench.change_set(task.id)
        assert change_set is not None
        staged = Path(change_set.stage_root)
        print("staged note before approval:", (staged / "note.txt").read_text().strip())
        print("original before approval:", original.read_text().strip())
        waiting = workbench.execution.get_run(run.id)
        print("run state before approval:", waiting.state.value if waiting else "missing")

    # Closing and reopening both stores models a process interruption. The new
    # Agent resumes the checkpointed Run instead of repeating the write.
    with Workbench(home, sandbox="local") as workbench:
        workbench.resolve_approval(
            approval_id,
            allow=True,
            response={"approved_by": "lesson-user"},
        )
        with Agent(
            adapter=DemoAdapter((_final_reply(),)),
            model=DemoAdapter.name,
            tools=workbench.workspace_tools(task.id, run.id),
            session_path=workbench.claims_path_for_run(run.id),
        ) as agent:
            with workbench.execution_scope(agent.rowset, run_id=run.id) as execution:
                result = await agent.resume_durable(
                    execution_store=execution,
                    run_id=run.id,
                    approval_policy=workbench.approval_policy(task.id),
                )

        report = workbench.build_report(task.id, result)
        print("original before apply:", original.read_text().strip())
        print("verified:", report.verified)
        print("change set state:", report.change_set_state)
        print("staged diff:\n" + report.diff.rstrip())
        applied = workbench.apply_change_set(task.id)
        print("applied files:", list(applied))
        print("original after apply:", original.read_text().strip())


def main() -> None:
    asyncio.run(run_demo())


if __name__ == "__main__":
    main()
