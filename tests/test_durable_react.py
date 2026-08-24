"""End-to-end recovery windows for checkpointed ReAct execution."""
from __future__ import annotations

import asyncio
from pathlib import Path
import subprocess
import sys
import threading
import time

import pytest

from lipas import (
    Agent,
    DurablePhaseTimeout,
    DurableRecoveryRequired,
    ExecutionStateError,
    ExecutionStore,
    RunState,
    RunSuspended,
    tool,
    writes_require_approval,
)
from lipas.adapter import Reply, Usage
from lipas.behaviour import AgentState
from lipas.durable import DurableReActRunner
from lipas.exceptions import OrphanedEffectError
from lipas.effect import EffectKind, F_KIND, TAG_EFFECT_RESULT
from lipas.session import open_session
from lipas.serialization.codec import UnserializableClaim
from lipas.supervisor import (
    Policy,
    PolicyRule,
    TAG_GOAL_BLOCKED,
    TAG_SUPERVISOR_TERMINATE,
    TerminateAction,
)
from tests.fake_adapter import FakeAdapter


class SimulatedCrash(BaseException):
    """Models process death: normal exception cleanup must not settle the run."""


class SlowAdapter:
    name = "slow"

    async def estimate_cost(self, request):
        from decimal import Decimal
        from lipas.adapter import ResourceEstimate
        return ResourceEstimate(
            model=request.model, input_tokens=0,
            max_output_tokens=request.max_tokens, max_cost_usd=Decimal("0"),
        )

    async def stream(self, request):
        await asyncio.sleep(0.12)
        from lipas.adapter import Done
        yield Done(reply=_final_reply("slow but owned"))


def _reply_with_tool() -> Reply:
    return Reply(
        content=(
            {
                "type": "tool_use",
                "id": "tool_aaaaaaaaaaaa",
                "name": "write_note",
                "input": {"text": "saved"},
            },
        ),
        usage=Usage(input=2, output=1),
        stop_reason="tool_use",
        model="fake",
    )


def _reply_with_two_tools(tool_name: str) -> Reply:
    return Reply(
        content=(
            {
                "type": "tool_use",
                "id": "tool_first",
                "name": tool_name,
                "input": {"value": "first"},
            },
            {
                "type": "tool_use",
                "id": "tool_second",
                "name": tool_name,
                "input": {"value": "second"},
            },
        ),
        usage=Usage(input=2, output=1),
        stop_reason="tool_use",
        model="fake",
    )


def _final_reply(text: str = "done") -> Reply:
    return Reply(
        content=({"type": "text", "text": text},),
        usage=Usage(input=1, output=1),
        stop_reason="end_turn",
        model="fake",
    )


def _task_run(store: ExecutionStore, workspace):
    task = store.create_task("write and verify one note", workspace)
    return task, store.create_run(task.id)


def _reclaim(store: ExecutionStore, run_id: str):
    stale = store.get_run(run_id)
    assert stale is not None and stale.lease_expires is not None
    return store.claim_run(run_id, now=stale.lease_expires + 1)


def test_automatic_heartbeat_keeps_a_slow_model_phase_owned(tmp_path):
    with ExecutionStore(tmp_path / "execution.db") as store:
        _, run = _task_run(store, tmp_path)
        agent = Agent(
            adapter=SlowAdapter(), model="fake",
            session_path=tmp_path / "claims.db",
        )
        try:
            result = asyncio.run(agent.run_durable(
                "wait safely", execution_store=store, run_id=run.id,
                lease_seconds=0.06, heartbeat_interval_s=0.015,
            ))
        finally:
            agent.close()
        assert result.text == "slow but owned"
        settled = store.get_run(run.id)
        assert settled is not None and settled.state is RunState.COMPLETED
        assert settled.attempt == 1


def test_cancelling_runner_stops_its_heartbeat_task(tmp_path):
    async def scenario(agent, store, run_id):
        running = asyncio.create_task(agent.run_durable(
            "cancel worker",
            execution_store=store,
            run_id=run_id,
            lease_seconds=0.1,
            heartbeat_interval_s=0.02,
        ))
        await asyncio.sleep(0.04)
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running
        cancelled_at = store.get_run(run_id)
        assert cancelled_at is not None
        lease_expires = cancelled_at.lease_expires
        await asyncio.sleep(0.05)
        after = store.get_run(run_id)
        assert after is not None
        assert after.lease_expires == lease_expires

    with ExecutionStore(tmp_path / "execution.db") as store:
        _, run = _task_run(store, tmp_path)
        agent = Agent(
            adapter=SlowAdapter(), model="fake",
            session_path=tmp_path / "claims.db",
        )
        try:
            asyncio.run(scenario(agent, store, run.id))
        finally:
            agent.close()


def test_model_phase_timeout_is_typed_and_fails_the_run(tmp_path):
    with ExecutionStore(tmp_path / "execution.db") as store:
        _, run = _task_run(store, tmp_path)
        agent = Agent(
            adapter=SlowAdapter(), model="fake",
            session_path=tmp_path / "claims.db",
        )
        try:
            with pytest.raises(DurablePhaseTimeout) as raised:
                asyncio.run(agent.run_durable(
                    "time out", execution_store=store, run_id=run.id,
                    lease_seconds=1, heartbeat_interval_s=0.1,
                    phase_timeout_s=0.01,
                ))
        finally:
            agent.close()
        assert raised.value.phase == "model"
        failed = store.get_run(run.id)
        assert failed is not None and failed.state is RunState.FAILED
        assert failed.error is not None
        assert failed.error["recovery_required"] is True
        reopened = store.reopen_uncertain(
            run.id,
            evidence={
                "source": "test",
                "observation": "provider lookup confirmed no terminal outcome",
            },
        )
        assert reopened.state is RunState.PENDING


def test_automatic_heartbeat_continues_during_a_slow_sync_tool(tmp_path):
    calls: list[str] = []

    @tool(side_effect="idempotent_write")
    def write_note(text: str) -> str:
        """Block in ordinary sync Python longer than one lease."""
        time.sleep(0.12)
        calls.append(text)
        return text

    with ExecutionStore(tmp_path / "execution.db") as store:
        _, run = _task_run(store, tmp_path)
        agent = Agent(
            adapter=FakeAdapter.from_replies([_reply_with_tool(), _final_reply()]),
            model="fake", tools=[write_note],
            session_path=tmp_path / "claims.db",
        )
        try:
            result = asyncio.run(agent.run_durable(
                "write slowly", execution_store=store, run_id=run.id,
                lease_seconds=0.06, heartbeat_interval_s=0.015,
            ))
        finally:
            agent.close()
        assert result.text == "done"
        assert calls == ["saved"]
        settled = store.get_run(run.id)
        assert settled is not None and settled.state is RunState.COMPLETED


def test_durable_runner_executes_independent_reads_in_parallel(tmp_path):
    started: list[str] = []
    both_started = asyncio.Event()

    @tool(side_effect="read_only")
    async def read_value(value: str) -> str:
        """Wait until both independent reads have started."""
        started.append(value)
        if len(started) == 2:
            both_started.set()
        await asyncio.wait_for(both_started.wait(), timeout=0.5)
        return value

    with ExecutionStore(tmp_path / "execution.db") as store:
        _, run = _task_run(store, tmp_path)
        agent = Agent(
            adapter=FakeAdapter.from_replies([
                _reply_with_two_tools("read_value"),
                _final_reply(),
            ]),
            model="fake",
            tools=[read_value],
            session_path=tmp_path / "claims.db",
            max_parallel_tools=2,
        )
        try:
            result = asyncio.run(agent.run_durable(
                "read both", execution_store=store, run_id=run.id,
            ))
        finally:
            agent.close()

    assert result.text == "done"
    assert started == ["first", "second"]
    tool_results = result.state.messages[-2]["content"]
    assert [value["tool_use_id"] for value in tool_results] == [
        "tool_first", "tool_second",
    ]


def test_ordinary_agent_executes_independent_reads_in_parallel():
    started: list[str] = []
    both_started = asyncio.Event()

    @tool(side_effect="read_only")
    async def read_value(value: str) -> str:
        """Wait until both ordinary-Agent reads have started."""
        started.append(value)
        if len(started) == 2:
            both_started.set()
        await asyncio.wait_for(both_started.wait(), timeout=0.5)
        return value

    agent = Agent(
        adapter=FakeAdapter.from_replies([
            _reply_with_two_tools("read_value"),
            _final_reply(),
        ]),
        model="fake",
        tools=[read_value],
        max_parallel_tools=2,
    )
    try:
        result = asyncio.run(agent.run("read both"))
    finally:
        agent.close()

    assert result.text == "done"
    assert started == ["first", "second"]


def test_durable_runner_keeps_writes_serial(tmp_path):
    active = 0
    maximum_active = 0
    calls: list[str] = []

    @tool(side_effect="idempotent_write")
    async def write_value(value: str) -> str:
        """Record write concurrency without touching the filesystem."""
        nonlocal active, maximum_active
        active += 1
        maximum_active = max(maximum_active, active)
        await asyncio.sleep(0.01)
        calls.append(value)
        active -= 1
        return value

    with ExecutionStore(tmp_path / "execution.db") as store:
        _, run = _task_run(store, tmp_path)
        agent = Agent(
            adapter=FakeAdapter.from_replies([
                _reply_with_two_tools("write_value"),
                _final_reply(),
            ]),
            model="fake",
            tools=[write_value],
            session_path=tmp_path / "claims.db",
            max_parallel_tools=2,
        )
        try:
            result = asyncio.run(agent.run_durable(
                "write both", execution_store=store, run_id=run.id,
            ))
        finally:
            agent.close()

    assert result.text == "done"
    # Parallel read handlers may enter their worker threads in either order;
    # the durable contract is exactly-once execution and ordered tool-result
    # folding, not an incidental thread-scheduling order.
    assert sorted(calls) == ["first", "second"]
    assert maximum_active == 1


def test_durable_runner_keeps_budgeted_reads_serial(tmp_path):
    active = 0
    maximum_active = 0

    @tool(side_effect="read_only")
    async def read_value(value: str) -> str:
        """Measure concurrency for a budgeted read."""
        nonlocal active, maximum_active
        active += 1
        maximum_active = max(maximum_active, active)
        await asyncio.sleep(0.01)
        active -= 1
        return value

    with ExecutionStore(tmp_path / "execution.db") as store:
        _, run = _task_run(store, tmp_path)
        agent = Agent(
            adapter=FakeAdapter.from_replies([
                _reply_with_two_tools("read_value"),
                _final_reply(),
            ]),
            model="fake",
            tools=[read_value],
            session_path=tmp_path / "claims.db",
            budgets={"tool_calls": 10},
            max_parallel_tools=2,
        )
        try:
            result = asyncio.run(agent.run_durable(
                "read within budget", execution_store=store, run_id=run.id,
            ))
        finally:
            agent.close()

    assert result.text == "done"
    assert maximum_active == 1


def test_parallel_read_results_recover_after_checkpoint_crash(tmp_path):
    calls: list[str] = []

    @tool(side_effect="read_only")
    def read_value(value: str) -> str:
        """Return one value while recording actual executions."""
        calls.append(value)
        return value

    path = tmp_path / "execution.db"
    claims = tmp_path / "claims.db"
    store = ExecutionStore(path)
    _, pending = _task_run(store, tmp_path)
    claimed = store.claim_run(pending.id)
    first = Agent(
        adapter=FakeAdapter.from_replies([_reply_with_two_tools("read_value")]),
        model="fake",
        tools=[read_value],
        session_path=claims,
        max_parallel_tools=2,
    )
    original_save = store.save_checkpoint

    def crash_before_parallel_checkpoint(*args, **kwargs):
        if kwargs.get("phase") == "after_tool":
            raise SimulatedCrash()
        return original_save(*args, **kwargs)

    store.save_checkpoint = crash_before_parallel_checkpoint  # type: ignore[method-assign]
    try:
        with pytest.raises(SimulatedCrash):
            asyncio.run(DurableReActRunner(
                first.behaviour, store, claimed,
            ).run_to_completion(
                AgentState(messages=({"role": "user", "content": "read"},)),
            ))
    finally:
        store.save_checkpoint = original_save  # type: ignore[method-assign]
        first.close()

    assert sorted(calls) == ["first", "second"]
    replacement = _reclaim(store, pending.id)
    second = Agent(
        adapter=FakeAdapter.from_replies([_final_reply("recovered")]),
        model="fake",
        tools=[read_value],
        session_path=claims,
        max_parallel_tools=2,
    )
    try:
        result = asyncio.run(DurableReActRunner(
            second.behaviour, store, replacement,
        ).run_to_completion())
    finally:
        second.close()
        store.close()

    assert result.text == "recovered"
    assert calls == ["first", "second"]


def test_parallel_reads_checkpoint_before_write_approval_and_resume(tmp_path):
    reads: list[str] = []
    writes: list[str] = []
    both_started = asyncio.Event()

    @tool(side_effect="read_only")
    async def read_value(value: str) -> str:
        """Wait for both reads before returning."""
        reads.append(value)
        if len(reads) == 2:
            both_started.set()
        await asyncio.wait_for(both_started.wait(), timeout=0.5)
        return value

    @tool(side_effect="idempotent_write")
    def write_value(value: str) -> str:
        """Record the approved write."""
        writes.append(value)
        return value

    reply = Reply(
        content=(
            {
                "type": "tool_use", "id": "read_first",
                "name": "read_value", "input": {"value": "first"},
            },
            {
                "type": "tool_use", "id": "read_second",
                "name": "read_value", "input": {"value": "second"},
            },
            {
                "type": "tool_use", "id": "write_last",
                "name": "write_value", "input": {"value": "saved"},
            },
        ),
        usage=Usage(input=3, output=1),
        stop_reason="tool_use",
        model="fake",
    )
    with ExecutionStore(tmp_path / "execution.db") as store:
        _, run = _task_run(store, tmp_path)
        agent = Agent(
            adapter=FakeAdapter.from_replies([reply, _final_reply()]),
            model="fake",
            tools=[read_value, write_value],
            session_path=tmp_path / "claims.db",
            max_parallel_tools=2,
        )
        try:
            with pytest.raises(RunSuspended) as suspended:
                asyncio.run(agent.run_durable(
                    "read then write",
                    execution_store=store,
                    run_id=run.id,
                    approval_policy=writes_require_approval,
                ))

            checkpoint = store.get_checkpoint(run.id)
            assert checkpoint is not None
            assert checkpoint.state["next_tool_index"] == 2
            assert reads == ["first", "second"]
            assert writes == []

            store.resolve_interrupt(suspended.value.interrupt.id, allow=True)
            result = asyncio.run(agent.resume_durable(
                execution_store=store,
                run_id=run.id,
                approval_policy=writes_require_approval,
            ))
        finally:
            agent.close()

    assert result.text == "done"
    assert reads == ["first", "second"]
    assert writes == ["saved"]


def test_agent_run_durable_completes_and_persists_terminal_checkpoint(tmp_path):
    calls: list[str] = []

    @tool(side_effect="idempotent_write")
    def write_note(text: str) -> str:
        """Persist one note in the test sink."""
        calls.append(text)
        return text

    with ExecutionStore(tmp_path / "execution.db") as store:
        task, run = _task_run(store, tmp_path)
        agent = Agent(
            adapter=FakeAdapter.from_replies([_reply_with_tool(), _final_reply()]),
            model="fake",
            tools=[write_note],
            session_path=tmp_path / "claims.db",
        )
        try:
            result = asyncio.run(
                agent.run_durable(
                    "write the note",
                    execution_store=store,
                    run_id=run.id,
                ),
            )
        finally:
            agent.close()

        assert result.text == "done"
        assert calls == ["saved"]
        finished = store.get_run(run.id)
        assert finished is not None
        assert finished.state is RunState.COMPLETED
        assert store.get_task(task.id).state.value == "completed"  # type: ignore[union-attr]
        checkpoint = store.get_checkpoint(run.id)
        assert checkpoint is not None
        assert checkpoint.phase == "terminal"
        assert checkpoint.state["final_result"]["text"] == "done"


def test_completed_durable_run_restores_result_without_reclaim_or_live_call(tmp_path):
    execution_path = tmp_path / "execution.db"
    claims_path = tmp_path / "claims.db"
    with ExecutionStore(execution_path) as store:
        _, run = _task_run(store, tmp_path)
        first_agent = Agent(
            adapter=FakeAdapter.from_replies([_final_reply("persisted")]),
            model="fake",
            session_path=claims_path,
        )
        try:
            asyncio.run(first_agent.run_durable(
                "hello", execution_store=store, run_id=run.id,
            ))
        finally:
            first_agent.close()

        second_adapter = FakeAdapter.echoing()
        second_agent = Agent(
            adapter=second_adapter,
            model="fake",
            session_path=claims_path,
        )
        try:
            restored = asyncio.run(second_agent.resume_durable(
                execution_store=store, run_id=run.id,
            ))
        finally:
            second_agent.close()

        assert restored.text == "persisted"
        assert second_adapter.calls_made == 0


def test_durable_runner_observes_cancel_before_next_external_call(tmp_path):
    with ExecutionStore(tmp_path / "execution.db") as store:
        _, pending = _task_run(store, tmp_path)
        claimed = store.claim_run(pending.id)
        adapter = FakeAdapter.echoing()
        agent = Agent(
            adapter=adapter,
            model="fake",
            session_path=tmp_path / "claims.db",
        )
        original_save = store.save_checkpoint

        def cancel_after_before_llm(*args, **kwargs):
            checkpoint = original_save(*args, **kwargs)
            if kwargs.get("phase") == "before_llm":
                store.request_cancel(pending.id)
            return checkpoint

        store.save_checkpoint = cancel_after_before_llm  # type: ignore[method-assign]
        try:
            result = asyncio.run(
                DurableReActRunner(
                    agent.behaviour, store, claimed,
                ).run_to_completion(
                    AgentState(messages=({"role": "user", "content": "hello"},)),
                ),
            )
        finally:
            store.save_checkpoint = original_save  # type: ignore[method-assign]
            agent.close()

        assert result.stop_reason == "cancelled"
        assert adapter.calls_made == 0
        cancelled = store.get_run(pending.id)
        assert cancelled is not None and cancelled.state is RunState.CANCELLED
        terminal = store.get_checkpoint(pending.id)
        assert terminal is not None and terminal.phase == "terminal"
        assert terminal.state["final_result"]["stop_reason"] == "cancelled"


def test_cancel_during_unsettled_sync_tool_requires_recovery(tmp_path):
    started = threading.Event()

    @tool(side_effect="idempotent_write")
    def slow_write(text: str) -> str:
        """Remain in-flight long enough for logical cancellation to race it."""
        started.set()
        # Keep the thread in-flight across several RunContext polling
        # intervals. A very short sleep can legitimately settle before
        # cooperative cancellation is observed, which is a different
        # (ordinary cancelled) contract.
        time.sleep(0.5)
        return text

    tool_reply = Reply(
        content=({
            "type": "tool_use",
            "id": "tool_aaaaaaaaaaaa",
            "name": "slow_write",
            "input": {"text": "saved"},
        },),
        usage=Usage(input=2, output=1),
        stop_reason="tool_use",
        model="fake",
    )

    with ExecutionStore(tmp_path / "execution.db") as store:
        _, run = _task_run(store, tmp_path)
        agent = Agent(
            adapter=FakeAdapter.from_replies([tool_reply, _final_reply()]),
            model="fake",
            tools=[slow_write],
            session_path=tmp_path / "claims.db",
        )

        async def scenario() -> None:
            running = asyncio.create_task(agent.run_durable(
                "write", execution_store=store, run_id=run.id,
                lease_seconds=1.0, heartbeat_interval_s=0.1,
            ))
            await asyncio.to_thread(started.wait, 1.0)
            store.request_cancel(run.id)
            with pytest.raises(DurableRecoveryRequired):
                await running

        try:
            asyncio.run(scenario())
        finally:
            agent.close()
        failed = store.get_run(run.id)
        assert failed is not None and failed.state is RunState.FAILED
        assert failed.error is not None
        assert failed.error["recovery_required"] is True


def test_cancelled_terminal_run_can_restore_its_result(tmp_path):
    with ExecutionStore(tmp_path / "execution.db") as store:
        _, pending = _task_run(store, tmp_path)
        claimed = store.claim_run(pending.id)
        agent = Agent(
            adapter=FakeAdapter.echoing(),
            model="fake",
            session_path=tmp_path / "claims.db",
        )
        store.request_cancel(pending.id)
        try:
            cancelled = asyncio.run(
                DurableReActRunner(
                    agent.behaviour, store, claimed,
                ).run_to_completion(
                    AgentState(messages=({"role": "user", "content": "hello"},)),
                ),
            )
            restored = asyncio.run(agent.resume_durable(
                execution_store=store, run_id=pending.id,
            ))
        finally:
            agent.close()

        assert cancelled.stop_reason == "cancelled"
        assert restored == cancelled


def test_terminal_restore_rejects_a_different_claim_store(tmp_path):
    with ExecutionStore(tmp_path / "execution.db") as store:
        _, run = _task_run(store, tmp_path)
        first = Agent(
            adapter=FakeAdapter.from_replies([_final_reply("persisted")]),
            model="fake",
            session_path=tmp_path / "correct-claims.db",
        )
        try:
            asyncio.run(first.run_durable(
                "hello", execution_store=store, run_id=run.id,
            ))
        finally:
            first.close()

        wrong = Agent(
            adapter=FakeAdapter.echoing(),
            model="fake",
            session_path=tmp_path / "wrong-claims.db",
        )
        try:
            with pytest.raises(ExecutionStateError, match="different claim store"):
                asyncio.run(wrong.resume_durable(
                    execution_store=store, run_id=run.id,
                ))
        finally:
            wrong.close()


def test_initial_checkpoint_failure_does_not_leave_run_leased(tmp_path):
    with ExecutionStore(tmp_path / "execution.db") as store:
        _, run = _task_run(store, tmp_path)
        agent = Agent(
            adapter=FakeAdapter.echoing(),
            model="fake",
            session_path=tmp_path / "claims.db",
        )
        try:
            with pytest.raises(UnserializableClaim):
                asyncio.run(agent.run_durable(
                    [{"role": "user", "content": {"not", "serializable"}}],
                    execution_store=store,
                    run_id=run.id,
                ))
            failed = store.get_run(run.id)
        finally:
            agent.close()

        assert failed is not None and failed.state is RunState.FAILED
        assert failed.lease_token is None
        assert failed.error and failed.error["type"] == "UnserializableClaim"


def test_cancellation_wins_a_race_with_terminal_completion(tmp_path):
    with ExecutionStore(tmp_path / "execution.db") as store:
        _, run = _task_run(store, tmp_path)
        agent = Agent(
            adapter=FakeAdapter.from_replies([_final_reply("too late")]),
            model="fake",
            session_path=tmp_path / "claims.db",
        )
        original_complete = store.complete_run

        def cancel_immediately_before_completion(*args, **kwargs):
            store.request_cancel(run.id)
            return original_complete(*args, **kwargs)

        store.complete_run = cancel_immediately_before_completion  # type: ignore[method-assign]
        try:
            result = asyncio.run(agent.run_durable(
                "hello", execution_store=store, run_id=run.id,
            ))
            cancelled = store.get_run(run.id)
            terminal = store.get_checkpoint(run.id)
        finally:
            store.complete_run = original_complete  # type: ignore[method-assign]
            agent.close()

        assert result.stop_reason == "cancelled"
        assert cancelled is not None and cancelled.state is RunState.CANCELLED
        assert terminal is not None
        assert terminal.state["final_result"]["stop_reason"] == "cancelled"


def test_durable_supervision_terminates_and_settles_without_stranding_lease(
    tmp_path,
):
    policy = Policy.of(PolicyRule(
        "stop_after_reply",
        lambda _view, _ctx: TerminateAction("policy stop"),
    ))
    with ExecutionStore(tmp_path / "execution.db") as store:
        _, run = _task_run(store, tmp_path)
        agent = Agent(
            adapter=FakeAdapter.from_replies([_final_reply("ignored")]),
            model="fake",
            session_path=tmp_path / "claims.db",
            supervisor_policy=policy,
        )
        try:
            result = asyncio.run(agent.run_durable(
                "hello", execution_store=store, run_id=run.id,
            ))
            finished = store.get_run(run.id)
            terminations = agent.rowset.store.filter(
                tag=TAG_SUPERVISOR_TERMINATE,
            )
            blocked = agent.rowset.store.filter(tag=TAG_GOAL_BLOCKED)
        finally:
            agent.close()

        assert result.stop_reason == "supervisor_terminate"
        assert finished is not None and finished.state is RunState.FAILED
        assert finished.lease_token is None
        assert len(terminations) == len(blocked) == 1


def test_crash_after_durable_supervisor_tick_does_not_duplicate_claims(tmp_path):
    policy = Policy.of(PolicyRule(
        "stop_after_reply",
        lambda _view, _ctx: TerminateAction("policy stop"),
    ))
    store = ExecutionStore(tmp_path / "execution.db")
    _, pending = _task_run(store, tmp_path)
    first = Agent(
        adapter=FakeAdapter.from_replies([_final_reply("ignored")]),
        model="fake",
        session_path=tmp_path / "claims.db",
        supervisor_policy=policy,
    )
    original_save = store.save_checkpoint

    def crash_before_terminal_checkpoint(*args, **kwargs):
        if kwargs.get("phase") == "terminal":
            raise SimulatedCrash()
        return original_save(*args, **kwargs)

    store.save_checkpoint = crash_before_terminal_checkpoint  # type: ignore[method-assign]
    try:
        with pytest.raises(SimulatedCrash):
            asyncio.run(first.run_durable(
                "hello", execution_store=store, run_id=pending.id,
            ))
    finally:
        store.save_checkpoint = original_save  # type: ignore[method-assign]
        first.close()

    replacement = _reclaim(store, pending.id)
    second_adapter = FakeAdapter.echoing()
    second = Agent(
        adapter=second_adapter,
        model="fake",
        session_path=tmp_path / "claims.db",
        supervisor_policy=policy,
    )
    try:
        result = asyncio.run(DurableReActRunner(
            second.behaviour, store, replacement,
        ).run_to_completion())
        terminations = second.rowset.store.filter(
            tag=TAG_SUPERVISOR_TERMINATE,
        )
        blocked = second.rowset.store.filter(tag=TAG_GOAL_BLOCKED)
    finally:
        second.close()
        store.close()

    assert result.stop_reason == "supervisor_terminate"
    assert second_adapter.calls_made == 0
    assert len(terminations) == len(blocked) == 1
    assert blocked[0].fields["source_claim_seq"] == terminations[0].seq


def test_approval_suspends_then_resumes_the_same_tool_once(tmp_path):
    calls: list[str] = []
    approvals: list[str] = []

    @tool(side_effect="external_write")
    def write_note(text: str) -> str:
        """Persist one note in the test sink."""
        calls.append(text)
        return text

    def require_approval(tool, arguments):
        approvals.append(tool.name)
        return {"tool_name": tool.name, "arguments": dict(arguments)}

    with ExecutionStore(tmp_path / "execution.db") as store:
        _, run = _task_run(store, tmp_path)
        agent = Agent(
            adapter=FakeAdapter.from_replies([_reply_with_tool(), _final_reply()]),
            model="fake",
            tools=[write_note],
            session_path=tmp_path / "claims.db",
        )
        try:
            with pytest.raises(RunSuspended) as suspended:
                asyncio.run(
                    agent.run_durable(
                        "write the note",
                        execution_store=store,
                        run_id=run.id,
                        approval_policy=require_approval,
                    ),
                )

            waiting = store.get_run(run.id)
            assert waiting is not None and waiting.state is RunState.WAITING
            assert calls == []
            assert approvals == ["write_note"]

            store.resolve_interrupt(
                suspended.value.interrupt.id,
                allow=True,
                response={"approved_by": "test"},
            )
            result = asyncio.run(
                agent.resume_durable(
                    execution_store=store,
                    run_id=run.id,
                    approval_policy=require_approval,
                ),
            )
        finally:
            agent.close()

        assert result.text == "done"
        assert calls == ["saved"]
        assert approvals == ["write_note"]


def test_shared_claim_session_does_not_reuse_tool_result_across_runs(tmp_path):
    calls: list[str] = []

    @tool(side_effect="idempotent_write")
    def write_note(text: str) -> str:
        """Persist one note in the test sink."""
        calls.append(text)
        return text

    with ExecutionStore(tmp_path / "execution.db") as store:
        agent = Agent(
            adapter=FakeAdapter.from_replies([
                _reply_with_tool(),
                _final_reply("first"),
                _reply_with_tool(),
                _final_reply("second"),
            ]),
            model="fake",
            tools=[write_note],
            session_path=tmp_path / "claims.db",
        )
        try:
            _, first_run = _task_run(store, tmp_path)
            first = asyncio.run(
                agent.run_durable(
                    "first note", execution_store=store, run_id=first_run.id,
                ),
            )
            second_task = store.create_task("write a second note", tmp_path)
            second_run = store.create_run(second_task.id)
            second = asyncio.run(
                agent.run_durable(
                    "second note", execution_store=store, run_id=second_run.id,
                ),
            )
        finally:
            agent.close()

    assert first.text == "first"
    assert second.text == "second"
    assert calls == ["saved", "saved"]


def test_crash_after_tool_result_resumes_without_executing_tool_twice(tmp_path):
    calls: list[str] = []

    @tool(side_effect="idempotent_write")
    def write_note(text: str) -> str:
        """Persist one note in the test sink."""
        calls.append(text)
        return text

    path = tmp_path / "execution.db"
    claims = tmp_path / "claims.db"
    store = ExecutionStore(path)
    _, pending = _task_run(store, tmp_path)
    claimed = store.claim_run(pending.id)
    first_agent = Agent(
        adapter=FakeAdapter.from_replies([_reply_with_tool()]),
        model="fake",
        tools=[write_note],
        session_path=claims,
    )
    original_save = store.save_checkpoint

    def crash_before_after_tool_checkpoint(*args, **kwargs):
        if kwargs.get("phase") == "after_tool":
            raise SimulatedCrash()
        return original_save(*args, **kwargs)

    store.save_checkpoint = crash_before_after_tool_checkpoint  # type: ignore[method-assign]
    try:
        with pytest.raises(SimulatedCrash):
            asyncio.run(
                DurableReActRunner(first_agent.behaviour, store, claimed).run_to_completion(
                    initial=AgentState(
                        messages=tuple(first_agent._messages_from_prompt("write the note")),
                        metadata={"caused_by": pending.id},
                    ),
                ),
            )
    finally:
        store.save_checkpoint = original_save  # type: ignore[method-assign]
        first_agent.close()

    assert calls == ["saved"]
    replacement = _reclaim(store, pending.id)
    second_adapter = FakeAdapter.from_replies([_final_reply()])
    second_agent = Agent(
        adapter=second_adapter,
        model="fake",
        tools=[write_note],
        session_path=claims,
    )
    try:
        result = asyncio.run(
            DurableReActRunner(
                second_agent.behaviour, store, replacement,
            ).run_to_completion(),
        )
    finally:
        second_agent.close()
        store.close()

    assert result.text == "done"
    assert calls == ["saved"]
    assert second_adapter.calls_made == 1  # only the next iteration's LLM call


def test_crash_after_tool_error_restores_the_same_observation(tmp_path):
    calls: list[str] = []

    @tool(side_effect="idempotent_write")
    def write_note(text: str) -> str:
        """Fail after recording one attempted write."""
        calls.append(text)
        raise ValueError("disk full")

    path = tmp_path / "execution.db"
    claims = tmp_path / "claims.db"
    store = ExecutionStore(path)
    _, pending = _task_run(store, tmp_path)
    claimed = store.claim_run(pending.id)
    first_agent = Agent(
        adapter=FakeAdapter.from_replies([_reply_with_tool()]),
        model="fake",
        tools=[write_note],
        session_path=claims,
    )
    original_save = store.save_checkpoint

    def crash_before_after_tool_checkpoint(*args, **kwargs):
        if kwargs.get("phase") == "after_tool":
            raise SimulatedCrash()
        return original_save(*args, **kwargs)

    store.save_checkpoint = crash_before_after_tool_checkpoint  # type: ignore[method-assign]
    try:
        with pytest.raises(SimulatedCrash):
            asyncio.run(
                DurableReActRunner(
                    first_agent.behaviour, store, claimed,
                ).run_to_completion(
                    AgentState(messages=({"role": "user", "content": "write"},)),
                ),
            )
    finally:
        store.save_checkpoint = original_save  # type: ignore[method-assign]
        first_agent.close()

    def verify_observation(request):
        tool_result = request.messages[-1]["content"][0]
        assert tool_result["tool_use_id"] == "tool_aaaaaaaaaaaa"
        assert tool_result["content"] == "ValueError: disk full"
        assert tool_result["is_error"] is True
        return _final_reply("recovered")

    replacement = _reclaim(store, pending.id)
    second_adapter = FakeAdapter.from_handler(verify_observation)
    second_agent = Agent(
        adapter=second_adapter,
        model="fake",
        tools=[write_note],
        session_path=claims,
    )
    try:
        result = asyncio.run(
            DurableReActRunner(
                second_agent.behaviour, store, replacement,
            ).run_to_completion(),
        )
    finally:
        second_agent.close()
        store.close()

    assert result.text == "recovered"
    assert calls == ["saved"]


def test_crash_after_llm_result_reuses_recorded_reply_without_live_call(tmp_path):
    path = tmp_path / "execution.db"
    claims = tmp_path / "claims.db"
    store = ExecutionStore(path)
    _, pending = _task_run(store, tmp_path)
    claimed = store.claim_run(pending.id)
    first_agent = Agent(
        adapter=FakeAdapter.from_replies([_final_reply("recorded")]),
        model="fake",
        session_path=claims,
    )
    original_save = store.save_checkpoint

    def crash_before_after_llm_checkpoint(*args, **kwargs):
        if kwargs.get("phase") == "after_llm":
            raise SimulatedCrash()
        return original_save(*args, **kwargs)

    store.save_checkpoint = crash_before_after_llm_checkpoint  # type: ignore[method-assign]
    try:
        with pytest.raises(SimulatedCrash):
            asyncio.run(
                DurableReActRunner(first_agent.behaviour, store, claimed).run_to_completion(
                    AgentState(messages=({"role": "user", "content": "hello"},)),
                ),
            )
    finally:
        store.save_checkpoint = original_save  # type: ignore[method-assign]
        first_agent.close()

    replacement = _reclaim(store, pending.id)
    second_adapter = FakeAdapter.echoing()
    second_agent = Agent(
        adapter=second_adapter,
        model="fake",
        session_path=claims,
    )
    try:
        result = asyncio.run(
            DurableReActRunner(
                second_agent.behaviour, store, replacement,
            ).run_to_completion(),
        )
    finally:
        second_agent.close()
        store.close()

    assert result.text == "recorded"
    assert second_adapter.calls_made == 0


def test_resume_rejects_a_different_claim_store_before_live_work(tmp_path):
    store = ExecutionStore(tmp_path / "execution.db")
    _, pending = _task_run(store, tmp_path)
    claimed = store.claim_run(pending.id)
    first_agent = Agent(
        adapter=FakeAdapter.echoing(),
        model="fake",
        session_path=tmp_path / "correct-claims.db",
    )
    original_save = store.save_checkpoint

    def crash_after_before_llm_checkpoint(*args, **kwargs):
        checkpoint = original_save(*args, **kwargs)
        if kwargs.get("phase") == "before_llm":
            raise SimulatedCrash()
        return checkpoint

    store.save_checkpoint = crash_after_before_llm_checkpoint  # type: ignore[method-assign]
    try:
        with pytest.raises(SimulatedCrash):
            asyncio.run(
                DurableReActRunner(
                    first_agent.behaviour, store, claimed,
                ).run_to_completion(
                    AgentState(messages=({"role": "user", "content": "hello"},)),
                ),
            )
    finally:
        store.save_checkpoint = original_save  # type: ignore[method-assign]
        first_agent.close()

    replacement = _reclaim(store, pending.id)
    wrong_adapter = FakeAdapter.echoing()
    wrong_agent = Agent(
        adapter=wrong_adapter,
        model="fake",
        session_path=tmp_path / "wrong-claims.db",
    )
    try:
        with pytest.raises(ExecutionStateError, match="different claim store"):
            asyncio.run(
                DurableReActRunner(
                    wrong_agent.behaviour, store, replacement,
                ).run_to_completion(),
            )
    finally:
        wrong_agent.close()
        store.close()

    assert wrong_adapter.calls_made == 0


def test_crash_after_history_fold_deduplicates_iteration_claim(tmp_path):
    path = tmp_path / "execution.db"
    claims = tmp_path / "claims.db"
    store = ExecutionStore(path)
    _, pending = _task_run(store, tmp_path)
    claimed = store.claim_run(pending.id)
    first_agent = Agent(
        adapter=FakeAdapter.from_replies([_final_reply()]),
        model="fake",
        session_path=claims,
    )
    original_save = store.save_checkpoint

    def crash_before_terminal_checkpoint(*args, **kwargs):
        if kwargs.get("phase") == "terminal":
            raise SimulatedCrash()
        return original_save(*args, **kwargs)

    store.save_checkpoint = crash_before_terminal_checkpoint  # type: ignore[method-assign]
    try:
        with pytest.raises(SimulatedCrash):
            asyncio.run(
                DurableReActRunner(first_agent.behaviour, store, claimed).run_to_completion(
                    AgentState(messages=({"role": "user", "content": "hello"},)),
                ),
            )
    finally:
        store.save_checkpoint = original_save  # type: ignore[method-assign]
        first_agent.close()

    replacement = _reclaim(store, pending.id)
    second_agent = Agent(
        adapter=FakeAdapter.echoing(),
        model="fake",
        session_path=claims,
    )
    try:
        asyncio.run(
            DurableReActRunner(
                second_agent.behaviour, store, replacement,
            ).run_to_completion(),
        )
        observations = second_agent.rowset.store.filter(tag="observation")
    finally:
        second_agent.close()
        store.close()

    assert len(observations) == 1


def test_crash_after_terminal_checkpoint_resumes_only_final_settlement(tmp_path):
    path = tmp_path / "execution.db"
    claims = tmp_path / "claims.db"
    store = ExecutionStore(path)
    _, pending = _task_run(store, tmp_path)
    claimed = store.claim_run(pending.id)
    first_agent = Agent(
        adapter=FakeAdapter.from_replies([_final_reply("checkpointed")]),
        model="fake",
        session_path=claims,
    )
    original_complete = store.complete_run

    def crash_before_settlement(*args, **kwargs):
        raise SimulatedCrash()

    store.complete_run = crash_before_settlement  # type: ignore[method-assign]
    try:
        with pytest.raises(SimulatedCrash):
            asyncio.run(
                DurableReActRunner(
                    first_agent.behaviour, store, claimed,
                ).run_to_completion(
                    AgentState(messages=({"role": "user", "content": "hello"},)),
                ),
            )
    finally:
        store.complete_run = original_complete  # type: ignore[method-assign]
        first_agent.close()

    terminal = store.get_checkpoint(pending.id)
    assert terminal is not None and terminal.phase == "terminal"
    replacement = _reclaim(store, pending.id)
    second_adapter = FakeAdapter.echoing()
    second_agent = Agent(
        adapter=second_adapter,
        model="fake",
        session_path=claims,
    )
    try:
        result = asyncio.run(
            DurableReActRunner(
                second_agent.behaviour, store, replacement,
            ).run_to_completion(),
        )
    finally:
        second_agent.close()
        store.close()

    assert result.text == "checkpointed"
    assert second_adapter.calls_made == 0


def test_orphaned_llm_effect_is_visible_and_never_retried(tmp_path):
    class CrashingAdapter(FakeAdapter):
        def _next_reply(self, request):
            raise SimulatedCrash()

    path = tmp_path / "execution.db"
    claims = tmp_path / "claims.db"
    store = ExecutionStore(path)
    _, pending = _task_run(store, tmp_path)
    claimed = store.claim_run(pending.id)
    first_agent = Agent(
        adapter=CrashingAdapter(handler=lambda _: _final_reply()),
        model="fake",
        session_path=claims,
    )
    with pytest.raises(SimulatedCrash):
        asyncio.run(
            DurableReActRunner(first_agent.behaviour, store, claimed).run_to_completion(
                AgentState(messages=({"role": "user", "content": "hello"},)),
            ),
        )
    first_agent.close()

    replacement = _reclaim(store, pending.id)
    second_adapter = FakeAdapter.echoing()
    second_agent = Agent(
        adapter=second_adapter,
        model="fake",
        session_path=claims,
    )
    try:
        with pytest.raises(OrphanedEffectError):
            asyncio.run(
                DurableReActRunner(
                    second_agent.behaviour, store, replacement,
                ).run_to_completion(),
            )
        failed = store.get_run(pending.id)
        restored = asyncio.run(second_agent.resume_durable(
            execution_store=store,
            run_id=pending.id,
        ))
    finally:
        second_agent.close()
        store.close()

    assert second_adapter.calls_made == 0
    assert failed is not None and failed.state is RunState.FAILED
    assert failed.error and failed.error["type"] == "OrphanedEffectError"
    assert restored.stop_reason == "error"
    assert restored.error and restored.error["type"] == "OrphanedEffectError"


def test_forced_process_stop_restores_completed_write_without_repeating_it(tmp_path):
    """A SIGKILL after Effect result commit must not resubmit the write."""
    project_root = Path(__file__).resolve().parents[1]
    execution_path = tmp_path / "execution.db"
    with ExecutionStore(execution_path) as store:
        task = store.create_task("write once across process death", tmp_path)
        run = store.create_run(task.id, run_id="run-process-kill")

    crashed = subprocess.run(
        [
            sys.executable,
            "-m",
            "tests.durable_process_worker",
            "crash",
            str(tmp_path),
            run.id,
        ],
        cwd=project_root,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert crashed.returncode < 0, crashed.stderr
    assert (tmp_path / "write-attempts.log").read_text(encoding="utf-8").splitlines() == [
        "persisted once",
    ]

    claims = open_session(tmp_path / "claims.db")
    try:
        tool_results = [
            claim for claim in claims.store.filter(tag=TAG_EFFECT_RESULT)
            if claim.fields.get(F_KIND) == EffectKind.TOOL_CALL.value
        ]
        assert len(tool_results) == 1
    finally:
        claims.store.close()

    with ExecutionStore(execution_path) as store:
        stale = store.get_run(run.id)
        assert stale is not None and stale.lease_expires is not None
        delay = stale.lease_expires - time.time() + 0.02
    if delay > 0:
        time.sleep(delay)

    resumed = subprocess.run(
        [
            sys.executable,
            "-m",
            "tests.durable_process_worker",
            "resume",
            str(tmp_path),
            run.id,
        ],
        cwd=project_root,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert resumed.returncode == 0, resumed.stderr
    assert resumed.stdout.strip() == "recovered"
    assert (tmp_path / "write-attempts.log").read_text(encoding="utf-8").splitlines() == [
        "persisted once",
    ]
    with ExecutionStore(execution_path) as store:
        finished = store.get_run(run.id)
        assert finished is not None and finished.state is RunState.COMPLETED


@pytest.mark.parametrize("phase", ["before_llm", "after_llm", "after_tool", "terminal"])
def test_forced_process_stop_at_every_react_checkpoint_is_recoverable(tmp_path, phase):
    """Each model/tool phase either replays or executes the write exactly once."""
    project_root = Path(__file__).resolve().parents[1]
    execution_path = tmp_path / "execution.db"
    with ExecutionStore(execution_path) as store:
        task = store.create_task(f"phase {phase}", tmp_path)
        run = store.create_run(task.id, run_id=f"run-{phase}")

    crashed = subprocess.run(
        [
            sys.executable, "-m", "tests.durable_process_worker", "crash",
            str(tmp_path), run.id, phase,
        ], cwd=project_root, capture_output=True, text=True, timeout=10, check=False,
    )
    assert crashed.returncode < 0, (phase, crashed.stderr)

    with ExecutionStore(execution_path) as store:
        stale = store.get_run(run.id)
        assert stale is not None and stale.lease_expires is not None
        delay = stale.lease_expires - time.time() + 0.02
    if delay > 0:
        time.sleep(delay)

    resumed = subprocess.run(
        [
            sys.executable, "-m", "tests.durable_process_worker", "resume",
            str(tmp_path), run.id, phase,
        ], cwd=project_root, capture_output=True, text=True, timeout=10, check=False,
    )
    assert resumed.returncode == 0, (phase, resumed.stderr)
    assert resumed.stdout.strip() == "recovered"
    assert (tmp_path / "write-attempts.log").read_text(encoding="utf-8").splitlines() == [
        "persisted once",
    ]


def test_denied_approval_restores_cancelled_result_without_live_work(tmp_path):
    calls: list[str] = []

    @tool(side_effect="external_write")
    def write_note(text: str) -> str:
        """A write that must never run after approval is denied."""
        calls.append(text)
        return text

    adapter = FakeAdapter.from_replies([_reply_with_tool()])
    with ExecutionStore(tmp_path / "execution.db") as store:
        _, run = _task_run(store, tmp_path)
        with Agent(
            adapter=adapter,
            model="fake",
            tools=[write_note],
            session_path=tmp_path / "claims.db",
        ) as agent:
            with pytest.raises(RunSuspended) as suspended:
                asyncio.run(agent.run_durable(
                    "write the note",
                    execution_store=store,
                    run_id=run.id,
                    approval_policy=writes_require_approval,
                ))
            store.resolve_interrupt(
                suspended.value.interrupt.id,
                allow=False,
                response={"reason": "operator_denied"},
            )
            result = asyncio.run(agent.resume_durable(
                execution_store=store,
                run_id=run.id,
                approval_policy=writes_require_approval,
            ))

    assert result.stop_reason == "cancelled"
    assert calls == []
    assert adapter.calls_made == 1
