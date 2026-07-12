"""Regression tests for beta-facing OpenAI, streaming, journal and mailbox APIs."""
from __future__ import annotations

import asyncio

import httpx
import pytest

from lipas.adapter import Done, OpenAIResponsesAdapter, Reply, Request, ToolSpec, Usage, complete
from lipas.team import Team
from lipas.calculus import Claim
from lipas.agent import Agent
from lipas.operations import OperationJournal, OperationStateError, PendingOperation
from lipas.orchestration import AgentOrchestrator, Mailbox, MailboxLeaseError
from lipas.orchestration import TAG_AGENT_HANDOFF, TAG_AGENT_MAIL_ACK, TAG_AGENT_MAIL_CLAIM
from lipas.store import ClaimStore
from lipas.harness import LLMHarness
from lipas.tool_harness import ToolHarness
from lipas.tools import ToolRegistry, tool
from lipas.guard import GuardVerdict
from lipas.supervisor import (
    F_SUP_ATTEMPT_INDEX, F_SUP_IDEMPOTENCY_KEY, F_SUP_MAX_ATTEMPTS,
    F_SUP_REASON, F_SUP_TARGET_EFFECT_ID, TAG_SUPERVISOR_ESCALATE,
    TAG_SUPERVISOR_RETRY, TAG_SUPERVISOR_TERMINATE,
    Policy, PolicyRule, TerminateAction, project_supervisor,
)
from tests.fake_adapter import FakeAdapter
from lipas.rows import RowSet
from lipas.rows.capability import CapabilityRow
from lipas.rows.effect import (
    EffectRow, F_ARGUMENTS, F_CAUSED_BY, F_EFFECT_ID, F_REPLY, F_REQUEST,
    TAG_EFFECT_INTENT,
)
from lipas.rows.history import HistoryRow


def test_openai_responses_normalizes_text_and_tool_calls():
    events = [
        {"type": "response.output_text.delta", "output_index": 0, "delta": "hello"},
        {"type": "response.completed", "response": {"model": "gpt-test", "status": "completed", "usage": {"input_tokens": 3, "output_tokens": 2}, "output": [
            {"type": "message", "content": [{"type": "output_text", "text": "hello"}]},
            {"type": "function_call", "call_id": "call_1", "name": "lookup", "arguments": '{"id":"42"}'},
        ]}},
    ]
    payload = "\n\n".join(f"data: {__import__('json').dumps(e)}" for e in events).encode()
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(200, content=payload)))
    adapter = OpenAIResponsesAdapter(api_key="test", base_url="https://test", client=client)
    request = Request("gpt-test", [{"role": "user", "content": "hi"}], 10, tools=[ToolSpec("lookup", "look up", {"type": "object"})])
    async def run():
        stream = [event async for event in adapter.stream(request)]
        assert stream[0].text == "hello"
        assert isinstance(stream[-1], Done)
        reply = stream[-1].reply
        assert reply.stop_reason == "tool_use"
        assert reply.content[1]["input"] == {"id": "42"}
    asyncio.run(run())
    asyncio.run(client.aclose())


def test_operation_journal_requires_reconciliation_after_uncertain(tmp_path):
    journal = OperationJournal(str(tmp_path / "operations.db"))
    with pytest.raises(RuntimeError):
        journal.execute(key="mail-1", kind="email", request={"to": "a"}, provider=lambda **_: (_ for _ in ()).throw(RuntimeError("lost")))
    assert journal.get("mail-1").state == "uncertain"  # type: ignore[union-attr]
    with pytest.raises(PendingOperation):
        journal.execute(key="mail-1", kind="email", request={"to": "a"}, provider=lambda **_: {"sent": True})
    assert journal.reconcile("mail-1", lambda _: (True, {"sent": True}, "provider-1")).state == "succeeded"


def test_operation_journal_refuses_pending_row_after_restart(tmp_path):
    path = str(tmp_path / "operations.db")
    with OperationJournal(path) as first:
        first.prepare(key="charge-1", kind="charge", request={"amount": 1})
    with OperationJournal(path) as restarted:
        with pytest.raises(PendingOperation):
            restarted.execute(key="charge-1", kind="charge", request={"amount": 1}, provider=lambda **_: {"charged": True})
        assert restarted.reconcile("charge-1", lambda _: (False, None, None)).state == "failed"


def test_operation_journal_never_rewrites_a_terminal_outcome(tmp_path):
    with OperationJournal(tmp_path / "operations.db") as journal:
        journal.prepare(key="mail-1", kind="email", request={"to": "a"})
        journal.settle("mail-1", result={"sent": True}, provider_reference="provider-1")

        lookup_called = False

        def stale_lookup(_key):
            nonlocal lookup_called
            lookup_called = True
            return False, None, None

        # A repeated reconciliation must not consult stale provider state or
        # mutate a known success into a failure.
        assert journal.reconcile("mail-1", stale_lookup).state == "succeeded"
        assert lookup_called is False
        with pytest.raises(OperationStateError, match="terminal"):
            journal.fail("mail-1", error={"type": "contradiction"})


def test_operation_journal_repeats_the_same_terminal_transition_idempotently(tmp_path):
    with OperationJournal(tmp_path / "operations.db") as journal:
        journal.prepare(key="mail-1", kind="email", request={"to": "a"})
        first = journal.settle("mail-1", result={"sent": True}, provider_reference="provider-1")
        second = journal.settle("mail-1", result={"sent": True}, provider_reference="provider-1")
        assert second == first


def test_operation_journal_marks_result_recording_failure_uncertain(tmp_path):
    with OperationJournal(tmp_path / "operations.db") as journal:
        # The provider returned, so a non-serializable result is not a safe
        # reason to leave the submission looking as if it never started.
        with pytest.raises(TypeError):
            journal.execute(
                key="mail-1",
                kind="email",
                request={"to": "a"},
                provider=lambda **_: {"provider_payload": {"not-json"}},
            )
        operation = journal.get("mail-1")
        assert operation is not None
        assert operation.state == "uncertain"
        assert operation.error and operation.error["type"] == "TypeError"


def test_sqlite_runtime_helpers_create_missing_parent_directories(tmp_path):
    journal_path = tmp_path / "new-project" / "runs" / "operations.db"
    mailbox_path = tmp_path / "another-project" / "runs" / "mailbox.db"

    with OperationJournal(journal_path):
        assert journal_path.is_file()
    mailbox = Mailbox(mailbox_path)
    try:
        assert mailbox_path.is_file()
    finally:
        mailbox.close()


def test_operation_journal_transitions_are_claims_linked_to_effect(tmp_path):
    rowset = RowSet(ClaimStore(), [HistoryRow(), CapabilityRow(), EffectRow()])
    journal = OperationJournal(str(tmp_path / "operations.db"), rowset=rowset)
    journal.prepare(key="mail-1", kind="email", request={"to": "a"}, effect_id="call_aaaaaaaaaaaa")
    journal.mark_uncertain("mail-1", error={"type": "timeout"})
    journal.reconcile("mail-1", lambda _: (True, {"sent": True}, "provider-1"))
    claims = list(rowset.store)
    assert [claim.tag for claim in claims] == [
        "operation_prepared", "operation_uncertain", "operation_succeeded",
    ]
    assert {claim.fields["effect_id"] for claim in claims} == {"call_aaaaaaaaaaaa"}
    journal.close()


def test_mailbox_is_idempotent_and_acknowledges_after_handler(tmp_path):
    mailbox = Mailbox(str(tmp_path / "mailbox.db"))
    orchestrator = AgentOrchestrator(mailbox)
    seen = []
    async def worker(message):
        seen.append(message.id)
        return message.payload["task"]
    orchestrator.register("worker", worker)
    async def run():
        assert await orchestrator.handoff(sender="root", recipient="worker", payload={"task": "ok"}, message_id="m1") == "ok"
    asyncio.run(run())
    assert seen == ["m1"] and mailbox.get("m1").status == "acknowledged"  # type: ignore[union-attr]
    with pytest.raises(MailboxLeaseError):
        asyncio.run(orchestrator.handoff(sender="root", recipient="worker", payload={"task": "ok"}, message_id="m1"))


def test_mailbox_expired_lease_is_recoverable(tmp_path):
    mailbox = Mailbox(str(tmp_path / "mailbox.db"))
    mailbox.send(sender="a", recipient="b", payload={"x": 1}, message_id="m1")
    claimed = mailbox.claim("b", lease_seconds=0.01)[0]
    assert claimed.status == "leased"
    assert mailbox.recover_expired(now=10**12) == 1
    recovered = mailbox.claim("b")[0]
    assert recovered.id == "m1" and recovered.attempts == 2


def test_expired_lease_cannot_acknowledge_before_recovery(tmp_path):
    mailbox = Mailbox(tmp_path / "mailbox.db")
    try:
        mailbox.send(sender="a", recipient="b", payload={"x": 1}, message_id="m1")
        claimed = mailbox.claim("b")[0]
        # Set expiry directly so this test checks acknowledge's SQL predicate,
        # not merely the separate recover_expired() path.
        mailbox._conn.execute("UPDATE mailbox SET lease_expires=0 WHERE id='m1'")
        mailbox._conn.commit()
        with pytest.raises(MailboxLeaseError, match="active lease"):
            mailbox.acknowledge("m1", lease_token=claimed.lease_token or "")
        assert mailbox.recover_expired(now=1) == 1
        assert mailbox.get("m1").status == "pending"  # type: ignore[union-attr]
    finally:
        mailbox.close()


def test_journal_and_mailbox_close_are_idempotent(tmp_path):
    journal = OperationJournal(tmp_path / "operations.db")
    journal.close()
    journal.close()
    mailbox = Mailbox(tmp_path / "mailbox.db")
    mailbox.close()
    mailbox.close()


def test_team_adapts_an_ordinary_async_function(tmp_path):
    mailbox, team = Mailbox(str(tmp_path / "mailbox.db")), None
    async def echo(prompt): return {"echo": prompt}
    team = AgentOrchestrator(mailbox)
    team.register("echo", lambda message: echo(message.payload["prompt"]))
    assert asyncio.run(team.handoff(sender="root", recipient="echo", payload={"prompt": "hello"})) == {"echo": "hello"}


def test_team_is_a_small_facade_over_named_members(tmp_path):
    async def upper(prompt): return str(prompt).upper()
    team = Team.open(str(tmp_path / "team.db")).add("upper", upper)
    try:
        assert asyncio.run(team.ask("upper", "hello", message_id="upper-1")) == "HELLO"
        tags = [claim.tag for claim in team.rowset.store]
        assert [TAG_AGENT_HANDOFF, TAG_AGENT_MAIL_CLAIM, TAG_AGENT_MAIL_ACK] == tags
    finally:
        team.close()


def test_team_ask_sync_is_the_normal_script_entrypoint(tmp_path):
    async def upper(prompt):
        return str(prompt).upper()

    with Team.open(str(tmp_path / "team.db")) as team:
        team.add("upper", upper)
        assert team.ask_sync("upper", "hello", message_id="upper-1") == "HELLO"


def test_team_rejects_a_synchronous_member_before_delivery(tmp_path):
    with Team.open(tmp_path / "team.db") as team:
        with pytest.raises(TypeError, match="async callable"):
            team.add("bad", lambda prompt: prompt)


def test_team_message_id_causes_agent_effect_trace(tmp_path):
    agent = Agent(adapter=FakeAdapter.echoing(), model="fake")
    team = Team.open(str(tmp_path / "team.db")).add("assistant", agent)
    try:
        asyncio.run(team.ask("assistant", "hello", message_id="research-1"))
        intents = agent.rowset.store.filter(tag=TAG_EFFECT_INTENT)
        assert intents and intents[0].fields[F_CAUSED_BY] == "research-1"
        assert F_EFFECT_ID in intents[0].fields
    finally:
        team.close()
        agent.close()


def test_supervisor_projection_reads_tag_indexed_recommendations():
    store = ClaimStore()
    store.fold(Claim(tag=TAG_SUPERVISOR_RETRY, fields={F_SUP_TARGET_EFFECT_ID: "call_1", F_SUP_IDEMPOTENCY_KEY: "k1", F_SUP_ATTEMPT_INDEX: 1, F_SUP_MAX_ATTEMPTS: 3, F_SUP_REASON: "transient"}))
    store.fold(Claim(tag=TAG_SUPERVISOR_ESCALATE, fields={F_SUP_REASON: "needs approval"}))
    store.fold(Claim(tag=TAG_SUPERVISOR_TERMINATE, fields={F_SUP_REASON: "budget"}))
    view = project_supervisor(store)
    assert view.retries[0].target_effect_id == "call_1"
    assert view.terminated and view.terminate_reason == "budget"
    assert view.escalations[0][F_SUP_REASON] == "needs approval"


def test_agent_policy_is_wired_into_default_react_lifecycle():
    policy = Policy.of(PolicyRule("stop", lambda _view, _ctx: TerminateAction(reason="test")))
    agent = Agent(adapter=FakeAdapter.echoing(), model="fake", supervisor_policy=policy)
    result = asyncio.run(agent.run("hello"))
    assert result.stop_reason == "supervisor_terminate"
    assert agent.rowset.store.filter(tag=TAG_SUPERVISOR_TERMINATE)


def test_agent_supervision_accepts_a_path_session(tmp_path):
    policy = Policy.of(PolicyRule("stop", lambda _view, _ctx: TerminateAction(reason="test")))
    agent = Agent(
        adapter=FakeAdapter.echoing(),
        model="fake",
        session_path=tmp_path / "runs" / "agent.db",
        supervisor_policy=policy,
    )
    try:
        assert asyncio.run(agent.run("hello")).stop_reason == "supervisor_terminate"
    finally:
        agent.close()


def test_runtime_snapshots_mutable_llm_request_and_reply_values():
    request = Request("fake", [{"role": "user", "content": "before"}], 10)
    reply = Reply(
        content=({"type": "text", "text": "before"},),
        usage=Usage(input=1, output=1),
        stop_reason="end_turn",
        model="fake",
    )
    rowset = RowSet(ClaimStore(), [HistoryRow(), CapabilityRow(), EffectRow()])
    harness = LLMHarness(
        adapter=FakeAdapter.from_replies([reply]),
        rowset=rowset,
    )
    asyncio.run(harness.call(request))

    request.messages[0]["content"] = "after"
    reply.content[0]["text"] = "after"
    intent = rowset.store.filter(tag=TAG_EFFECT_INTENT)[0]
    result = rowset.store.filter(tag="call_result")[0]
    assert intent.fields[F_REQUEST].messages[0]["content"] == "before"
    assert result.fields[F_REPLY].content[0]["text"] == "before"


def test_runtime_snapshots_mutable_tool_arguments_before_tool_execution():
    @tool(side_effect="pure")
    def mutate(payload: dict[str, str]) -> str:
        """Mutate the private payload to model an unsafe legacy client."""
        payload["status"] = "changed"
        return payload["status"]

    rowset = RowSet(ClaimStore(), [HistoryRow(), CapabilityRow(), EffectRow()])
    harness = ToolHarness(tools=ToolRegistry([mutate]), rowset=rowset)
    caller_arguments = {"payload": {"status": "before"}}
    asyncio.run(harness.call(tool_name="mutate", arguments=caller_arguments))

    intent = rowset.store.filter(tag=TAG_EFFECT_INTENT)[0]
    assert caller_arguments == {"payload": {"status": "before"}}
    assert intent.fields[F_ARGUMENTS] == {"payload": {"status": "before"}}


@pytest.mark.parametrize("estimate", [
    lambda _args: (_ for _ in ()).throw(RuntimeError("estimator unavailable")),
    lambda _args: {"tool_calls": float("nan")},
])
def test_invalid_tool_estimate_rejects_before_tool_execution(estimate):
    calls = 0

    @tool(side_effect="external_write", estimate=estimate)
    def send(target: str) -> str:
        """Send a message only after a valid resource estimate."""
        nonlocal calls
        calls += 1
        return target

    rowset = RowSet(ClaimStore(), [HistoryRow(), CapabilityRow(), EffectRow()])
    result = asyncio.run(
        ToolHarness(tools=ToolRegistry([send]), rowset=rowset).call(
            tool_name="send", arguments={"target": "ada"},
        )
    )
    assert result["is_error"] is True
    assert calls == 0
    rejection = rowset.store.filter(tag="call_rejected")[0]
    assert rejection.fields["reason"] == "estimate_invalid"


def test_tool_estimate_receives_the_same_default_arguments_as_the_tool():
    seen = []

    @tool(side_effect="pure", estimate=lambda args: seen.append(dict(args)) or {})
    def greet(name: str = "Ada") -> str:
        """Return a greeting with one default argument."""
        return f"hello {name}"

    rowset = RowSet(ClaimStore(), [HistoryRow(), CapabilityRow(), EffectRow()])
    result = asyncio.run(
        ToolHarness(tools=ToolRegistry([greet]), rowset=rowset).call(
            tool_name="greet", arguments={},
        )
    )
    assert result["content"] == "hello Ada"
    assert seen == [{"name": "Ada"}]
    intent = rowset.store.filter(tag=TAG_EFFECT_INTENT)[0]
    assert intent.fields[F_ARGUMENTS] == {"name": "Ada"}


def test_guard_and_estimate_mutation_cannot_rewrite_admitted_inputs():
    class MutatingLLMGuard:
        name = "mutating"

        async def check(self, target, _estimate):
            target.request.messages[0]["content"] = "changed by guard"
            return GuardVerdict.allow()

    request = Request("fake", [{"role": "user", "content": "before"}], 10)
    rowset = RowSet(ClaimStore(), [HistoryRow(), CapabilityRow(), EffectRow()])
    llm = LLMHarness(
        adapter=FakeAdapter.echoing(),
        rowset=rowset,
        guards=[MutatingLLMGuard()],
    )
    assert asyncio.run(llm.call(request)).content[0]["text"] == "echo: before"
    assert request.messages[0]["content"] == "before"

    @tool(
        side_effect="pure",
        estimate=lambda args: args["payload"].update({"status": "changed"}) or {},
    )
    def inspect(payload: dict[str, str]) -> str:
        """Return the original payload status."""
        return payload["status"]

    tool_rowset = RowSet(ClaimStore(), [HistoryRow(), CapabilityRow(), EffectRow()])
    tool_result = asyncio.run(
        ToolHarness(tools=ToolRegistry([inspect]), rowset=tool_rowset).call(
            tool_name="inspect", arguments={"payload": {"status": "before"}},
        )
    )
    assert tool_result["content"] == "before"
