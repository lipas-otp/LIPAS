"""Regression tests for beta-facing OpenAI, streaming, journal and mailbox APIs."""
from __future__ import annotations

import asyncio

import httpx
import pytest

from lipas.adapter import Done, OpenAIResponsesAdapter, Request, ToolSpec, complete
from lipas.operations import OperationJournal, PendingOperation
from lipas.orchestration import AgentOrchestrator, Mailbox


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
