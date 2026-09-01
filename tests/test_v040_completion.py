"""v0.40 completion contracts: connectors, reconciliation, and migrations."""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

import pytest

from lipas import (
    ActionGateway,
    CheckpointMigrationError,
    EmailConnector,
    EmailApprovalRequired,
    EmailDelivery,
    EmailMessage,
    EgressPolicy,
    HttpClient,
    HttpOperationUncertain,
    OperationJournal,
    final_result_from_checkpoint,
    migrate_checkpoint_payload,
)
from lipas.integrations import MCPActionServer, MCPClient, MCPClientError, MCPHttpClient
from lipas.operations import PendingOperation
from lipas.tools import tool


@dataclass
class _Response:
    status_code: int
    headers: dict[str, str]
    content: bytes
    url: str


class _Http:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[dict] = []

    async def request(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        if self.fail:
            raise TimeoutError("provider timeout")
        return _Response(202, {"x-request-id": "provider-1"}, b'{"accepted":true}', url)


class _HttpxLike:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def request(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        return _Response(200, {}, b"ok", url)


def test_http_write_is_uncertain_then_reconcileable(tmp_path):
    journal = OperationJournal(tmp_path / "operations.db")
    client = _Http(fail=True)
    http = HttpClient(
        base_url="https://api.example.test/v1",
        egress=EgressPolicy(frozenset({"api.example.test"})),
        journal=journal,
        client=client,
    )

    async def run():
        with pytest.raises(HttpOperationUncertain):
            await http.request(
                "POST", "messages", json_body={"text": "hello"},
                idempotency_key="message-1",
            )

    asyncio.run(run())
    assert journal.get("message-1").state == "uncertain"  # type: ignore[union-attr]
    with pytest.raises(PendingOperation):
        asyncio.run(http.request(
            "POST", "messages", json_body={"text": "hello"},
            idempotency_key="message-1",
        ))
    reconciled = journal.reconcile(
        "message-1", lambda _key: (True, {"accepted": True}, "provider-1"),
    )
    assert reconciled.state == "succeeded"
    journal.close()


def test_http_maps_json_body_and_rejects_duplicate_pending_submission(tmp_path):
    journal = OperationJournal(tmp_path / "operations.db")
    client = _HttpxLike()
    http = HttpClient(
        base_url="https://api.example.test/v1",
        egress=EgressPolicy(frozenset({"api.example.test"})),
        journal=journal,
        client=client,
    )

    async def run():
        response = await http.request(
            "POST", "messages", json_body={"text": "hello"},
            idempotency_key="message-2",
        )
        assert response.status_code == 200

    asyncio.run(run())
    assert client.calls[0]["json"] == {"text": "hello"}
    assert "json_body" not in client.calls[0]

    pending = OperationJournal(tmp_path / "pending.db")
    pending.prepare(
        key="message-3", kind="http_request",
        request={
            "method": "POST", "url": "https://api.example.test/v1/messages",
            "params": {},
            "body_sha256": http._body_hash({"text": "pending"}),
            "provider_request_id": "message-3",
        },
        provider_request_id="message-3",
    )
    pending_http = HttpClient(
        base_url="https://api.example.test/v1",
        egress=EgressPolicy(frozenset({"api.example.test"})),
        journal=pending,
        client=client,
    )
    with pytest.raises(PendingOperation):
        asyncio.run(pending_http.request(
            "POST", "messages", json_body={"text": "pending"},
            idempotency_key="message-3",
        ))
    assert len(client.calls) == 1
    journal.close()
    pending.close()


def test_http_rejects_empty_ids_and_binds_provider_identity(tmp_path):
    journal = OperationJournal(tmp_path / "identity-http.db")
    client = _HttpxLike()
    http = HttpClient(
        base_url="https://api.example.test/v1",
        egress=EgressPolicy(frozenset({"api.example.test"})),
        journal=journal,
        client=client,
    )

    # The fake succeeds, so the first call is terminal; use it to assert the
    # durable identity and then verify a changed provider id is rejected.
    async def successful():
        with pytest.raises(ValueError, match="request_id"):
            await http.request("GET", "health", request_id="")
        response = await http.request(
            "POST", "messages", json_body={"text": "identity"},
            request_id="provider-request-1", idempotency_key="operation-1",
        )
        assert response.status_code == 200
        with pytest.raises(ValueError, match="different operation"):
            await http.request(
                "POST", "messages", json_body={"text": "identity"},
                request_id="provider-request-2", idempotency_key="operation-1",
            )

    asyncio.run(successful())
    operation = journal.get("operation-1")
    assert operation is not None
    assert operation.provider_request_id == "provider-request-1"
    assert client.calls[0]["headers"]["X-Request-ID"] == "provider-request-1"
    assert client.calls[0]["headers"]["Idempotency-Key"] == "operation-1"
    journal.close()


def test_operation_provider_identity_cannot_be_reused_by_another_key(tmp_path):
    journal = OperationJournal(tmp_path / "provider-identity.db")
    journal.prepare(
        key="operation-a", kind="http_request", request={"url": "https://a"},
        provider_request_id="provider-a",
    )
    with pytest.raises(ValueError, match="provider_request_id"):
        journal.prepare(
            key="operation-b", kind="http_request", request={"url": "https://b"},
            provider_request_id="provider-a",
        )
    with pytest.raises(ValueError, match="provider_reference"):
        journal.settle("operation-a", result={"ok": True}, provider_reference=" ")
    journal.close()


def test_http_cancellation_preserves_cancelled_error_but_marks_uncertain(tmp_path):
    class BlockingHttp:
        async def request(self, method, url, **kwargs):
            await asyncio.Event().wait()

    journal = OperationJournal(tmp_path / "cancelled-http.db")
    http = HttpClient(
        base_url="https://api.example.test/v1",
        egress=EgressPolicy(frozenset({"api.example.test"})),
        journal=journal,
        client=BlockingHttp(),
    )

    async def run():
        task = asyncio.create_task(http.request(
            "POST", "messages", json_body={"text": "cancel"},
            idempotency_key="cancel-1",
        ))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())
    operation = journal.get("cancel-1")
    assert operation is not None and operation.state == "uncertain"
    journal.close()


def test_http_write_redirect_is_uncertain_not_success(tmp_path):
    class RedirectHttp:
        async def request(self, method, url, **kwargs):
            return _Response(302, {"location": "https://other.example.test"}, b"", url)

    journal = OperationJournal(tmp_path / "redirect-http.db")
    http = HttpClient(
        base_url="https://api.example.test/v1",
        egress=EgressPolicy(frozenset({"api.example.test"})),
        journal=journal,
        client=RedirectHttp(),
    )

    async def run():
        with pytest.raises(HttpOperationUncertain):
            await http.request(
                "POST", "messages", json_body={"text": "redirect"},
                idempotency_key="redirect-1",
            )

    asyncio.run(run())
    operation = journal.get("redirect-1")
    assert operation is not None and operation.state == "uncertain"
    journal.close()


class _Mail:
    def __init__(self) -> None:
        self.calls = 0
        self.delivered: dict[str, EmailDelivery] = {}

    def send(self, message, *, idempotency_key):
        self.calls += 1
        delivery = EmailDelivery(f"mail-{self.calls}", provider_request_id=idempotency_key)
        self.delivered[idempotency_key] = delivery
        return delivery

    def lookup(self, *, idempotency_key):
        delivery = self.delivered.get(idempotency_key)
        return (
            delivery is not None,
            None if delivery is None else {"accepted": True},
            None if delivery is None else delivery.provider_reference,
        )


def test_email_connector_is_idempotent(tmp_path):
    provider = _Mail()
    connector = EmailConnector(provider, OperationJournal(tmp_path / "mail.db"))
    message = EmailMessage(
        "bot@example.test", ("owner@example.test",), "Draft", "Review",
    )
    with pytest.raises(EmailApprovalRequired):
        connector.send(message, idempotency_key="draft-1")
    first = connector.send(message, idempotency_key="draft-1", approved=True)
    second = connector.send(message, idempotency_key="draft-1", approved=True)
    assert first.provider_reference == second.provider_reference
    assert provider.calls == 1


def test_email_provider_rejection_is_terminal_failure(tmp_path):
    class RejectingMail(_Mail):
        def send(self, message, *, idempotency_key):
            self.calls += 1
            return EmailDelivery("rejected", accepted=False)

    connector = EmailConnector(
        RejectingMail(), OperationJournal(tmp_path / "mail-rejected.db"),
    )
    operation = connector.send(
        EmailMessage("bot@example.test", ("owner@example.test",), "No", "No"),
        idempotency_key="draft-rejected",
        approved=True,
    )
    assert operation.state == "failed"


def test_email_provider_rejection_without_reference_is_terminal_failure(tmp_path):
    class RejectingMail:
        def __init__(self) -> None:
            self.calls = 0

        def send(self, message, *, idempotency_key):
            self.calls += 1
            return {"accepted": False, "reason": "policy"}

        def lookup(self, *, idempotency_key):
            return (False, None, None)

    provider = RejectingMail()
    journal = OperationJournal(tmp_path / "mail-rejected-no-reference.db")
    connector = EmailConnector(provider, journal)
    operation = connector.send(
        EmailMessage("bot@example.test", ("owner@example.test",), "No", "No"),
        idempotency_key="draft-rejected-no-reference",
        approved=True,
    )
    assert operation.state == "failed"
    assert provider.calls == 1
    journal.close()


def test_email_provider_without_reference_is_uncertain(tmp_path):
    class NoReference:
        def send(self, message, *, idempotency_key):
            return {"accepted": True}

        def lookup(self, *, idempotency_key):
            return (False, None, None)

    journal = OperationJournal(tmp_path / "mail-no-reference.db")
    connector = EmailConnector(NoReference(), journal)
    with pytest.raises(RuntimeError, match="uncertain"):
        connector.send(
            EmailMessage("bot@example.test", ("owner@example.test",), "No", "No"),
            idempotency_key="mail-no-reference",
            approved=True,
        )
    operation = journal.get("mail-no-reference")
    assert operation is not None and operation.state == "uncertain"
    journal.close()


def test_email_reconcile_without_reference_stays_uncertain(tmp_path):
    class LookupWithoutReference(_Mail):
        def send(self, message, *, idempotency_key):
            self.calls += 1
            raise TimeoutError("provider timeout")

        def lookup(self, *, idempotency_key):
            return (True, {"accepted": True}, None)

    provider = LookupWithoutReference()
    journal = OperationJournal(tmp_path / "mail-reconcile-no-reference.db")
    connector = EmailConnector(provider, journal)
    with pytest.raises(RuntimeError, match="uncertain"):
        connector.send(
            EmailMessage("bot@example.test", ("owner@example.test",), "No", "No"),
            idempotency_key="mail-reconcile-no-reference",
            approved=True,
        )
    with pytest.raises(ValueError, match="provider_reference"):
        connector.reconcile("mail-reconcile-no-reference")
    operation = journal.get("mail-reconcile-no-reference")
    assert operation is not None and operation.state == "uncertain"
    journal.close()


def test_email_rejects_header_injection_before_persistence(tmp_path):
    with pytest.raises(ValueError, match="CR/LF"):
        EmailMessage(
            "bot@example.test", ("owner@example.test",),
            "Subject\r\nBcc: attacker@example.test", "Body",
        )


def test_mcp_client_transport_boundary():
    messages = []

    async def send(message):
        messages.append(message)
        method = message["method"]
        if method == "initialize":
            return {"jsonrpc": "2.0", "id": message["id"], "result": {"server": "test"}}
        if method == "tools/list":
            return {"jsonrpc": "2.0", "id": message["id"], "result": {"tools": []}}
        if "id" not in message:
            return None
        return {"jsonrpc": "2.0", "id": message["id"], "result": {"isError": False}}

    async def run():
        client = MCPClient(send)
        assert await client.initialize() == {"server": "test"}
        assert await client.list_tools() == ()
        with pytest.raises(TypeError, match="arguments"):
            await client.call_tool("lookup", "not-a-mapping")  # type: ignore[arg-type]
        result = await client.call_tool("lookup", {"id": "1"}, request_id="lookup-1")
        assert result["isError"] is False

    asyncio.run(run())
    tool_message = next(value for value in messages if value.get("method") == "tools/call")
    assert tool_message["params"]["_lipas_request_id"] == "lookup-1"


def test_mcp_http_client_propagates_identity_and_decodes_sse():
    class Response:
        status_code = 200
        headers = {"content-type": "text/event-stream"}
        content = (
            b'data: {"jsonrpc":"2.0","id":1,"result":{"ok":true}}\n\n'
        )

    class Client:
        def __init__(self):
            self.calls = []

        async def post(self, url, **kwargs):
            self.calls.append((url, kwargs))
            return Response()

    async def run():
        transport_client = Client()
        transport = MCPHttpClient(
            "https://mcp.example.test/mcp", client=transport_client,
        )
        payload = await transport.send({
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"_lipas_request_id": "mcp-write-1"},
        })
        assert payload["result"]["ok"] is True
        headers = transport_client.calls[0][1]["headers"]
        assert headers["X-Request-ID"] == "mcp-write-1"
        assert headers["Idempotency-Key"] == "mcp-write-1"

    asyncio.run(run())


def test_mcp_sse_must_match_the_jsonrpc_request_id():
    with pytest.raises(MCPClientError, match="no event for request id"):
        MCPHttpClient._sse_payload(
            b'data: {"jsonrpc":"2.0","id":99,"result":{}}\n\n',
            response_id=1,
        )


def test_mcp_http_client_overwrites_conflicting_identity_headers():
    class Response:
        status_code = 200
        headers = {"content-type": "application/json"}
        content = b'{"jsonrpc":"2.0","id":1,"result":{}}'

        def json(self):
            return {"jsonrpc": "2.0", "id": 1, "result": {}}

    class Client:
        async def post(self, _url, **kwargs):
            self.kwargs = kwargs
            return Response()

    async def run():
        client = Client()
        transport = MCPHttpClient(
            "https://mcp.example.test/mcp",
            client=client,
            headers={"X-Request-ID": "caller", "Idempotency-Key": "caller"},
        )
        await transport.send({
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"_lipas_request_id": "stable-1"},
        })
        assert client.kwargs["headers"]["X-Request-ID"] == "stable-1"
        assert client.kwargs["headers"]["Idempotency-Key"] == "stable-1"

    asyncio.run(run())


def test_mcp_server_honors_host_request_identity():
    class Gateway:
        allow_writes = False

        def __init__(self):
            self.calls = []

        def specs(self):
            return ()

        async def call(self, *args, **kwargs):
            self.calls.append((args, kwargs))
            from lipas.gateway import ActionResult
            return ActionResult(
                request_id=kwargs["request_id"], effect_id="tool_aaaaaaaaaaaa",
                tool_name=args[0], status="ok", content="ok",
            )

    async def run():
        gateway = Gateway()
        server = MCPActionServer(gateway)  # type: ignore[arg-type]
        response = await server.handle({
            "jsonrpc": "2.0", "id": 7, "method": "tools/call",
            "params": {
                "name": "lookup", "arguments": {},
                "_lipas_request_id": "host-stable-7",
            },
        })
        assert response is not None and response["result"]["isError"] is False
        assert gateway.calls[0][1]["request_id"] == "host-stable-7"

    asyncio.run(run())


def test_checkpoint_payload_migration_accepts_legacy_envelope():
    assert migrate_checkpoint_payload({"agent_state": {}})["schema_version"] == 1
    with pytest.raises(Exception):
        migrate_checkpoint_payload({"schema_version": 99})


def test_terminal_checkpoint_restore_uses_schema_gate():
    from lipas.execution import Checkpoint

    checkpoint = Checkpoint(
        "run-future", 1, "terminal",
        {
            "schema_version": 99,
            "final_result": {"state": {}, "stop_reason": "natural_stop"},
        },
        0.0,
    )
    with pytest.raises(CheckpointMigrationError):
        final_result_from_checkpoint(checkpoint)


def test_async_timeout_orphan_converges_before_redelivery(tmp_path):
    completed: list[str] = []

    @tool(side_effect="idempotent_write")
    def slow(value: str) -> str:
        """Finish after the gateway deadline."""
        time.sleep(0.03)
        completed.append(value)
        return value

    async def run():
        with ActionGateway(
            [slow], session=tmp_path / "actions.db", default_timeout_s=0.005,
            allow_writes=True,
        ) as gateway:
            first = await gateway.call("slow", {"value": "x"}, request_id="slow-1")
            assert first.status == "uncertain"
            await asyncio.sleep(0.05)
            second = await gateway.call("slow", {"value": "x"}, request_id="slow-1")
            assert second.status == "ok"
            assert completed == ["x"]

    asyncio.run(run())


def test_gateway_does_not_serialize_unrelated_calls():
    from lipas.gateway import ActionGateway

    started: list[str] = []
    active = 0
    maximum_active = 0

    @tool(side_effect="pure")
    async def wait_for(value: str) -> str:
        """Wait briefly so independent gateway calls overlap."""
        nonlocal active, maximum_active
        started.append(value)
        active += 1
        maximum_active = max(maximum_active, active)
        await asyncio.sleep(0.03)
        active -= 1
        return value

    async def run():
        with ActionGateway([wait_for], default_timeout_s=1.0) as gateway:
            first, second = await asyncio.gather(
                gateway.call("wait_for", {"value": "a"}, request_id="parallel-a"),
                gateway.call("wait_for", {"value": "b"}, request_id="parallel-b"),
            )
            assert first.status == second.status == "ok"
            assert sorted(started) == ["a", "b"]
            assert maximum_active == 2

    asyncio.run(run())
