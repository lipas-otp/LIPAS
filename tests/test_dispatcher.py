"""Persistent multi-Task dispatcher and worker product flow."""
from __future__ import annotations

import asyncio
import re
import time

from lipas import Agent, ExecutionStore, RunState, TaskDispatcher
from lipas.adapter import Reply, Usage
from lipas.cli import main
from lipas.workbench import Workbench
from tests.fake_adapter import FakeAdapter


def _create_runs(path, workspace, count: int):
    with ExecutionStore(path) as store:
        return tuple(
            store.create_run(
                store.create_task(f"task {index}", workspace).id,
            )
            for index in range(count)
        )


def _final_reply(text: str = "done") -> Reply:
    return Reply(
        content=({"type": "text", "text": text},),
        usage=Usage(input=1, output=1),
        stop_reason="end_turn",
        model="fake",
    )


def test_dispatcher_runs_fifo_queue_with_bounded_concurrency(tmp_path):
    path = tmp_path / "execution.db"
    runs = _create_runs(path, tmp_path, 4)
    active = 0
    maximum_active = 0

    async def execute(_task, discovered):
        nonlocal active, maximum_active
        with ExecutionStore(path) as store:
            active += 1
            maximum_active = max(maximum_active, active)
            await asyncio.sleep(0.01)
            active -= 1
            store.complete_run(
                discovered.id, discovered.lease_token, result={},  # type: ignore[arg-type]
            )

    dispatcher = TaskDispatcher(path, execute, max_concurrency=2)
    outcomes = asyncio.run(dispatcher.run_until_idle())

    assert [value.run_id for value in outcomes] == [run.id for run in runs]
    assert all(value.status == "completed" for value in outcomes)
    assert maximum_active == 2


def test_dispatcher_reclaims_an_expired_run_after_restart(tmp_path):
    path = tmp_path / "execution.db"
    run = _create_runs(path, tmp_path, 1)[0]
    with ExecutionStore(path) as store:
        first = store.claim_run(
            run.id, lease_seconds=1, now=time.time() - 10,
        )
        assert first.attempt == 1

    async def execute(_task, discovered):
        with ExecutionStore(path) as store:
            store.complete_run(
                discovered.id, discovered.lease_token, result={},  # type: ignore[arg-type]
            )

    outcomes = asyncio.run(TaskDispatcher(
        path, execute, max_concurrency=1,
    ).run_until_idle())

    assert len(outcomes) == 1
    assert outcomes[0].status == "completed"
    assert outcomes[0].attempt == 2


def test_two_dispatchers_cannot_execute_the_same_run(tmp_path):
    path = tmp_path / "execution.db"
    run = _create_runs(path, tmp_path, 1)[0]
    executions: list[str] = []

    async def execute(_task, discovered):
        executions.append(discovered.id)
        await asyncio.sleep(0.02)
        with ExecutionStore(path) as store:
            store.complete_run(
                discovered.id, discovered.lease_token, result={},  # type: ignore[arg-type]
            )

    async def race():
        first = TaskDispatcher(path, execute, max_concurrency=1)
        second = TaskDispatcher(path, execute, max_concurrency=1)
        return await asyncio.gather(
            first._execute(run), second._execute(run),
        )

    first_outcome, second_outcome = asyncio.run(race())
    statuses = {first_outcome.status, second_outcome.status}
    assert executions == [run.id]
    assert statuses == {"completed", "claimed_elsewhere"}


def test_waiting_approval_releases_slot_for_the_next_task(tmp_path):
    path = tmp_path / "execution.db"
    first, second = _create_runs(path, tmp_path, 2)
    order: list[str] = []

    async def execute(_task, discovered):
        with ExecutionStore(path) as store:
            order.append(discovered.id)
            if discovered.id == first.id:
                checkpoint = store.save_checkpoint(
                    discovered.id,
                    discovered.lease_token,  # type: ignore[arg-type]
                    expected_version=0,
                    phase="ready",
                    state={"queued": True},
                )
                store.suspend(
                    discovered.id,
                    discovered.lease_token,  # type: ignore[arg-type]
                    expected_version=checkpoint.version,
                    phase="ready",
                    checkpoint_state=checkpoint.state,
                    kind="approval",
                    request={"operation": "write"},
                )
            else:
                store.complete_run(
                    discovered.id, discovered.lease_token, result={},  # type: ignore[arg-type]
                )

    outcomes = asyncio.run(TaskDispatcher(
        path, execute, max_concurrency=1,
    ).run_until_idle())

    assert order == [first.id, second.id]
    assert [value.status for value in outcomes] == ["waiting", "completed"]
    with ExecutionStore(path) as store:
        assert store.get_run(first.id).state is RunState.WAITING  # type: ignore[union-attr]
        assert store.get_run(second.id).state is RunState.COMPLETED  # type: ignore[union-attr]


def test_dispatcher_finishes_cancelled_expired_run_after_restart(tmp_path):
    path = tmp_path / "execution.db"
    run = _create_runs(path, tmp_path, 1)[0]
    with ExecutionStore(path) as store:
        claimed = store.claim_run(
            run.id, lease_seconds=1, now=time.time() - 10,
        )
        store.cancel_task(claimed.task_id)

    executed: list[str] = []

    async def execute(_task, discovered):
        executed.append(discovered.id)

    outcomes = asyncio.run(TaskDispatcher(
        path, execute, max_concurrency=1,
    ).run_until_idle())

    assert len(outcomes) == 1
    assert outcomes[0].status == "cancelled"
    assert outcomes[0].attempt == 2
    assert executed == []


def test_continuous_dispatcher_picks_up_a_task_submitted_after_start(tmp_path):
    path = tmp_path / "execution.db"
    observed = []
    stop = asyncio.Event()

    async def execute(_task, discovered):
        with ExecutionStore(path) as store:
            store.complete_run(
                discovered.id, discovered.lease_token, result={},  # type: ignore[arg-type]
            )

    def collect(outcome):
        observed.append(outcome)
        stop.set()

    async def scenario():
        dispatcher = TaskDispatcher(
            path,
            execute,
            max_concurrency=1,
            poll_interval_s=0.01,
            outcome_sink=collect,
        )
        server = asyncio.create_task(dispatcher.serve(stop))
        await asyncio.sleep(0.02)
        _create_runs(path, tmp_path, 1)
        await asyncio.wait_for(server, timeout=1)

    asyncio.run(scenario())
    assert len(observed) == 1
    assert observed[0].status == "completed"


def test_task_submit_and_worker_once_complete_multiple_tasks(
    tmp_path, monkeypatch, capsys,
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    home = tmp_path / "home"

    def fake_agent(_args, workbench, *, task_id, run_id):
        return Agent(
            adapter=FakeAdapter.from_replies([_final_reply(task_id)]),
            model="fake",
            tools=workbench.workspace_tools(task_id, run_id),
            session_path=workbench.claims_path_for_run(run_id),
        )

    monkeypatch.setattr("lipas.cli._workbench_agent", fake_agent)
    for goal in ("first task", "second task"):
        assert main([
            "task", "submit", str(workspace), goal, "--home", str(home),
        ]) == 0
    capsys.readouterr()

    assert main([
        "task", "worker", "--once", "--max-concurrency", "2",
        "--home", str(home), "--sandbox", "local",
    ]) == 0
    output = capsys.readouterr().out
    assert output.count("status=completed") == 2
    with Workbench(home) as workbench:
        assert all(
            run.state is RunState.COMPLETED
            for run in workbench.execution.list_runs()
        )
        for task in workbench.list_tasks():
            kinds = [event.kind for event in workbench.events(task.id)]
            assert "dispatch_started" in kinds
            assert "dispatch_finished" in kinds


def test_worker_releases_approval_slot_and_resumes_deferred_run(
    tmp_path, monkeypatch, capsys,
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    home = tmp_path / "home"
    replies = iter([
        Reply(
            content=({
                "type": "tool_use",
                "id": "write_note",
                "name": "write_workspace_file",
                "input": {"relative_path": "note.txt", "content": "queued\n"},
            },),
            usage=Usage(input=1, output=1),
            stop_reason="tool_use",
            model="fake",
        ),
        Reply(
            content=({
                "type": "tool_use",
                "id": "verify_note",
                "name": "run_workspace_command",
                "input": {
                    "argv": ["python", "-m", "compileall", "."],
                    "timeout_seconds": 30,
                },
            },),
            usage=Usage(input=1, output=1),
            stop_reason="tool_use",
            model="fake",
        ),
        _final_reply("completed after approval"),
    ])

    def fake_agent(_args, workbench, *, task_id, run_id):
        return Agent(
            adapter=FakeAdapter.from_handler(lambda _request: next(replies)),
            model="fake",
            tools=workbench.workspace_tools(task_id, run_id),
            session_path=workbench.claims_path_for_run(run_id),
        )

    monkeypatch.setattr("lipas.cli._workbench_agent", fake_agent)
    assert main([
        "task", "submit", str(workspace), "write queued note",
        "--home", str(home),
    ]) == 0
    capsys.readouterr()

    worker_args = [
        "task", "worker", "--once", "--home", str(home),
        "--sandbox", "local",
    ]
    assert main(worker_args) == 0
    waiting = capsys.readouterr().out
    approval = re.search(
        r"approval (approval_[a-f0-9]+)", waiting,
    ).group(1)  # type: ignore[union-attr]
    assert "status=waiting" in waiting

    assert main([
        "task", "approvals", "--json", "--home", str(home),
    ]) == 0
    inbox = capsys.readouterr().out
    assert approval in inbox
    assert '"state": "pending"' in inbox
    assert "queued\\n" not in inbox

    assert main([
        "task", "approve", approval, "--defer-resume",
        "--home", str(home), "--sandbox", "local",
    ]) == 0
    assert "queued for a worker" in capsys.readouterr().out

    assert main(worker_args) == 0
    completed = capsys.readouterr().out
    assert "status=completed" in completed
    assert not (workspace / "note.txt").exists()
    with Workbench(home) as workbench:
        task_id = workbench.list_tasks()[0].id
    assert main(["task", "apply", task_id, "--home", str(home)]) == 0
    capsys.readouterr()
    assert (workspace / "note.txt").read_text(encoding="utf-8") == "queued\n"
