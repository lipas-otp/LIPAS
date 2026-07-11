"""Regression tests for beta-facing OpenAI, streaming, journal and mailbox APIs."""
from __future__ import annotations

import asyncio

import httpx
import pytest

from lipas.adapter import Done, OpenAIResponsesAdapter, Request, ToolSpec, complete
from lipas.team import Team
from lipas.calculus import Claim
from lipas.agent import Agent
from lipas.operations import OperationJournal, PendingOperation
from lipas.orchestration import AgentOrchestrator, Mailbox, MailboxLeaseError
from lipas.store import ClaimStore
from lipas.supervisor import (
    F_SUP_ATTEMPT_INDEX, F_SUP_IDEMPOTENCY_KEY, F_SUP_MAX_ATTEMPTS,
    F_SUP_REASON, F_SUP_TARGET_EFFECT_ID, TAG_SUPERVISOR_ESCALATE,
    TAG_SUPERVISOR_RETRY, TAG_SUPERVISOR_TERMINATE,
    Policy, PolicyRule, TerminateAction,
)
from lipas.supervisor_projection import project_supervisor
from lipas.testing.fake_adapter import FakeAdapter


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


def test_team_adapts_an_ordinary_async_function(tmp_path):
    mailbox, team = Mailbox(str(tmp_path / "mailbox.db")), None
    async def echo(prompt): return {"echo": prompt}
    team = AgentOrchestrator(mailbox)
    team.register("echo", lambda message: echo(message.payload["prompt"]))
    assert asyncio.run(team.handoff(sender="root", recipient="echo", payload={"prompt": "hello"})) == {"echo": "hello"}


def test_team_is_a_small_facade_over_agent_cells(tmp_path):
    async def upper(prompt): return str(prompt).upper()
    team = Team.open(str(tmp_path / "team.db")).add("upper", upper)
    try:
        assert asyncio.run(team.ask("upper", "hello", message_id="upper-1")) == "HELLO"
    finally:
        team.close()


def test_supervisor_projection_reads_tag_indexed_recommendations():
    store = ClaimStore()
    store.fold(Claim(tag=TAG_SUPERVISOR_RETRY, fields={F_SUP_TARGET_EFFECT_ID: "call_1", F_SUP_IDEMPOTENCY_KEY: "k1", F_SUP_ATTEMPT_INDEX: 1, F_SUP_MAX_ATTEMPTS: 3, F_SUP_REASON: "transient"}))
    store.fold(Claim(tag=TAG_SUPERVISOR_ESCALATE, fields={F_SUP_REASON: "needs approval"}))
    store.fold(Claim(tag=TAG_SUPERVISOR_TERMINATE, fields={F_SUP_REASON: "budget"}))
    view = project_supervisor(store)
    assert view.retries[0].target_effect_id == "call_1"
    assert view.terminated and view.terminate_reason == "budget"
    assert view.escalations[0][F_SUP_REASON] == "needs approval"
    from lipas.calculus_supervisor import project_supervisor as compatibility_projection
    assert compatibility_projection(store) == view


def test_agent_policy_is_wired_into_default_react_lifecycle():
    policy = Policy.of(PolicyRule("stop", lambda _view, _ctx: TerminateAction(reason="test")))
    agent = Agent(adapter=FakeAdapter.echoing(), model="fake", supervisor_policy=policy)
    result = asyncio.run(agent.run("hello"))
    assert result.stop_reason == "supervisor_terminate"
    assert agent.rowset.store.filter(tag=TAG_SUPERVISOR_TERMINATE)
