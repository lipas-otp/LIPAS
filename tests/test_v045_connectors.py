"""0.45 connector identity, strict serialization, and recovery contracts."""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from lipas import EmailMessage, HttpClient, HttpClientError, OperationJournal
from lipas.integrations.mcp import MCPClientError, MCPHttpClient


def test_http_and_operation_journals_reject_non_finite_or_non_json_payloads(
    tmp_path: Path,
):
    journal = OperationJournal(tmp_path / "operations.db")
    with pytest.raises(ValueError, match="strict JSON"):
        journal.prepare(
            key="nan", kind="test", request={"value": float("nan")},
        )
    client = HttpClient(
        base_url="https://provider.test",
        journal=journal,
    )
    with pytest.raises(HttpClientError, match="strict JSON"):
        client._body_hash(object())
    journal.close()
    with pytest.raises(RuntimeError, match="closed"):
        journal.get("nan")


def test_email_message_normalizes_immutable_recipients_and_rejects_bad_addresses():
    recipients = [" owner@example.test "]
    message = EmailMessage(
        " bot@example.test ", recipients, "Subject", "Body",
    )
    recipients.append("attacker@example.test")
    assert message.sender == "bot@example.test"
    assert message.recipients == ("owner@example.test",)
    with pytest.raises(ValueError, match="email address"):
        EmailMessage("not-an-address", ("owner@example.test",), "Subject", "Body")


def test_mcp_http_provider_identity_is_unique_per_client_instance():
    seen: list[str] = []

    class Response:
        status_code = 200
        headers = {"content-type": "application/json"}
        content = b'{"jsonrpc":"2.0","id":1,"result":{}}'

        def json(self):
            return {"jsonrpc": "2.0", "id": 1, "result": {}}

    class Client:
        async def post(self, _url, **kwargs):
            seen.append(kwargs["headers"]["X-Request-ID"])
            return Response()

    async def run() -> None:
        first = MCPHttpClient("https://mcp.example.test", client=Client())
        second = MCPHttpClient("https://mcp.example.test", client=Client())
        await first.send({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        await second.send({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})

    asyncio.run(run())
    assert len(seen) == 2 and seen[0] != seen[1]


def test_mcp_http_malformed_write_response_becomes_uncertain(tmp_path: Path):
    class Response:
        status_code = 200
        headers = {"content-type": "application/json"}
        content = b"not-json"
        url = "https://mcp.example.test/mcp"

        def json(self):
            raise ValueError("bad json")

    class Client:
        async def post(self, *_args, **_kwargs):
            return Response()

    journal = OperationJournal(tmp_path / "mcp.db")
    transport = MCPHttpClient(
        "https://mcp.example.test/mcp", client=Client(), journal=journal,
    )
    with pytest.raises(MCPClientError):
        # A malformed provider response is still ambiguous for an external
        # write; it must not remain a silently retryable pending row.
        asyncio.run(transport.send({
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"_lipas_request_id": "write-1"},
        }))
    assert journal.get("write-1").state == "uncertain"  # type: ignore[union-attr]
