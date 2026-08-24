"""ExecutionStore-backed multi-Agent coordination contracts."""
from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

import pytest

from lipas import (
    Agent,
    AgentCoordinator,
    AgentEventType,
    CoordinationBusy,
    CoordinationBudgetExceeded,
    CoordinationCapabilityDenied,
    CoordinationFailed,
    CoordinationIdentityConflict,
    CoordinationRecoveryRequired,
    CapabilityPolicy,
    SharedBudgetPolicy,
    CoordinationResultError,
    HandoffEnvelope,
    HandoffExecutionError,
    LIPASRuntime,
    MemberInfo,
    RunCancelled,
    RunContext,
    RunDeadlineExceeded,
    RunSuspended,
    Transfer,
    current_run_context,
    tool,
    writes_require_approval,
)
from lipas.adapter import Reply, Usage
from lipas.behaviour import AgentState, FinalResult
from tests.fake_adapter import FakeAdapter


def _run(awaitable: Any) -> Any:
    return asyncio.run(awaitable)


def _request_fingerprint(envelope: HandoffEnvelope, *, version: str = "1") -> str:
    request = {
        "version": 1,
        "id": envelope.id,
        "coordination_id": envelope.coordination_id,
        "sender": envelope.sender,
        "recipient": envelope.recipient,
        "payload": envelope.payload,
        "sequence": envelope.sequence,
        "parent_id": envelope.parent_id,
        "metadata": dict(envelope.metadata),
        "member": {
            "name": envelope.recipient,
            "version": version,
            "capabilities": [],
        },
    }
    encoded = json.dumps(
        request,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _abandon_once(
    coordinator: AgentCoordinator,
    envelope: HandoffEnvelope,
) -> None:
    """Model a process crash after claim and before member invocation."""
    member = coordinator._member(envelope.recipient)
    run, claimed = coordinator._claim_handoff(
        envelope,
        member=member,
        fingerprint=_request_fingerprint(envelope),
    )
    assert claimed and run.attempt == 1


def test_handoff_envelope_has_stable_identity_and_validates_fields():
    first = HandoffEnvelope.create(
        coordination_id="work-1",
        sender="planner",
        recipient="writer",
        payload={"topic": "SQLite"},
        sequence=2,
        parent_id="previous",
    )
    second = HandoffEnvelope.create(
        coordination_id="work-1",
        sender="planner",
        recipient="writer",
        payload={"topic": "changed payload"},
        sequence=2,
        parent_id="previous",
    )

    assert first.id == second.id
    with pytest.raises(ValueError, match="recipient"):
        HandoffEnvelope.create(
            coordination_id="work-1",
            sender="planner",
            recipient="",
            payload=None,
        )
    with pytest.raises(CoordinationResultError, match="unsupported object"):
        HandoffEnvelope.create(
            coordination_id="work-1",
            sender="planner",
            recipient="writer",
            payload=object(),
        )
    with pytest.raises(ValueError, match="trimmed"):
        MemberInfo(" writer ")
    cyclic: list[Any] = []
    cyclic.append(cyclic)
    with pytest.raises(CoordinationResultError, match="reference cycle"):
        HandoffEnvelope.create(
            coordination_id="work-1",
            sender="planner",
            recipient="writer",
            payload=cyclic,
        )


def test_handoff_replays_terminal_result_without_reinvoking_member(tmp_path: Path):
    async def scenario() -> None:
        calls = 0

        async def writer(payload: Any) -> dict[str, Any]:
            nonlocal calls
            calls += 1
            return {"received": payload, "calls": calls}

        with AgentCoordinator.open(tmp_path / "coord.db") as coordinator:
            coordinator.add("writer", writer)
            envelope = HandoffEnvelope.create(
                coordination_id="replay",
                sender="user",
                recipient="writer",
                payload={"text": "hello"},
            )
            first = await coordinator.dispatch(envelope)
            replay = await coordinator.dispatch(envelope)

            assert calls == 1
            assert first.value == replay.value
            assert first.replayed is False
            assert replay.replayed is True
            assert replay.attempt == 1
            assert [
                event.type
                for event in coordinator.execution.agent_events(first.run_id)
            ] == [
                AgentEventType.HANDOFF_STARTED,
                AgentEventType.HANDOFF_COMPLETED,
            ]

    _run(scenario())


def test_handoff_identity_reuse_with_different_input_fails_closed(tmp_path: Path):
    async def scenario() -> None:
        async def member(payload: Any) -> Any:
            return payload

        with AgentCoordinator.open(tmp_path / "coord.db") as coordinator:
            coordinator.add("member", member)
            await coordinator.handoff(
                "member", {"version": 1},
                coordination_id="identity",
                handoff_id="fixed-handoff",
            )
            with pytest.raises(CoordinationIdentityConflict):
                await coordinator.handoff(
                    "member", {"version": 2},
                    coordination_id="identity",
                    handoff_id="fixed-handoff",
                )

    _run(scenario())


def test_member_contract_version_is_part_of_durable_identity(tmp_path: Path):
    async def scenario() -> None:
        async def member(payload: Any) -> Any:
            return payload

        database = tmp_path / "coord.db"
        envelope = HandoffEnvelope.create(
            coordination_id="member-version",
            sender="user",
            recipient="member",
            payload="work",
        )
        with AgentCoordinator.open(database) as first:
            first.add("member", member, version="2026-08-23")
            await first.dispatch(envelope)
        with AgentCoordinator.open(database) as upgraded:
            upgraded.add("member", member, version="2026-09-01")
            with pytest.raises(CoordinationIdentityConflict):
                await upgraded.dispatch(envelope)

    _run(scenario())


def test_concurrent_duplicate_has_one_owner_and_does_not_mutate_input(
    tmp_path: Path,
):
    async def scenario() -> None:
        started = asyncio.Event()
        release = asyncio.Event()
        calls = 0

        async def member(payload: dict[str, Any]) -> str:
            nonlocal calls
            calls += 1
            payload["member-only"] = True
            started.set()
            await release.wait()
            return "done"

        original = {"input": [1, 2]}
        with AgentCoordinator.open(tmp_path / "coord.db") as coordinator:
            coordinator.add("member", member)
            envelope = HandoffEnvelope.create(
                coordination_id="race",
                sender="user",
                recipient="member",
                payload=original,
            )
            first = asyncio.create_task(coordinator.dispatch(envelope))
            await started.wait()
            with pytest.raises(CoordinationBusy):
                await coordinator.dispatch(envelope)
            release.set()
            assert (await first).value == "done"
            assert calls == 1
            assert original == {"input": [1, 2]}
            assert envelope.payload == {"input": [1, 2]}

    _run(scenario())


def test_expired_handoff_requires_explicit_redelivery_safety(tmp_path: Path):
    async def scenario() -> None:
        async def member(payload: Any) -> Any:
            return payload

        database = tmp_path / "coord.db"
        with AgentCoordinator.open(
            database, lease_seconds=0.02,
        ) as first:
            first.add("member", member)
            envelope = HandoffEnvelope.create(
                coordination_id="expired",
                sender="user",
                recipient="member",
                payload="work",
            )
            _abandon_once(first, envelope)
            await asyncio.sleep(0.04)
            with pytest.raises(CoordinationRecoveryRequired):
                await first.dispatch(envelope)

        with AgentCoordinator.open(
            database, lease_seconds=0.1,
        ) as recovery:
            recovery.add("member", member, redelivery_safe=True)
            outcome = await recovery.dispatch(envelope)
            assert outcome.value == "work"
            assert outcome.attempt == 2

    _run(scenario())


def test_shared_budget_and_capability_policy_are_durable_and_fail_closed(
    tmp_path: Path,
):
    async def scenario() -> None:
        calls = 0

        async def member(payload: Any) -> Any:
            nonlocal calls
            calls += 1
            return payload

        with AgentCoordinator.open(
            tmp_path / "coord.db",
            budget_policy=SharedBudgetPolicy({"handoffs": 1}),
            capability_policy=CapabilityPolicy(
                grants={"writer": {"notes.write"}},
            ),
        ) as coordinator:
            coordinator.add("writer", member, capabilities=["notes.write"])
            first = await coordinator.handoff(
                "writer", "first", coordination_id="shared-policy",
                handoff_id="shared-first",
            )
            assert first.value == "first"
            with pytest.raises(CoordinationBudgetExceeded):
                await coordinator.handoff(
                    "writer", "second", coordination_id="shared-policy",
                    handoff_id="shared-second",
                )
            snapshot = coordinator.budget_snapshot()
            assert snapshot is not None
            assert snapshot["spent"] == {"handoffs": 1.0}
            assert calls == 1

        with AgentCoordinator.open(
            tmp_path / "capability.db",
            capability_policy=CapabilityPolicy(grants={"writer": {"notes.read"}}),
        ) as denied:
            with pytest.raises(CoordinationCapabilityDenied):
                denied.add("writer", member, capabilities=["notes.write"])

    _run(scenario())


def test_aggregate_event_handle_reconnects_across_handoffs(tmp_path: Path):
    async def scenario() -> None:
        async def member(payload: Any) -> dict[str, Any]:
            return {"payload": payload}

        with AgentCoordinator.open(tmp_path / "coord.db") as coordinator:
            coordinator.add("member", member)
            await coordinator.handoff(
                "member", "a", coordination_id="aggregate-events",
                handoff_id="aggregate-a",
            )
            await coordinator.handoff(
                "member", "b", coordination_id="aggregate-events",
                handoff_id="aggregate-b",
            )
            handle = coordinator.event_handle("aggregate-events")
            first = handle.read(limit=2)
            assert len(first.events) == 2
            assert first.next_cursor is not None
            second = handle.read(after=first.next_cursor, limit=20)
            all_events = first.events + second.events
            assert {event.data["handoff_id"] for event in all_events} == {
                "aggregate-a",
                "aggregate-b",
            }
            assert all(event.coordination_id == "aggregate-events" for event in all_events)
            assert not second.has_more

    _run(scenario())


def test_expired_durable_agent_handoff_reclaims_its_checkpointed_run(
    tmp_path: Path,
):
    async def scenario() -> None:
        agent = Agent(
            adapter=FakeAdapter.echoing(),
            model="fake",
            session_path=tmp_path / "claims.db",
        )
        try:
            with AgentCoordinator.open(
                tmp_path / "coord.db", lease_seconds=0.02,
            ) as coordinator:
                coordinator.add("agent", agent)
                envelope = HandoffEnvelope.create(
                    coordination_id="expired-durable-agent",
                    sender="planner",
                    recipient="agent",
                    payload="recover me",
                )
                _abandon_once(coordinator, envelope)
                await asyncio.sleep(0.04)
                outcome = await coordinator.dispatch(envelope)
                assert outcome.attempt == 2
                assert outcome.value["text"] == "echo: recover me"
                assert agent.adapter.calls_made == 1
        finally:
            agent.close()

    _run(scenario())


def test_expired_cancel_requested_handoff_settles_without_redelivery(
    tmp_path: Path,
):
    async def scenario() -> None:
        calls = 0

        async def unsafe_member(payload: Any) -> Any:
            nonlocal calls
            calls += 1
            return payload

        with AgentCoordinator.open(
            tmp_path / "coord.db", lease_seconds=0.02,
        ) as coordinator:
            coordinator.add("unsafe", unsafe_member)
            envelope = HandoffEnvelope.create(
                coordination_id="cancel-expired",
                sender="user",
                recipient="unsafe",
                payload="must not run",
            )
            _abandon_once(coordinator, envelope)
            requested = coordinator.cancel_handoff(envelope)
            assert requested.cancel_requested
            await asyncio.sleep(0.04)
            with pytest.raises(RunCancelled):
                await coordinator.dispatch(envelope)
            settled = coordinator.get_handoff_run(envelope)
            assert settled is not None
            assert settled.state.value == "cancelled"
            assert settled.attempt == 2
            assert calls == 0
            assert [
                event.type
                for event in coordinator.execution.agent_events(settled.id)
            ] == [AgentEventType.HANDOFF_CANCELLED]

    _run(scenario())


def test_heartbeat_keeps_long_member_owned_across_store_connections(
    tmp_path: Path,
):
    async def scenario() -> None:
        started = asyncio.Event()

        async def slow(payload: Any) -> Any:
            started.set()
            await asyncio.sleep(0.24)
            return payload

        database = tmp_path / "coord.db"
        with AgentCoordinator.open(
            database, lease_seconds=0.1, heartbeat_interval_s=0.02,
        ) as owner, AgentCoordinator.open(
            database, lease_seconds=0.1, heartbeat_interval_s=0.02,
        ) as contender:
            owner.add("slow", slow, redelivery_safe=True)
            contender.add("slow", slow, redelivery_safe=True)
            envelope = HandoffEnvelope.create(
                coordination_id="heartbeat",
                sender="user",
                recipient="slow",
                payload="work",
            )
            task = asyncio.create_task(owner.dispatch(envelope))
            await started.wait()
            await asyncio.sleep(0.14)
            with pytest.raises(CoordinationBusy):
                await contender.dispatch(envelope)
            assert (await task).value == "work"

    _run(scenario())


def test_sequential_and_round_robin_have_explicit_order(tmp_path: Path):
    async def scenario() -> None:
        seen: list[HandoffEnvelope] = []

        async def step(envelope: HandoffEnvelope) -> str:
            seen.append(envelope)
            return f"{envelope.payload}>{envelope.recipient}"

        with AgentCoordinator.open(tmp_path / "coord.db") as coordinator:
            coordinator.add("a", step, receives_envelope=True)
            coordinator.add("b", step, receives_envelope=True)
            sequential = await coordinator.sequential(
                ["a", "b"], "start", coordination_id="sequence",
            )
            assert sequential.value == "start>a>b"
            assert seen[1].sender == "a"
            assert seen[1].parent_id == seen[0].id

            seen.clear()
            round_robin = await coordinator.round_robin(
                ["a", "b"], "start", rounds=2,
                coordination_id="round-robin",
            )
            assert [item.recipient for item in seen] == ["a", "b", "a", "b"]
            assert round_robin.strategy == "round_robin"
            assert round_robin.value == "start>a>b>a>b"

    _run(scenario())


def test_parallel_is_bounded_ordered_and_can_report_partial_results(
    tmp_path: Path,
):
    async def scenario() -> None:
        active = 0
        peak = 0

        async def worker(payload: str) -> str:
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            try:
                await asyncio.sleep(0.02)
                if payload == "bad":
                    raise ValueError("private member detail")
                return payload.upper()
            finally:
                active -= 1

        with AgentCoordinator.open(
            tmp_path / "coord.db", max_concurrency=2,
        ) as coordinator:
            coordinator.add("worker", worker)
            partial = await coordinator.parallel(
                [("worker", "a"), ("worker", "bad"), ("worker", "c")],
                coordination_id="partial",
                require_all=False,
            )
            assert peak == 2
            assert [outcome.envelope.sequence for outcome in partial.outcomes] == [0, 2]
            assert partial.value == ("A", "C")
            assert len(partial.failures) == 1
            assert partial.failures[0].error_type == "HandoffExecutionError"
            assert "private member detail" not in partial.failures[0].message

            with pytest.raises(CoordinationFailed) as raised:
                await coordinator.parallel(
                    [("worker", "a"), ("worker", "bad")],
                    coordination_id="require-all",
                )
            assert len(raised.value.result.failures) == 1

    _run(scenario())


def test_map_reduce_fans_in_ordered_durable_results(tmp_path: Path):
    async def scenario() -> None:
        async def mapper(payload: int) -> int:
            await asyncio.sleep(0)
            return payload * 2

        async def reducer(payload: dict[str, Any]) -> int:
            assert [item["recipient"] for item in payload["results"]] == [
                "mapper", "mapper", "mapper",
            ]
            return sum(item["value"] for item in payload["results"])

        with AgentCoordinator.open(tmp_path / "coord.db") as coordinator:
            coordinator.add("mapper", mapper)
            coordinator.add("reducer", reducer)
            result = await coordinator.map_reduce(
                [("mapper", 1), ("mapper", 2), ("mapper", 3)],
                "reducer",
                coordination_id="map-reduce",
                max_concurrency=2,
            )
            assert result.strategy == "map_reduce"
            assert result.value == 12
            assert len(result.outcomes) == 4
            reduced = result.outcomes[-1]
            assert reduced.envelope.sender == "coordinator"
            assert reduced.envelope.metadata["parent_ids"] == [
                item.envelope.id for item in result.outcomes[:-1]
            ]

            replay = await coordinator.map_reduce(
                [("mapper", 1), ("mapper", 2), ("mapper", 3)],
                "reducer",
                coordination_id="map-reduce",
            )
            assert replay.value == 12
            assert all(item.replayed for item in replay.outcomes)

    _run(scenario())


def test_selector_is_a_durable_member_and_must_choose_a_candidate(tmp_path: Path):
    async def scenario() -> None:
        selector_calls = 0

        async def selector(payload: dict[str, Any]) -> dict[str, str]:
            nonlocal selector_calls
            selector_calls += 1
            assert [item["name"] for item in payload["candidates"]] == ["a", "b"]
            return {"selected": "b"}

        async def target(payload: Any) -> dict[str, Any]:
            return {"handled": payload}

        async def invalid(_: Any) -> str:
            return "outside"

        with AgentCoordinator.open(tmp_path / "coord.db") as coordinator:
            coordinator.add("selector", selector)
            coordinator.add("invalid", invalid)
            coordinator.add("a", target, description="first")
            coordinator.add("b", target, description="second")
            result = await coordinator.select(
                "selector", ["a", "b"], {"request": 1},
                coordination_id="select",
            )
            assert result.value == {"handled": {"request": 1}}
            assert [item.envelope.recipient for item in result.outcomes] == [
                "selector", "b",
            ]
            replay = await coordinator.select(
                "selector", ["a", "b"], {"request": 1},
                coordination_id="select",
            )
            assert selector_calls == 1
            assert all(item.replayed for item in replay.outcomes)

            with pytest.raises(CoordinationFailed, match="selector"):
                await coordinator.select(
                    "invalid", ["a", "b"], None,
                    coordination_id="invalid-select",
                )

    _run(scenario())


def test_swarm_transfer_chain_is_bounded_and_replayable(tmp_path: Path):
    async def scenario() -> None:
        calls: list[str] = []

        async def planner(payload: Any) -> Transfer:
            calls.append("planner")
            return Transfer("writer", {"plan": payload}, "draft it")

        async def writer(payload: Any) -> dict[str, Any]:
            calls.append("writer")
            return {"draft": payload}

        async def loop(payload: Any) -> Transfer:
            return Transfer("loop", payload)

        with AgentCoordinator.open(tmp_path / "coord.db") as coordinator:
            coordinator.add("planner", planner)
            coordinator.add("writer", writer)
            coordinator.add("loop", loop)
            result = await coordinator.swarm(
                "planner", "topic", coordination_id="swarm",
            )
            assert calls == ["planner", "writer"]
            assert result.value == {"draft": {"plan": "topic"}}
            replay = await coordinator.swarm(
                "planner", "topic", coordination_id="swarm",
            )
            assert calls == ["planner", "writer"]
            assert all(item.replayed for item in replay.outcomes)

            with pytest.raises(CoordinationFailed) as raised:
                await coordinator.swarm(
                    "loop", "forever", coordination_id="bounded", max_hops=3,
                )
            assert raised.value.result.failures[0].error_type == "MaxHopsExceeded"
            assert len(raised.value.result.outcomes) == 3

    _run(scenario())


def test_policy_failures_keep_their_coordination_strategy(tmp_path: Path):
    async def scenario() -> None:
        async def fail(_: Any) -> None:
            raise ValueError("failed")

        async def choose(_: Any) -> str:
            return "fail"

        async def invalid_transfer(_: Any) -> Transfer:
            return Transfer("missing", None)

        with AgentCoordinator.open(tmp_path / "coord.db") as coordinator:
            coordinator.add("fail", fail)
            coordinator.add("choose", choose)
            coordinator.add("invalid-transfer", invalid_transfer)

            with pytest.raises(CoordinationFailed) as round_robin:
                await coordinator.round_robin(
                    ["fail"], None, coordination_id="failed-round-robin",
                )
            assert round_robin.value.result.strategy == "round_robin"

            with pytest.raises(CoordinationFailed) as selected:
                await coordinator.select(
                    "choose", ["fail"], None,
                    coordination_id="failed-selection",
                )
            assert selected.value.result.strategy == "selector"
            assert len(selected.value.result.outcomes) == 1

            with pytest.raises(CoordinationFailed) as swarm:
                await coordinator.swarm(
                    "invalid-transfer", None,
                    coordination_id="invalid-transfer",
                )
            assert swarm.value.result.strategy == "swarm"
            assert swarm.value.result.failures[0].error_type == (
                "InvalidTransferRecipient"
            )

    _run(scenario())


def test_persisted_handoff_cancellation_stops_the_active_owner(tmp_path: Path):
    async def scenario() -> None:
        started = asyncio.Event()

        async def member(_: Any) -> str:
            started.set()
            await asyncio.sleep(1)
            return "too late"

        database = tmp_path / "coord.db"
        with AgentCoordinator.open(
            database, lease_seconds=0.2, heartbeat_interval_s=0.02,
        ) as owner, AgentCoordinator.open(database) as operator:
            owner.add("member", member)
            envelope = HandoffEnvelope.create(
                coordination_id="cancel-handoff",
                sender="user",
                recipient="member",
                payload=None,
            )
            task = asyncio.create_task(owner.dispatch(envelope))
            await started.wait()
            assert operator.get_handoff_run(envelope) is not None
            requested = operator.cancel_handoff(envelope.id)
            assert requested.cancel_requested
            with pytest.raises(RunCancelled):
                await task
            settled = operator.get_handoff_run(envelope)
            assert settled is not None
            assert settled.state.value == "cancelled"
            assert [
                event.type for event in operator.execution.agent_events(settled.id)
            ] == [
                AgentEventType.HANDOFF_STARTED,
                AgentEventType.HANDOFF_CANCELLED,
            ]

    _run(scenario())


def test_context_cancellation_and_absolute_deadline_reach_members(tmp_path: Path):
    async def scenario() -> None:
        contexts: list[RunContext] = []

        async def slow(payload: Any) -> Any:
            context = current_run_context(required=True)
            assert context is not None
            contexts.append(context)
            await asyncio.sleep(1)
            return payload

        with AgentCoordinator.open(tmp_path / "coord.db") as coordinator:
            coordinator.add("slow", slow)
            parent = RunContext.create(run_id="cancel-parent")
            task = asyncio.create_task(coordinator.parallel(
                [("slow", 1), ("slow", 2)],
                coordination_id="cancel-branches",
                context=parent,
            ))
            while len(contexts) < 2:
                await asyncio.sleep(0)
            parent.cancel()
            with pytest.raises(RunCancelled):
                await task
            assert all(item.cancellation is parent.cancellation for item in contexts)

            with pytest.raises(RunDeadlineExceeded):
                await coordinator.handoff(
                    "slow", "late", coordination_id="deadline",
                    timeout_s=0.02,
                )

    _run(scenario())


class _RecordingAgent(Agent):
    def __init__(self) -> None:
        self.seen: list[tuple[Any, AgentState, RunContext]] = []

    async def run(
        self,
        prompt: Any,
        *,
        state: AgentState | None = None,
        context: RunContext | None = None,
        **_: Any,
    ) -> FinalResult:
        assert state is not None and context is not None
        self.seen.append((prompt, state, context))
        return FinalResult("agent answer", state, "natural_stop")


def test_agent_member_receives_causality_and_branch_context(tmp_path: Path):
    async def scenario() -> None:
        agent = _RecordingAgent()
        with AgentCoordinator.open(tmp_path / "coord.db") as coordinator:
            coordinator.add("agent", agent)
            outcome = await coordinator.handoff(
                "agent", "question", coordination_id="agent-coordination",
            )
            prompt, state, context = agent.seen[0]
            assert prompt == "question"
            assert state.metadata["caused_by"] == outcome.envelope.id
            assert state.metadata["coordination_id"] == "agent-coordination"
            assert context.run_id == outcome.run_id
            assert context.metadata["sender"] == "user"
            assert outcome.value["text"] == "agent answer"

    _run(scenario())


def test_ordinary_agent_member_replay_uses_coordination_result(tmp_path: Path):
    async def scenario() -> None:
        agent = _RecordingAgent()
        with AgentCoordinator.open(tmp_path / "coord.db") as coordinator:
            coordinator.add("agent", agent)
            envelope = HandoffEnvelope.create(
                coordination_id="ordinary-agent-replay",
                sender="planner",
                recipient="agent",
                payload="question",
            )
            first = await coordinator.dispatch(envelope)
            replay = await coordinator.dispatch(envelope)
            assert first.value["text"] == "agent answer"
            assert first.value["stop_reason"] == "natural_stop"
            assert replay.replayed
            assert replay.value == first.value
            assert len(agent.seen) == 1

    _run(scenario())


def test_durable_agent_member_reuses_claim_for_approval_checkpoint_and_replay(
    tmp_path: Path,
):
    async def scenario() -> None:
        writes: list[str] = []

        @tool(side_effect="idempotent_write")
        def write_note(text: str) -> str:
            """Write a note for the approval/replay test."""
            writes.append(text)
            return text

        tool_reply = Reply(
            content=({
                "type": "tool_use",
                "id": "write-1",
                "name": "write_note",
                "input": {"text": "approved"},
            },),
            usage=Usage(input=1, output=1),
            stop_reason="tool_use",
            model="fake",
        )
        final_reply = Reply(
            content=({"type": "text", "text": "finished"},),
            usage=Usage(input=1, output=1),
            stop_reason="end_turn",
            model="fake",
        )
        adapter = FakeAdapter.from_replies([tool_reply, final_reply])
        agent = Agent(
            adapter=adapter,
            model="fake",
            tools=[write_note],
            session_path=tmp_path / "agent-claims.db",
        )
        try:
            with AgentCoordinator.open(tmp_path / "coord.db") as coordinator:
                coordinator.add(
                    "writer",
                    agent,
                    approval_policy=writes_require_approval,
                )
                envelope = HandoffEnvelope.create(
                    coordination_id="durable-agent",
                    sender="planner",
                    recipient="writer",
                    payload="write the note",
                )
                with pytest.raises(RunSuspended) as suspended:
                    await coordinator.dispatch(envelope)
                waiting = coordinator.get_handoff_run(envelope)
                assert waiting is not None and waiting.state.value == "waiting"
                assert waiting.attempt == 1
                coordinator.execution.resolve_interrupt(
                    suspended.value.interrupt.id,
                    allow=True,
                    response={"approved_by": "test"},
                )
                outcome = await coordinator.dispatch(envelope)
                assert outcome.value["text"] == "finished"
                assert outcome.value["stop_reason"] == "natural_stop"
                assert writes == ["approved"]
                assert adapter.calls_made == 2
                completed = coordinator.get_handoff_run(envelope)
                assert completed is not None and completed.state.value == "completed"
                assert completed.attempt == 2

                replay = await coordinator.dispatch(envelope)
                assert replay.replayed
                assert replay.value == outcome.value
                assert adapter.calls_made == 2
                assert writes == ["approved"]
        finally:
            agent.close()

    _run(scenario())


def test_durable_agent_member_input_interrupt_resumes_without_tool_execution(
    tmp_path: Path,
):
    async def scenario() -> None:
        tool_calls = 0

        @tool(side_effect="read_only")
        def answer(value: str) -> str:
            """Return the operator-provided value for the input test."""
            nonlocal tool_calls
            tool_calls += 1
            return value

        tool_reply = Reply(
            content=({
                "type": "tool_use",
                "id": "answer-1",
                "name": "answer",
                "input": {"value": "ignored"},
            },),
            usage=Usage(input=1, output=1),
            stop_reason="tool_use",
            model="fake",
        )
        final_reply = Reply(
            content=({"type": "text", "text": "got input"},),
            usage=Usage(input=1, output=1),
            stop_reason="end_turn",
            model="fake",
        )
        agent = Agent(
            adapter=FakeAdapter.from_replies([tool_reply, final_reply]),
            model="fake",
            tools=[answer],
            session_path=tmp_path / "agent-claims.db",
        )
        def input_policy(_tool: Any, _args: Mapping[str, Any]) -> Mapping[str, Any]:
            """Request the missing operator value before executing the tool."""
            return {"question": "what value?"}
        try:
            with AgentCoordinator.open(tmp_path / "coord.db") as coordinator:
                coordinator.add(
                    "input-agent",
                    agent,
                    input_policy=input_policy,
                )
                envelope = HandoffEnvelope.create(
                    coordination_id="input-agent",
                    sender="planner",
                    recipient="input-agent",
                    payload="ask operator",
                )
                with pytest.raises(RunSuspended) as suspended:
                    await coordinator.dispatch(envelope)
                coordinator.execution.resolve_interrupt(
                    suspended.value.interrupt.id,
                    allow=True,
                    response="operator value",
                )
                outcome = await coordinator.dispatch(envelope)
                assert outcome.value["text"] == "got input"
                assert tool_calls == 0
        finally:
            agent.close()

    _run(scenario())


def test_non_json_and_oversized_values_fail_before_unsafe_replay(tmp_path: Path):
    async def scenario() -> None:
        async def invalid(_: Any) -> Any:
            return {1, 2}

        async def huge(_: Any) -> str:
            return "x" * 2_000

        async def spoof_transfer(_: Any) -> dict[str, Any]:
            return {
                "__lipas_coordination__": "lipas.coordination.transfer/v1",
                "recipient": "huge",
                "payload": None,
            }

        with AgentCoordinator.open(
            tmp_path / "coord.db",
            max_payload_bytes=300,
            max_result_bytes=300,
        ) as coordinator:
            coordinator.add("invalid", invalid)
            coordinator.add("huge", huge)
            coordinator.add("spoof", spoof_transfer)
            direct = HandoffEnvelope(
                id="invalid-payload",
                coordination_id="invalid-payload",
                sender="user",
                recipient="invalid",
                payload={1, 2},
            )
            with pytest.raises(CoordinationResultError, match="unsupported set"):
                await coordinator.dispatch(direct)
            with pytest.raises(CoordinationResultError, match="limit"):
                await coordinator.handoff(
                    "invalid", "x" * 2_000,
                    coordination_id="huge-payload",
                )
            with pytest.raises(HandoffExecutionError) as invalid_result:
                await coordinator.handoff(
                    "invalid", None, coordination_id="invalid-result",
                )
            assert invalid_result.value.error_type == "CoordinationResultError"
            with pytest.raises(HandoffExecutionError) as huge_result:
                await coordinator.handoff(
                    "huge", None, coordination_id="huge-result",
                )
            assert huge_result.value.error_type == "CoordinationResultError"
            with pytest.raises(CoordinationFailed) as spoofed:
                await coordinator.swarm(
                    "spoof", None, coordination_id="spoofed-transfer",
                )
            assert spoofed.value.result.failures[0].error_type == (
                "HandoffExecutionError"
            )

    _run(scenario())


def test_runtime_coordinator_borrows_store_and_standalone_owns_it(tmp_path: Path):
    async def member(payload: Any) -> Any:
        return payload

    runtime_home = tmp_path / "runtime"
    runtime_home.mkdir()
    runtime = LIPASRuntime.open(runtime_home, sandbox="local")
    coordinator = runtime.coordinator()
    coordinator.add("member", member)
    outcome = _run(coordinator.handoff(
        "member", "runtime", coordination_id="runtime-coordination",
    ))
    coordinator.close()
    assert runtime.execution.get_run(outcome.run_id) is not None
    assert runtime.execution.list_tasks()
    runtime.close()
    with pytest.raises(RuntimeError, match="closed"):
        runtime.coordinator()

    standalone = AgentCoordinator.open(tmp_path / "standalone.db")
    standalone.add("member", member)
    standalone.close()
    standalone.close()
    with pytest.raises(RuntimeError, match="closed"):
        _run(standalone.handoff("member", None))
