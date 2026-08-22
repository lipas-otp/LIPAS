"""Unified public runtime contracts inspired by the LIPA product fork."""
from __future__ import annotations

import asyncio
from decimal import Decimal
from types import SimpleNamespace

import pytest

from lipas import (
    Agent,
    ExecutionStore,
    LIPASRuntime,
    ModelCapabilities,
    ModelCapabilityError,
    ModelRegistry,
    ModelRequirements,
    Recommendation,
    RunContext,
    RunSuspended,
    SQLiteSessionStore,
    current_run_context,
    tool,
)
from lipas.adapter import Delta, Done, Reply, ResourceEstimate, Usage
from lipas.calculus import Claim
from lipas.supervisor import TAG_SUPERVISOR_TERMINATE


class ScriptedAdapter:
    name = "scripted"

    def __init__(self, replies, *, delay_s: float = 0.0):
        self.replies = list(replies)
        self.delay_s = delay_s
        self.seen = []

    async def estimate_cost(self, request):
        return ResourceEstimate(request.model, 1, request.max_tokens, Decimal("0"))

    async def stream(self, request):
        self.seen.append(request)
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        reply = self.replies.pop(0)
        for block in reply.content:
            if isinstance(block, dict) and block.get("type") == "text":
                yield Delta(index=0, text=str(block.get("text", "")))
        yield Done(reply)


def final(text: str = "done") -> Reply:
    return Reply(
        content=({"type": "text", "text": text},),
        usage=Usage(input=1, output=1),
        stop_reason="end_turn",
        model="scripted",
    )


def ask_input() -> Reply:
    return Reply(
        content=({
            "type": "tool_use",
            "id": "input-1",
            "name": "ask_operator",
            "input": {"question": "How long?"},
        },),
        usage=Usage(input=1, output=1),
        stop_reason="tool_use",
        model="scripted",
    )


def test_model_requirements_fail_closed_on_unknown_or_false_capability():
    registry = ModelRegistry([
        ModelCapabilities(
            provider="scripted", model="*", streaming=False,
        ),
    ])
    with pytest.raises(ModelCapabilityError) as raised:
        Agent(
            adapter=ScriptedAdapter([final()]),
            model="m",
            model_registry=registry,
            model_requirements=ModelRequirements(streaming=True),
        )
    assert raised.value.report.issues[0].capability == "streaming"


def test_session_handle_uses_one_identity_and_cooperative_context():
    async def scenario():
        agent = Agent(
            adapter=ScriptedAdapter([final("late")], delay_s=1), model="m",
        )
        handle = agent.session(session_id="demo").start("cancel")
        await asyncio.sleep(0.01)
        assert handle.cancel()
        result = await handle.result()
        events = [event async for event in handle.events()]
        agent.close()
        return handle, result, events

    handle, result, events = asyncio.run(scenario())
    assert result.stop_reason == "cancelled"
    assert result.metadata["run_id"] == handle.id
    assert all(event.run_id == handle.id for event in events)
    assert events[-1].type == "run_cancelled"


def test_run_context_is_visible_in_sync_tool_without_model_schema_parameter():
    seen = []

    @tool(side_effect="read_only")
    def inspect_context() -> str:
        """Read the host context rather than a model-visible argument."""
        context = current_run_context(required=True)
        seen.append(context.run_id)
        return context.run_id

    tool_reply = Reply(
        content=({
            "type": "tool_use", "id": "tool-1",
            "name": "inspect_context", "input": {},
        },),
        usage=Usage(input=1, output=1),
        stop_reason="tool_use",
        model="scripted",
    )
    context = RunContext.create(run_id="run_visible")
    agent = Agent(
        adapter=ScriptedAdapter([tool_reply, final()]),
        model="m",
        tools=[inspect_context],
    )
    result = asyncio.run(agent.run("inspect", context=context))
    agent.close()
    assert result.text == "done"
    assert seen == ["run_visible"]


def test_absolute_deadline_is_not_reset_between_react_phases():
    @tool(side_effect="read_only")
    async def slow_read() -> str:
        """Consume the remainder of the run-wide deadline."""
        await asyncio.sleep(0.04)
        return "late"

    tool_reply = Reply(
        content=({
            "type": "tool_use", "id": "slow-1",
            "name": "slow_read", "input": {},
        },),
        usage=Usage(input=1, output=1),
        stop_reason="tool_use",
        model="scripted",
    )
    agent = Agent(
        adapter=ScriptedAdapter([tool_reply, final()], delay_s=0.025),
        model="m",
        tools=[slow_read],
    )
    result = asyncio.run(agent.run("slow", timeout_s=0.05))
    agent.close()
    assert result.stop_reason == "error"
    assert result.error["type"] == "deadline_exceeded"


def test_observer_is_advisory_by_default_and_recorded():
    class StopObserver:
        async def observe(self, snapshot, context):
            return Recommendation(kind="terminate", reason="review")

    agent = Agent(
        adapter=ScriptedAdapter([final("kept")]),
        model="m",
        observers=[StopObserver()],
    )
    result = asyncio.run(agent.run("hello"))
    assert result.text == "kept"
    assert result.stop_reason == "natural_stop"
    assert len(agent.rowset.store.filter(tag="observer_recommendation")) == 1
    agent.close()


def test_input_interrupt_cannot_execute_tool_or_grant_later_approval(tmp_path):
    calls = []

    @tool(side_effect="pure")
    def ask_operator(question: str) -> str:
        """This body is replaced by operator input in durable mode."""
        calls.append(question)
        return "unexpected"

    def input_policy(tool_value, arguments):
        if tool_value.name == "ask_operator":
            return {"question": arguments["question"]}
        return None

    agent = Agent(
        adapter=ScriptedAdapter([ask_input(), final("resumed")]),
        model="m",
        tools=[ask_operator],
        session_path=tmp_path / "claims.db",
    )
    with ExecutionStore(tmp_path / "execution.db") as store:
        task = store.create_task("ask", tmp_path)
        run = store.create_run(task.id)
        with pytest.raises(RunSuspended) as suspended:
            asyncio.run(agent.run_durable(
                "ask", execution_store=store, run_id=run.id,
                input_policy=input_policy,
            ))
        assert suspended.value.interrupt.kind == "input"
        store.resolve_interrupt(
            suspended.value.interrupt.id,
            allow=True,
            response={"answer": "300"},
        )
        result = asyncio.run(agent.resume_durable(
            execution_store=store,
            run_id=run.id,
            input_policy=input_policy,
        ))
        events = store.agent_events(run.id)
    agent.close()
    assert result.text == "resumed"
    assert calls == []
    assert events[0].type == "run_started"
    assert events[-1].type == "run_completed"


def test_durable_deadline_returns_same_logical_result_and_settles_run(tmp_path):
    agent = Agent(
        adapter=ScriptedAdapter([final("late")], delay_s=1),
        model="m",
        session_path=tmp_path / "claims.db",
    )
    with ExecutionStore(tmp_path / "execution.db") as store:
        task = store.create_task("deadline", tmp_path)
        run = store.create_run(task.id)
        result = asyncio.run(agent.run_durable(
            "wait",
            execution_store=store,
            run_id=run.id,
            timeout_s=0.01,
        ))
        settled = store.get_run(run.id)
        events = store.agent_events(run.id)
    agent.close()
    assert result.stop_reason == "error"
    assert result.error["type"] == "deadline_exceeded"
    assert settled.state.value == "failed"
    assert events[-1].type == "run_completed"


def test_durable_reconnect_sink_failure_does_not_hide_settled_result(tmp_path):
    agent = Agent(
        adapter=ScriptedAdapter([final("persisted")]),
        model="m",
        session_path=tmp_path / "claims.db",
    )
    delivered = []
    with ExecutionStore(tmp_path / "execution.db") as store:
        task = store.create_task("events", tmp_path)
        run = store.create_run(task.id)
        result = asyncio.run(agent.run_durable(
            "complete", execution_store=store, run_id=run.id,
        ))

        def broken_sink(event):
            delivered.append(event.type)
            raise RuntimeError("UI disconnected")

        restored = asyncio.run(agent.resume_durable(
            execution_store=store,
            run_id=run.id,
            event_sink=broken_sink,
            event_cursor=0,
        ))
    agent.close()
    assert result.text == "persisted"
    assert restored.text == "persisted"
    assert delivered == ["run_started"]


def test_runtime_composition_root_owns_stores_and_audit(tmp_path):
    with LIPASRuntime.open(tmp_path / "state", sandbox="local") as runtime:
        assert runtime.execution is runtime.workbench.execution
        assert runtime.claims is not None
        assert runtime.operations is not None
        assert runtime.artifacts is not None
        assert runtime.audit().healthy
        _, run = runtime.workbench.create_task("answer", tmp_path)
        agent = runtime.agent_for_run(
            run.id,
            adapter=ScriptedAdapter([final("composed")]),
            model="m",
        )
        try:
            result = asyncio.run(runtime.run_durable(
                agent, "hello", run_id=run.id,
            ))
        finally:
            agent.close()
        assert result.text == "composed"


def test_runtime_audit_lints_registered_run_evidence(tmp_path):
    with LIPASRuntime.open(tmp_path / "state", sandbox="local") as runtime:
        _, run = runtime.workbench.create_task("lint", tmp_path)
        run_claims = runtime.claims_for_run(run.id)
        try:
            run_claims.fold(Claim(
                tag=TAG_SUPERVISOR_TERMINATE,
                fields={},
                source="test",
            ))
        finally:
            run_claims.store.close()

        report = runtime.audit()
        assert not report.healthy
        assert len(report.claim_issues) == 1
        assert report.claim_issues[0].scope == f"run:{run.id}"
        assert "goal_blocked_pairing" in str(report.claim_issues[0].issue)


def test_runtime_repair_restores_execution_audit_to_registered_run_tape(tmp_path):
    with LIPASRuntime.open(tmp_path / "state", sandbox="local") as runtime:
        _, run = runtime.workbench.create_task("repair", tmp_path)
        empty_claims = runtime.claims_for_run(run.id)
        empty_claims.store.close()

        report = runtime.audit(repair=True)
        assert report.execution_events_repaired >= 1
        repaired = runtime.claims_for_run(run.id)
        try:
            execution_claims = [
                claim for claim in repaired.store
                if claim.source == "execution.store"
            ]
            assert execution_claims
            assert all(
                claim.fields.get("run_id") == run.id
                for claim in execution_claims
            )
        finally:
            repaired.store.close()


def test_runtime_serializes_concurrent_durable_convenience_calls(tmp_path):
    with LIPASRuntime.open(tmp_path / "state", sandbox="local") as runtime:
        _, first_run = runtime.workbench.create_task("first", tmp_path)
        _, second_run = runtime.workbench.create_task("second", tmp_path)
        first_agent = runtime.agent_for_run(
            first_run.id,
            adapter=ScriptedAdapter([final("first")], delay_s=0.02),
            model="m",
        )
        second_agent = runtime.agent_for_run(
            second_run.id,
            adapter=ScriptedAdapter([final("second")], delay_s=0.02),
            model="m",
        )

        async def scenario():
            return await asyncio.gather(
                runtime.run_durable(
                    first_agent, "first", run_id=first_run.id,
                ),
                runtime.run_durable(
                    second_agent, "second", run_id=second_run.id,
                ),
            )

        try:
            results = asyncio.run(scenario())
        finally:
            first_agent.close()
            second_agent.close()
        assert [result.text for result in results] == ["first", "second"]


def test_runtime_preserves_execution_error_when_reattach_also_fails(
    tmp_path, monkeypatch,
):
    class ExecutionFailure(RuntimeError):
        pass

    class ReattachFailure(RuntimeError):
        pass

    run_claims = None
    with LIPASRuntime.open(tmp_path / "state", sandbox="local") as runtime:
        _, run = runtime.workbench.create_task("fail", tmp_path)
        run_claims = runtime.claims_for_run(run.id)

        class FailingAgent:
            rowset = run_claims

            async def run_durable(self, *_args, **_kwargs):
                raise ExecutionFailure("execution failed")

        attach_calls = 0
        global_attach_calls = 0

        original_attach = runtime.workbench.attach_rowset

        def recording_attach(rowset, *, run_id=None):
            nonlocal attach_calls
            attach_calls += 1
            return original_attach(rowset, run_id=run_id)

        def flaky_global_attach(_rowset):
            nonlocal global_attach_calls
            global_attach_calls += 1
            raise ReattachFailure("reattach failed")

        monkeypatch.setattr(runtime.workbench, "attach_rowset", recording_attach)
        monkeypatch.setattr(
            runtime.workbench, "attach_global_rowset", flaky_global_attach,
        )
        with pytest.raises(ExecutionFailure, match="execution failed"):
            asyncio.run(runtime.run_durable(
                FailingAgent(), "hello", run_id=run.id,
            ))
        assert attach_calls == 1
        assert global_attach_calls == 1
    assert run_claims is not None
    run_claims.store.close()


def test_runtime_close_attempts_every_resource_and_raises_first_error():
    calls = []

    class Resource:
        def __init__(self, name, error=None):
            self.name = name
            self.error = error

        def close(self):
            calls.append(self.name)
            if self.error is not None:
                raise self.error

    first = RuntimeError("sessions failed")
    runtime = object.__new__(LIPASRuntime)
    runtime._closed = False
    runtime.sessions = Resource("sessions", first)
    runtime.handoffs = Resource("handoffs", RuntimeError("handoffs failed"))
    runtime.operations = Resource("operations")
    runtime.workbench = Resource("workbench")
    runtime.claims = SimpleNamespace(store=Resource("claims"))
    runtime._workspace_lease = Resource("lease")

    with pytest.raises(RuntimeError) as raised:
        runtime.close()
    assert raised.value is first
    assert calls == [
        "sessions", "handoffs", "operations", "workbench", "claims", "lease",
    ]
    assert runtime._closed


def test_runtime_init_preserves_constructor_error_and_closes_prior_resources(
    tmp_path, monkeypatch,
):
    from lipas import runtime as runtime_module

    calls = []

    class Resource:
        def __init__(self, name, *, fail_close=False):
            self.name = name
            self.fail_close = fail_close

        def close(self):
            calls.append(self.name)
            if self.fail_close:
                raise RuntimeError(f"{self.name} close failed")

    class Storage:
        def __init__(self, home):
            self.home = home

        def require_current(self, *, create=False):
            assert create
            return tmp_path / "workspace.db"

        def inspect(self):
            return SimpleNamespace(state="current")

        def acquire_runtime_lease(self, *, exclusive=False):
            assert not exclusive
            return Resource("lease")

    class ConstructorFailure(RuntimeError):
        pass

    monkeypatch.setattr(runtime_module, "WorkspaceStorage", Storage)
    monkeypatch.setattr(
        runtime_module, "open_session",
        lambda _path: SimpleNamespace(store=Resource("claims")),
    )
    monkeypatch.setattr(
        runtime_module, "Workbench",
        lambda *_args, **_kwargs: Resource("workbench"),
    )
    monkeypatch.setattr(
        runtime_module, "OperationJournal",
        lambda *_args, **_kwargs: Resource("operations"),
    )
    monkeypatch.setattr(
        runtime_module, "Mailbox",
        lambda *_args, **_kwargs: Resource("handoffs", fail_close=True),
    )

    def fail_sessions(*_args, **_kwargs):
        raise ConstructorFailure("sessions construction failed")

    monkeypatch.setattr(runtime_module, "SQLiteSessionStore", fail_sessions)
    with pytest.raises(ConstructorFailure, match="sessions construction failed"):
        LIPASRuntime(tmp_path / "state", sandbox="local")
    assert calls == ["handoffs", "operations", "workbench", "claims", "lease"]


def test_agent_init_preserves_composition_error_and_closes_owned_stores(
    monkeypatch,
):
    from lipas import agent as agent_module

    calls = []

    class Resource:
        def __init__(self, name, *, fail_close=False):
            self.name = name
            self.fail_close = fail_close

        def close(self):
            calls.append(self.name)
            if self.fail_close:
                raise RuntimeError(f"{self.name} close failed")

    monkeypatch.setattr(
        agent_module, "open_session",
        lambda *_args, **_kwargs: SimpleNamespace(store=Resource("claims")),
    )
    monkeypatch.setattr(
        agent_module, "SQLiteSessionStore",
        lambda *_args, **_kwargs: Resource("conversations", fail_close=True),
    )

    class CompositionFailure(RuntimeError):
        pass

    def fail_harness(*_args, **_kwargs):
        raise CompositionFailure("harness composition failed")

    monkeypatch.setattr(agent_module, "LLMHarness", fail_harness)
    with pytest.raises(CompositionFailure, match="harness composition failed"):
        Agent(
            adapter=ScriptedAdapter([final()]),
            model="m",
            session_path="claims.db",
        )
    assert calls == ["conversations", "claims"]


def test_sqlite_session_store_round_trip(tmp_path):
    store = SQLiteSessionStore(tmp_path / "conversation.db")
    agent = Agent(
        adapter=ScriptedAdapter([final("one")]),
        model="m",
        session_store=store,
    )
    session = agent.session(session_id="user")
    assert asyncio.run(session.run("hello")).text == "one"
    assert session.version == 1
    agent.close()
    store.close()
