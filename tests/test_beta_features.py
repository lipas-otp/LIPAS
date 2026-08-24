"""Regression tests for beta-facing OpenAI, streaming, journal and mailbox APIs."""
from __future__ import annotations

import asyncio
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from lipas.adapter import (
    Done,
    OllamaAdapter,
    OpenAIResponsesAdapter,
    ModelPrice,
    PriceTable,
    Reply,
    Request,
    ToolSpec,
    Usage,
    complete,
)
from lipas.adapter.errors import DEFAULT_POLICY, ErrorKind, RetryPolicy, classify
from lipas.team import Team
from lipas.calculus import Claim
from lipas.agent import Agent
from lipas.operations import (
    OperationJournal,
    OperationSchemaVersionMismatch,
    OperationStateError,
    PendingOperation,
)
from lipas.orchestration import (
    AgentOrchestrator,
    Mailbox,
    MailboxLeaseError,
    MailboxSchemaVersionMismatch,
)
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


def test_openai_responses_translates_react_tool_history_to_wire_items():
    adapter = OpenAIResponsesAdapter(api_key="test")
    request = Request(
        "gpt-test",
        [
            {"role": "user", "content": "look it up"},
            {"role": "assistant", "content": [{
                "type": "tool_use",
                "id": "provider-call-1",
                "name": "lookup",
                "input": {"id": "42"},
            }]},
            {"role": "user", "content": [{
                "type": "tool_result",
                "tool_use_id": "provider-call-1",
                "content": "Ada",
            }]},
        ],
        10,
    )

    assert adapter._build_body(request, stream=True)["input"] == [
        {"role": "user", "content": "look it up"},
        {
            "type": "function_call",
            "call_id": "provider-call-1",
            "name": "lookup",
            "arguments": '{"id":"42"}',
        },
        {
            "type": "function_call_output",
            "call_id": "provider-call-1",
            "output": "Ada",
        },
    ]


def test_openai_stop_sequences_fail_closed_instead_of_being_ignored():
    adapter = OpenAIResponsesAdapter(api_key="test")
    request = Request(
        "gpt-test",
        [{"role": "user", "content": "hi"}],
        10,
        stop_sequences=["END"],
    )

    reply = asyncio.run(complete(adapter, request))

    assert reply.stop_reason == "error"
    assert reply.error_detail["provider_error"]["type"] == "ValueError"
    assert "does not support stop_sequences" in (
        reply.error_detail["provider_error"]["message"]
    )


def test_openai_network_error_has_classifier_compatible_shape():
    def fail(_request):
        raise httpx.ReadTimeout("slow")

    client = httpx.AsyncClient(transport=httpx.MockTransport(fail))
    adapter = OpenAIResponsesAdapter(
        api_key="test", base_url="https://test", client=client,
    )
    reply = asyncio.run(complete(
        adapter, Request("gpt-test", [{"role": "user", "content": "hi"}], 10),
    ))
    asyncio.run(client.aclose())

    assert reply.stop_reason == "error"
    assert classify(reply) is ErrorKind.TIMEOUT


def test_openai_incomplete_response_is_max_tokens_not_provider_error():
    event = {
        "type": "response.incomplete",
        "response": {
            "model": "gpt-test",
            "status": "incomplete",
            "usage": {"input_tokens": 3, "output_tokens": 10},
            "output": [{
                "type": "message",
                "content": [{"type": "output_text", "text": "partial"}],
            }],
        },
    }
    payload = f"data: {__import__('json').dumps(event)}\n\n".encode()
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _req: httpx.Response(200, content=payload),
        ),
    )
    adapter = OpenAIResponsesAdapter(
        api_key="test", base_url="https://test", client=client,
    )
    reply = asyncio.run(complete(
        adapter, Request("gpt-test", [{"role": "user", "content": "hi"}], 10),
    ))
    asyncio.run(client.aclose())

    assert reply.stop_reason == "max_tokens"
    assert reply.content[0]["text"] == "partial"


def test_openai_non_token_incomplete_response_is_a_typed_error():
    event = {
        "type": "response.incomplete",
        "response": {
            "model": "gpt-test",
            "status": "incomplete",
            "incomplete_details": {"reason": "content_filter"},
            "usage": {"input_tokens": 3, "output_tokens": 0},
            "output": [],
        },
    }
    payload = f"data: {__import__('json').dumps(event)}\n\n".encode()
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _req: httpx.Response(200, content=payload),
        ),
    )
    adapter = OpenAIResponsesAdapter(
        api_key="test", base_url="https://test", client=client,
    )
    reply = asyncio.run(complete(
        adapter, Request("gpt-test", [{"role": "user", "content": "hi"}], 10),
    ))
    asyncio.run(client.aclose())

    assert reply.stop_reason == "error"
    assert classify(reply) is ErrorKind.CONTENT_FILTER


def test_openai_cached_input_usage_uses_disjoint_pricing_buckets():
    adapter = OpenAIResponsesAdapter(api_key="test")
    reply = adapter._reply_from_response(
        Request("gpt-test", [{"role": "user", "content": "hi"}], 10),
        {
            "model": "gpt-test",
            "status": "completed",
            "output": [],
            "usage": {
                "input_tokens": 10,
                "output_tokens": 2,
                "input_tokens_details": {"cached_tokens": 4},
            },
        },
    )

    assert reply.usage == Usage(input=6, output=2, cache_read=4)


def test_openai_malformed_function_arguments_fail_closed():
    event = {
        "type": "response.completed",
        "response": {
            "model": "gpt-test",
            "status": "completed",
            "usage": {"input_tokens": 3, "output_tokens": 1},
            "output": [{
                "type": "function_call",
                "call_id": "provider-call",
                "name": "dangerous_defaulted_tool",
                "arguments": "{not-json",
            }],
        },
    }
    payload = f"data: {__import__('json').dumps(event)}\n\n".encode()
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _req: httpx.Response(200, content=payload),
        ),
    )
    adapter = OpenAIResponsesAdapter(
        api_key="test", base_url="https://test", client=client,
    )

    reply = asyncio.run(complete(
        adapter, Request("gpt-test", [{"role": "user", "content": "hi"}], 10),
    ))
    asyncio.run(client.aclose())

    assert reply.stop_reason == "error"
    assert reply.error_detail["provider_error"]["type"] == "ValueError"


def test_ollama_malformed_tool_arguments_are_a_terminal_error_reply():
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(
            200,
            request=request,
            json={
                "model": "gemma-test",
                "done_reason": "stop",
                "message": {"tool_calls": [{
                    "id": "provider-call",
                    "function": {
                        "name": "dangerous_defaulted_tool",
                        "arguments": "{not-json",
                    },
                }]},
            },
        )),
    )
    adapter = OllamaAdapter(client=client)

    reply = asyncio.run(complete(
        adapter, Request("gemma-test", [{"role": "user", "content": "hi"}], 10),
    ))
    asyncio.run(client.aclose())

    assert reply.stop_reason == "error"
    assert reply.error_detail["provider_error"]["type"] == "ValueError"


def test_ollama_preserves_each_tool_result_as_a_tool_message():
    adapter = OllamaAdapter()
    request = Request(
        "gemma-test",
        [
            {"role": "assistant", "content": [
                {"type": "tool_use", "id": "call-1", "name": "a", "input": {}},
                {"type": "tool_use", "id": "call-2", "name": "b", "input": {}},
            ]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "call-1", "content": "A"},
                {"type": "tool_result", "tool_use_id": "call-2", "content": "B"},
            ]},
        ],
        10,
    )

    assert adapter._build_body(request)["messages"] == [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "call-1", "function": {"name": "a", "arguments": {}}},
                {"id": "call-2", "function": {"name": "b", "arguments": {}}},
            ],
        },
        {"role": "tool", "tool_call_id": "call-1", "content": "A"},
        {"role": "tool", "tool_call_id": "call-2", "content": "B"},
    ]


def test_ollama_unknown_done_reason_is_not_natural_success():
    adapter = OllamaAdapter()
    request = Request(
        "gemma-test", [{"role": "user", "content": "hi"}], 10,
    )
    reply = adapter._reply_from_payload(request, {
        "model": "gemma-test",
        "done_reason": "future_reason",
        "message": {"content": "partial"},
    })
    assert reply.stop_reason == "error"
    assert reply.error_detail["provider_error"]["done_reason"] == "future_reason"


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


@pytest.mark.parametrize(
    "meta_table,constructor,error_type",
    [
        ("operation_meta", OperationJournal, OperationSchemaVersionMismatch),
        ("mailbox_meta", Mailbox, MailboxSchemaVersionMismatch),
    ],
)
def test_durable_component_schema_mismatch_fails_closed(
    tmp_path, meta_table, constructor, error_type,
):
    path = tmp_path / f"{meta_table}.db"
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            f"CREATE TABLE {meta_table}(key TEXT PRIMARY KEY,value TEXT NOT NULL)",
        )
        connection.execute(
            f"INSERT INTO {meta_table}(key,value) VALUES('schema_version','999')",
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(error_type, match="schema version 999"):
        constructor(path)


@pytest.mark.parametrize("key,kind", [("", "email"), (1, "email"), ("k", "")])
def test_operation_journal_rejects_invalid_identity_before_persistence(
    tmp_path, key, kind,
):
    with OperationJournal(tmp_path / "operations.db") as journal:
        with pytest.raises(ValueError):
            journal.prepare(key=key, kind=kind, request={})  # type: ignore[arg-type]
        assert journal.pending() == ()


@pytest.mark.parametrize(
    "kwargs",
    [
        {"sender": "", "recipient": "worker", "payload": {}},
        {"sender": "user", "recipient": "", "payload": {}},
        {"sender": "user", "recipient": "worker", "payload": {}, "message_id": ""},
    ],
)
def test_mailbox_rejects_invalid_identity_before_persistence(tmp_path, kwargs):
    mailbox = Mailbox(tmp_path / "mailbox.db")
    try:
        with pytest.raises(ValueError):
            mailbox.send(**kwargs)
    finally:
        mailbox.close()


def test_expired_mailbox_lease_cannot_be_released_by_stale_worker(tmp_path):
    mailbox = Mailbox(tmp_path / "mailbox.db")
    try:
        mailbox.send(sender="user", recipient="worker", payload={}, message_id="m1")
        claimed = mailbox.claim("worker")[0]
        with mailbox._conn:
            mailbox._conn.execute(
                "UPDATE mailbox SET lease_expires=0 WHERE id='m1'",
            )
        with pytest.raises(MailboxLeaseError):
            mailbox.release("m1", lease_token=claimed.lease_token or "")
        assert mailbox.recover_expired() == 1
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


def test_operation_journal_repairs_audit_after_commit_crash_window(tmp_path):
    class Crash(BaseException):
        pass

    path = tmp_path / "operations.db"
    rowset = RowSet(ClaimStore(), [HistoryRow(), CapabilityRow(), EffectRow()])
    journal = OperationJournal(path, rowset=rowset)
    original_fold = rowset.fold

    def crash_audit(_claim):
        raise Crash()

    rowset.fold = crash_audit  # type: ignore[method-assign]
    with pytest.raises(Crash):
        journal.prepare(key="mail-1", kind="email", request={"to": "a"})
    operation = journal.get("mail-1")
    assert operation is not None and operation.state == "pending"
    assert not rowset.store.filter(tag="operation_prepared")
    rowset.fold = original_fold  # type: ignore[method-assign]
    journal.close()

    with OperationJournal(path, rowset=rowset) as reopened:
        claims = rowset.store.filter(tag="operation_prepared")
        assert len(claims) == 1
        assert claims[0].claim_id.startswith("operation_audit_")
        assert reopened.prepare(
            key="mail-1", kind="email", request={"to": "a"},
        ).state == "pending"
        assert len(rowset.store.filter(tag="operation_prepared")) == 1
        assert reopened.repair_audit() == 0


def test_concurrent_operation_execute_has_one_submission_owner(tmp_path):
    path = tmp_path / "operations.db"
    OperationJournal(path).close()
    start = threading.Barrier(2)
    provider_entered = threading.Event()
    second_rejected = threading.Event()
    release_provider = threading.Event()
    provider_calls: list[str] = []

    def provider(*, idempotency_key: str):
        provider_calls.append(idempotency_key)
        provider_entered.set()
        assert release_provider.wait(timeout=2)
        return {"sent": True}

    def execute_once() -> str:
        with OperationJournal(path) as journal:
            start.wait(timeout=2)
            try:
                return journal.execute(
                    key="shared-key",
                    kind="email",
                    request={"to": "a@example.test"},
                    provider=provider,
                ).state
            except PendingOperation:
                second_rejected.set()
                return "not-owner"

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(execute_once) for _ in range(2)]
        assert provider_entered.wait(timeout=2)
        assert second_rejected.wait(timeout=2)
        release_provider.set()
        outcomes = [future.result(timeout=2) for future in futures]

    assert provider_calls == ["shared-key"]
    assert sorted(outcomes) == ["not-owner", "succeeded"]


def test_operation_journal_repairs_terminal_audit_after_commit(tmp_path):
    class Crash(BaseException):
        pass

    path = tmp_path / "operations.db"
    rowset = RowSet(ClaimStore(), [HistoryRow(), CapabilityRow(), EffectRow()])
    journal = OperationJournal(path, rowset=rowset)
    journal.prepare(key="mail-1", kind="email", request={"to": "a"})
    original_fold = rowset.fold
    rowset.fold = lambda _claim: (_ for _ in ()).throw(Crash())  # type: ignore[method-assign]
    with pytest.raises(Crash):
        journal.settle(
            "mail-1", result={"sent": True}, provider_reference="provider-1",
        )
    operation = journal.get("mail-1")
    assert operation is not None and operation.state == "succeeded"
    assert not rowset.store.filter(tag="operation_succeeded")
    rowset.fold = original_fold  # type: ignore[method-assign]
    journal.close()

    with OperationJournal(path, rowset=rowset) as reopened:
        assert len(rowset.store.filter(tag="operation_prepared")) == 1
        assert len(rowset.store.filter(tag="operation_succeeded")) == 1
        assert reopened.settle(
            "mail-1", result={"sent": True}, provider_reference="provider-1",
        ).state == "succeeded"
        assert len(rowset.store.filter(tag="operation_succeeded")) == 1


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


def test_mailbox_supports_symmetric_context_manager_cleanup(tmp_path):
    with Mailbox(tmp_path / "mailbox.db") as mailbox:
        assert mailbox.schema_version == 1
        mailbox.send(sender="root", recipient="worker", payload={"task": "ok"})

    with pytest.raises(sqlite3.ProgrammingError):
        mailbox.get("missing")


def test_mailbox_expired_lease_is_recoverable(tmp_path):
    mailbox = Mailbox(str(tmp_path / "mailbox.db"))
    mailbox.send(sender="a", recipient="b", payload={"x": 1}, message_id="m1")
    claimed = mailbox.claim("b", lease_seconds=0.01)[0]
    assert claimed.status == "leased"
    assert mailbox.recover_expired(now=10**12) == 1
    recovered = mailbox.claim("b")[0]
    assert recovered.id == "m1" and recovered.attempts == 2


def test_mailbox_audits_each_release_attempt_once(tmp_path):
    rowset = RowSet(ClaimStore(), [HistoryRow(), CapabilityRow(), EffectRow()])
    path = tmp_path / "mailbox.db"
    mailbox = Mailbox(path, rowset=rowset)
    try:
        mailbox.send(
            sender="a", recipient="b", payload={"x": 1}, message_id="m1",
        )
        for _ in range(2):
            claimed = mailbox.claim("b")[0]
            mailbox.release(
                claimed.id, lease_token=claimed.lease_token or "",
            )
    finally:
        mailbox.close()

    reopened = Mailbox(path, rowset=rowset)
    try:
        releases = rowset.store.filter(tag="agent_mail_released")
        assert [claim.fields["attempt"] for claim in releases] == [1, 2]
        assert len({claim.claim_id for claim in releases}) == 2
        assert reopened.repair_audit() == 0
    finally:
        reopened.close()


def test_mailbox_repairs_ack_audit_after_commit(tmp_path):
    class Crash(BaseException):
        pass

    path = tmp_path / "mailbox.db"
    rowset = RowSet(ClaimStore(), [HistoryRow(), CapabilityRow(), EffectRow()])
    mailbox = Mailbox(path, rowset=rowset)
    mailbox.send(sender="a", recipient="b", payload={"x": 1}, message_id="m1")
    claimed = mailbox.claim("b")[0]
    original_fold = rowset.fold
    rowset.fold = lambda _claim: (_ for _ in ()).throw(Crash())  # type: ignore[method-assign]
    with pytest.raises(Crash):
        mailbox.acknowledge(
            claimed.id, lease_token=claimed.lease_token or "",
        )
    message = mailbox.get("m1")
    assert message is not None and message.status == "acknowledged"
    assert not rowset.store.filter(tag=TAG_AGENT_MAIL_ACK)
    rowset.fold = original_fold  # type: ignore[method-assign]
    mailbox.close()

    reopened = Mailbox(path, rowset=rowset)
    try:
        assert len(rowset.store.filter(tag=TAG_AGENT_MAIL_ACK)) == 1
        assert reopened.repair_audit() == 0
    finally:
        reopened.close()


def test_handoff_claims_the_message_it_sent_not_an_older_pending_one(tmp_path):
    mailbox = Mailbox(tmp_path / "mailbox.db")
    orchestrator = AgentOrchestrator(mailbox)
    seen: list[str] = []

    async def worker(message):
        seen.append(message.id)
        return message.payload["task"]

    orchestrator.register("worker", worker)
    mailbox.send(
        sender="root", recipient="worker", payload={"task": "old"},
        message_id="old",
    )
    try:
        result = asyncio.run(orchestrator.handoff(
            sender="root",
            recipient="worker",
            payload={"task": "new"},
            message_id="new",
        ))
        assert result == "new"
        assert seen == ["new"]
        assert mailbox.get("old").status == "pending"  # type: ignore[union-attr]
        assert mailbox.get("new").status == "acknowledged"  # type: ignore[union-attr]
    finally:
        mailbox.close()


def test_llm_retry_accounts_usage_from_every_billed_attempt():
    failed = Reply(
        content=(),
        usage=Usage(input=2, output=1),
        stop_reason="error",
        model="fake",
        error_detail={"type": "http_error", "status_code": 429, "body": {}},
    )
    succeeded = Reply(
        content=({"type": "text", "text": "ok"},),
        usage=Usage(input=3, output=2),
        stop_reason="end_turn",
        model="fake",
    )
    rowset = RowSet(
        ClaimStore(),
        [HistoryRow(), CapabilityRow(), EffectRow()],
    )
    harness = LLMHarness(
        adapter=FakeAdapter.from_replies([failed, succeeded]),
        rowset=rowset,
        retry_policy={
            **DEFAULT_POLICY,
            ErrorKind.RATE_LIMIT: RetryPolicy(True, 0.0, 2),
        },
    )
    reply = asyncio.run(harness.call(
        Request("fake", [{"role": "user", "content": "hi"}], 10),
    ))

    assert reply is succeeded
    spends = rowset.store.filter(tag="resource_spent")
    totals = {claim.fields["bucket"]: claim.fields["amount"] for claim in spends}
    assert totals == {"tokens_in": 5.0, "tokens_out": 3.0}


def test_llm_orphan_can_be_closed_from_provider_observation():
    rowset = RowSet(
        ClaimStore(),
        [HistoryRow(), CapabilityRow(), EffectRow()],
    )
    harness = LLMHarness(
        adapter=FakeAdapter.from_replies([]),
        rowset=rowset,
    )
    request = Request("fake", [{"role": "user", "content": "hi"}], 10)
    harness._fold_intent("call_aaaaaaaaaaaa", request, None, "run-1")
    observed = Reply(
        content=({"type": "text", "text": "provider-completed"},),
        usage=Usage(input=2, output=1),
        stop_reason="end_turn",
        model="fake",
    )
    recovered = harness.reconcile_orphan(
        "call_aaaaaaaaaaaa", reply=observed, attempts=2,
    )
    assert recovered.content == observed.content
    node = rowset.project("effect").nodes["call_aaaaaaaaaaaa"]
    assert node.is_terminal
    assert node.result is not None
    assert node.result.fields["attempts"] == 2


def test_llm_cost_budget_accumulates_actual_priced_usage():
    from decimal import Decimal
    from lipas.adapter import ResourceEstimate

    class PricedFake(FakeAdapter):
        prices = PriceTable({
            "fake": ModelPrice(Decimal("1"), Decimal("0")),
        })

        async def estimate_cost(self, request):
            return ResourceEstimate(
                model=request.model,
                input_tokens=60_000,
                max_output_tokens=0,
                max_cost_usd=Decimal("0.06"),
            )

    success = Reply(
        content=({"type": "text", "text": "ok"},),
        usage=Usage(input=60_000),
        stop_reason="end_turn",
        model="fake",
    )
    rowset = RowSet(
        ClaimStore(),
        [HistoryRow(), CapabilityRow(budgets={"cost_usd": 0.1}), EffectRow()],
    )
    harness = LLMHarness(
        adapter=PricedFake.from_replies([success, success]),
        rowset=rowset,
    )
    request = Request("fake", [{"role": "user", "content": "hi"}], 10)

    first = asyncio.run(harness.call(request))
    second = asyncio.run(harness.call(request))

    assert first.stop_reason == "end_turn"
    assert second.stop_reason == "error"
    assert second.error_detail["reason"] == "budget_exhausted"
    projection = rowset.project("capability")
    assert projection["cost_usd"]["spent"] == pytest.approx(0.06)


def test_missing_optional_price_does_not_turn_a_successful_call_into_failure():
    class UnpricedFake(FakeAdapter):
        prices = PriceTable({})

    success = Reply(
        content=({"type": "text", "text": "ok"},),
        usage=Usage(input=3, cache_read=2, output=1),
        stop_reason="end_turn",
        model="unlisted",
    )
    rowset = RowSet(
        ClaimStore(), [HistoryRow(), CapabilityRow(), EffectRow()],
    )
    harness = LLMHarness(
        adapter=UnpricedFake.from_replies([success]), rowset=rowset,
    )

    reply = asyncio.run(harness.call(
        Request("unlisted", [{"role": "user", "content": "hi"}], 10),
    ))

    assert reply is success
    spends = rowset.store.filter(tag="resource_spent")
    totals = {claim.fields["bucket"]: claim.fields["amount"] for claim in spends}
    assert totals == {"tokens_in": 5.0, "tokens_out": 1.0}


@pytest.mark.parametrize("lease_seconds", [True, float("nan"), float("inf")])
def test_mailbox_rejects_lease_values_that_would_make_mail_unrecoverable(
    tmp_path, lease_seconds,
):
    mailbox = Mailbox(tmp_path / "mailbox-invalid-lease.db")
    try:
        sent = mailbox.send(sender="root", recipient="worker", payload={"x": 1})
        with pytest.raises(ValueError, match="finite positive"):
            mailbox.claim("worker", lease_seconds=lease_seconds)
        assert mailbox.get(sent.id).status == "pending"  # type: ignore[union-attr]
    finally:
        mailbox.close()


def test_recovered_llm_effect_completes_partially_written_spend():
    class Crash(BaseException):
        pass

    reply = Reply(
        content=({"type": "text", "text": "ok"},),
        usage=Usage(input=5, output=3),
        stop_reason="end_turn",
        model="fake",
    )
    adapter = FakeAdapter.from_replies([reply])
    rowset = RowSet(
        ClaimStore(),
        [HistoryRow(), CapabilityRow(), EffectRow()],
    )
    harness = LLMHarness(adapter=adapter, rowset=rowset)
    request = Request("fake", [{"role": "user", "content": "hi"}], 10)
    original_fold = rowset.fold

    def crash_on_output_spend(claim):
        if (
            claim.tag == "resource_spent"
            and claim.fields.get("bucket") == "tokens_out"
        ):
            raise Crash()
        return original_fold(claim)

    rowset.fold = crash_on_output_spend  # type: ignore[method-assign]
    with pytest.raises(Crash):
        asyncio.run(harness.call(
            request, effect_id="call_abcdef012345",
        ))
    rowset.fold = original_fold  # type: ignore[method-assign]

    recovered = asyncio.run(harness.call(
        request, effect_id="call_abcdef012345",
    ))
    spends = rowset.store.filter(tag="resource_spent")
    totals = {claim.fields["bucket"]: claim.fields["amount"] for claim in spends}

    assert recovered == reply
    assert adapter.calls_made == 1
    assert totals == {"tokens_in": 5.0, "tokens_out": 3.0}


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


def test_team_repairs_handoff_audit_after_commit_crash_window(tmp_path):
    class Crash(BaseException):
        pass

    path = tmp_path / "team.db"
    team = Team.open(path)
    original_fold = team.rowset.fold

    def crash_audit(_claim):
        raise Crash()

    team.rowset.fold = crash_audit  # type: ignore[method-assign]
    with pytest.raises(Crash):
        team.mailbox.send(
            sender="root",
            recipient="worker",
            payload={"task": "new"},
            message_id="new",
        )
    message = team.mailbox.get("new")
    assert message is not None and message.status == "pending"
    assert not team.rowset.store.filter(tag=TAG_AGENT_HANDOFF)
    team.rowset.fold = original_fold  # type: ignore[method-assign]
    team.close()

    with Team.open(path) as reopened:
        claims = reopened.rowset.store.filter(tag=TAG_AGENT_HANDOFF)
        assert len(claims) == 1
        assert claims[0].claim_id.startswith("mailbox_audit_")
        reopened.mailbox.send(
            sender="root",
            recipient="worker",
            payload={"task": "new"},
            message_id="new",
        )
        assert len(reopened.rowset.store.filter(tag=TAG_AGENT_HANDOFF)) == 1
        assert reopened.mailbox.repair_audit() == 0


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


def test_react_separates_provider_tool_id_from_internal_effect_id():
    calls: list[str] = []

    @tool(side_effect="read_only")
    def lookup(value: str) -> str:
        """Return one value."""
        calls.append(value)
        return value

    tool_reply = Reply(
        content=({
            "type": "tool_use",
            "id": "provider-call-id-with-arbitrary-shape",
            "name": "lookup",
            "input": {"value": "Ada"},
        },),
        usage=Usage(input=1, output=1),
        stop_reason="tool_use",
        model="fake",
    )
    final_reply = Reply(
        content=({"type": "text", "text": "done"},),
        usage=Usage(input=1, output=1),
        stop_reason="end_turn",
        model="fake",
    )
    adapter = FakeAdapter.from_replies([tool_reply, final_reply])
    agent = Agent(adapter=adapter, model="fake", tools=[lookup])
    try:
        result = asyncio.run(agent.run("look up Ada"))
        tool_intent = next(
            claim for claim in agent.rowset.store.filter(tag=TAG_EFFECT_INTENT)
            if claim.fields.get("kind") == "tool_call"
        )
    finally:
        agent.close()

    assert result.text == "done"
    assert calls == ["Ada"]
    assert tool_intent.fields[F_EFFECT_ID].startswith("tool_")
    returned = adapter.seen_requests[1].messages[-1]["content"][0]
    assert returned["tool_use_id"] == "provider-call-id-with-arbitrary-shape"


def test_react_does_not_report_truncated_model_output_as_natural_stop():
    reply = Reply(
        content=({"type": "text", "text": "partial"},),
        usage=Usage(input=1, output=10),
        stop_reason="max_tokens",
        model="fake",
    )
    agent = Agent(
        adapter=FakeAdapter.from_replies([reply]),
        model="fake",
    )
    try:
        result = asyncio.run(agent.run("answer fully"))
    finally:
        agent.close()

    assert result.text == "partial"
    assert result.stop_reason == "max_tokens"
    assert result.is_natural is False


def test_react_rejects_tool_stop_without_valid_tool_block():
    reply = Reply(
        content=({"type": "tool_use", "name": "missing-id", "input": {}},),
        usage=Usage(input=1, output=1),
        stop_reason="tool_use",
        model="fake",
    )
    agent = Agent(
        adapter=FakeAdapter.from_replies([reply]),
        model="fake",
    )
    try:
        result = asyncio.run(agent.run("use a tool"))
    finally:
        agent.close()

    assert result.stop_reason == "error"
    assert result.error["type"] == "malformed_tool_use"


def test_react_rejects_non_mapping_tool_arguments_before_dispatch():
    calls: list[str] = []

    @tool(side_effect="external_write")
    def dangerous_defaulted_tool(value: str = "default") -> str:
        """A default must never turn malformed provider data into a write."""
        calls.append(value)
        return value

    reply = Reply(
        content=({
            "type": "tool_use",
            "id": "provider-call",
            "name": "dangerous_defaulted_tool",
            "input": "not-a-mapping",
        },),
        usage=Usage(input=1, output=1),
        stop_reason="tool_use",
        model="fake",
    )
    agent = Agent(
        adapter=FakeAdapter.from_replies([reply]),
        model="fake",
        tools=[dangerous_defaulted_tool],
    )
    try:
        result = asyncio.run(agent.run("use the tool"))
    finally:
        agent.close()

    assert result.stop_reason == "error"
    assert result.error["type"] == "malformed_tool_use"
    assert calls == []


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


def test_tool_estimate_cannot_spend_an_undeclared_bucket():
    calls = 0

    @tool(
        side_effect="external_write",
        estimate=lambda _args: {"send.requests": 1},
    )
    def send(target: str) -> str:
        """Send a message only after declared resource admission."""
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
    assert "undeclared bucket" in rejection.fields["detail"]["detail"]


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
