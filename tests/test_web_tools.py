"""Tests for the allowlisted web retrieval capability."""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from lipas.http_client import EgressPolicy, HttpClient, HttpResponse
from lipas.web_tools import WebToolError, extract_page_text, fetch_url, fetch_url_tool


class _FakeHTTP:
    def __init__(self, response: HttpResponse) -> None:
        self.response = response
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    async def request(self, method: str, url: str, **kwargs: Any) -> Any:
        self.calls.append((method, url, kwargs))
        return type(
            "Response",
            (),
            {
                "status_code": self.response.status_code,
                "headers": self.response.headers,
                "content": self.response.body,
                "url": self.response.url,
            },
        )()


def _client(body: bytes, *, content_type: str = "text/html") -> tuple[HttpClient, _FakeHTTP]:
    fake = _FakeHTTP(HttpResponse(
        200, {"Content-Type": content_type}, body, "https://example.test/doc",
    ))
    client = HttpClient(
        base_url="https://example.test",
        egress=EgressPolicy(frozenset({"example.test"})),
        client=fake,
    )
    return client, fake


def test_extract_page_text_removes_scripts_and_preserves_title() -> None:
    text, title, truncated = extract_page_text(
        b"<html><head><title>Example</title><script>ignore()</script></head>"
        b"<body><h1>Hello</h1><p>World &amp; friends</p></body></html>",
        content_type="text/html; charset=utf-8",
    )
    assert title == "Example"
    assert text == "Hello\nWorld & friends"
    assert truncated is False


def test_fetch_url_uses_client_egress_and_records_digest() -> None:
    async def run() -> Any:
        client, fake = _client(b"hello", content_type="text/plain")
        result = await fetch_url(client, "/doc", params={"q": "lipas"})
        return result, fake

    result, fake = asyncio.run(run())
    assert result.text == "hello"
    assert result.bytes == 5
    assert len(result.sha256) == 64
    assert fake.calls[0][0:2] == ("GET", "https://example.test/doc")
    assert fake.calls[0][2]["params"] == {"q": "lipas"}


def test_fetch_url_rejects_oversize_response_and_tool_is_read_only() -> None:
    async def run() -> Any:
        client, _ = _client(b"0123456789", content_type="text/plain")
        with pytest.raises(WebToolError, match="size limit"):
            await fetch_url(client, "/doc", max_bytes=5)
        return fetch_url_tool(client)

    tool = asyncio.run(run())
    assert tool.name == "fetch_url"
    assert tool.side_effect.value == "read_only"
