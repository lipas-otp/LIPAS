"""Anthropic adapter contract tests (P2.1 spike acceptance criteria).

AC #1  Protocol conformance: isinstance(a, LLMAdapter) passes.
AC #2  Error contract: 429 / 500 / network drop / mid-stream
       provider error all return Reply(stop_reason="error",
       error_detail=...) and never raise.
AC #3  Leak: 100 construct-call-discard cycles with mock transport
       leave residual <= 2 instances of (AnthropicAdapter,
       httpx.AsyncClient).
AC #4  Temperature out-of-range: ValueError at translate-entry
       (programmer error, NOT Reply(error)). Boundary values pass.
"""
from __future__ import annotations

import asyncio
import json
from decimal import Decimal

import httpx
import pytest

from lipas.adapter import (
    LLMAdapter, Message, ModelPrice, PriceTable, Request, TextBlock,
    complete,
)
from lipas.adapter.anthropic import AnthropicAdapter

from test_adapter_leak import assert_no_leak_under_repeated_use


def run(coro):
    return asyncio.run(coro)


PRICE_TABLE = PriceTable(prices={
    "claude-test": ModelPrice(
        input_per_mtok=Decimal("3"),
        output_per_mtok=Decimal("15"),
    ),
})


def make_request(**overrides) -> Request:
    defaults = dict(
        model="claude-test",
        messages=[Message(role="user", content=[TextBlock(text="hi")])],
        max_tokens=100,
    )
    defaults.update(overrides)
    return Request(**defaults)  # type: ignore[arg-type]


def _sse(events: list[dict]) -> bytes:
    """Encode dicts as Anthropic-flavoured SSE."""
    parts = []
    for e in events:
        parts.append(f"event: {e['type']}\n")
        parts.append(f"data: {json.dumps(e)}\n\n")
    return "".join(parts).encode("utf-8")


SUCCESS_SSE = _sse([
    {"type": "message_start", "message": {
        "model": "claude-test",
        "usage": {"input_tokens": 10, "output_tokens": 0},
    }},
    {"type": "content_block_delta", "index": 0,
     "delta": {"type": "text_delta", "text": "hello"}},
    {"type": "message_delta",
     "delta": {"stop_reason": "end_turn"},
     "usage": {"output_tokens": 5}},
    {"type": "message_stop"},
])


def make_adapter(handler) -> AnthropicAdapter:
    return AnthropicAdapter(
        api_key="test-key",
        price_table=PRICE_TABLE,
        transport=httpx.MockTransport(handler),
    )


# ====================================================================
# AC #1 — Protocol conformance
# ====================================================================

class TestProtocolConformance:
    def test_isinstance_llmadapter(self):
        a = make_adapter(lambda r: httpx.Response(200, content=SUCCESS_SSE))
        assert isinstance(a, LLMAdapter)

    def test_happy_path_streams_and_completes(self):
        a = make_adapter(lambda r: httpx.Response(200, content=SUCCESS_SSE))
        reply = run(complete(a, make_request()))
        assert reply.stop_reason == "end_turn"
        assert reply.error_detail is None
        assert reply.content[0].text == "hello"  # type: ignore[attr-defined]
        assert reply.usage.input == 10
        assert reply.usage.output == 5

    def test_estimate_cost_returns_decimal(self):
        a = make_adapter(lambda r: httpx.Response(200, content=SUCCESS_SSE))
        est = run(a.estimate_cost(make_request()))
        assert est.model == "claude-test"
        assert isinstance(est.max_cost_usd, Decimal)
        assert est.max_output_tokens == 100


# ====================================================================
# AC #2 — Error contract consistency
# ====================================================================

class TestErrorContract:
    """Every transport / provider failure must surface as
    Reply(stop_reason="error", error_detail=...). complete() never
    raises for these — that is the foundation of upstream
    ErrorKind retry classification."""

    def test_429_rate_limit(self):
        a = make_adapter(lambda r: httpx.Response(
            429,
            json={"type": "error", "error": {
                "type": "rate_limit_error",
                "message": "Too many requests"}},
        ))
        reply = run(complete(a, make_request()))
        assert reply.stop_reason == "error"
        assert reply.error_detail is not None
        assert reply.error_detail["type"] == "http_error"
        assert reply.error_detail["status_code"] == 429

    def test_500_server_error(self):
        a = make_adapter(lambda r: httpx.Response(
            500,
            json={"type": "error", "error": {
                "type": "api_error",
                "message": "Internal"}},
        ))
        reply = run(complete(a, make_request()))
        assert reply.stop_reason == "error"
        assert reply.error_detail["status_code"] == 500

    def test_network_disconnect(self):
        def boom(request):
            raise httpx.ConnectError("connection refused")
        a = make_adapter(boom)
        reply = run(complete(a, make_request()))
        assert reply.stop_reason == "error"
        assert reply.error_detail["type"] == "network_error"
        assert "ConnectError" in reply.error_detail["exception_type"]

    def test_mid_stream_provider_error_preserves_partial(self):
        partial_then_err = _sse([
            {"type": "message_start", "message": {
                "model": "claude-test",
                "usage": {"input_tokens": 10, "output_tokens": 0}}},
            {"type": "content_block_delta", "index": 0,
             "delta": {"type": "text_delta", "text": "partial"}},
            {"type": "error", "error": {
                "type": "overloaded_error",
                "message": "Provider overloaded mid-stream"}},
        ])
        a = make_adapter(lambda r: httpx.Response(200, content=partial_then_err))
        reply = run(complete(a, make_request()))
        assert reply.stop_reason == "error"
        assert reply.error_detail["type"] == "provider_error"
        # Partial output preserved — caller may decide what to do with it.
        assert reply.content[0].text == "partial"  # type: ignore[attr-defined]


# ====================================================================
# AC #3 — Leak test
# ====================================================================

class TestLeak:
    def test_no_leak_under_100_iterations(self):
        # The transport is intentionally shared across iterations
        # (it's the test harness's own state, not adapter surface).
        # Each adapter constructs a fresh httpx.AsyncClient. We
        # track BOTH AnthropicAdapter and httpx.AsyncClient — a
        # transport-layer leak would still fail this test even if
        # the adapter shell itself were correctly freed.
        transport = httpx.MockTransport(
            lambda r: httpx.Response(200, content=SUCCESS_SSE)
        )

        def construct() -> AnthropicAdapter:
            return AnthropicAdapter(
                api_key="test-key",
                price_table=PRICE_TABLE,
                transport=transport,
            )

        async def call_once(a: AnthropicAdapter) -> None:
            await complete(a, make_request())

        run(assert_no_leak_under_repeated_use(
            construct=construct,
            call=call_once,
            iterations=100,
            track_types=(AnthropicAdapter, httpx.AsyncClient),
        ))


# ====================================================================
# AC #4 — Temperature out-of-range
# ====================================================================

class TestTemperatureOutOfRange:
    """Anthropic accepts temperature in [0.0, 1.0]. AnthropicAdapter
    raises ValueError synchronously for out-of-range values; this
    is a programmer error, NOT a runtime failure (see adapter
    docstring). Compare: UnknownModelError on bad model.
    Reply(stop_reason="error") is reserved for transient
    transport/provider failures the caller could not have prevented."""

    def test_too_high_raises_at_estimate_cost(self):
        a = make_adapter(lambda r: httpx.Response(200, content=SUCCESS_SSE))
        with pytest.raises(ValueError, match="temperature"):
            run(a.estimate_cost(make_request(temperature=5.0)))

    def test_too_high_raises_through_stream(self):
        a = make_adapter(lambda r: httpx.Response(200, content=SUCCESS_SSE))
        with pytest.raises(ValueError, match="temperature"):
            run(complete(a, make_request(temperature=5.0)))

    def test_negative_raises(self):
        a = make_adapter(lambda r: httpx.Response(200, content=SUCCESS_SSE))
        with pytest.raises(ValueError, match="temperature"):
            run(complete(a, make_request(temperature=-0.1)))

    def test_just_above_one_raises(self):
        a = make_adapter(lambda r: httpx.Response(200, content=SUCCESS_SSE))
        with pytest.raises(ValueError, match="temperature"):
            run(complete(a, make_request(temperature=1.0001)))

    def test_boundary_zero_ok(self):
        a = make_adapter(lambda r: httpx.Response(200, content=SUCCESS_SSE))
        reply = run(complete(a, make_request(temperature=0.0)))
        assert reply.stop_reason == "end_turn"

    def test_boundary_one_ok(self):
        a = make_adapter(lambda r: httpx.Response(200, content=SUCCESS_SSE))
        reply = run(complete(a, make_request(temperature=1.0)))
        assert reply.stop_reason == "end_turn"

    def test_temperature_omitted_ok(self):
        a = make_adapter(lambda r: httpx.Response(200, content=SUCCESS_SSE))
        reply = run(complete(a, make_request(temperature=None)))
        assert reply.stop_reason == "end_turn"


class TestExtraDoesNotShadowProtected:
    """request.extra is forward-compat passthrough only. Shadowing
    first-class Request fields is a programmer error and raises
    synchronously — same contract as temperature out-of-range.
    Critical for P2.2: retry layer reuses Request across attempts."""

    def test_extra_model_raises(self):
        a = make_adapter(lambda r: httpx.Response(200, content=SUCCESS_SSE))
        with pytest.raises(ValueError, match="protected keys"):
            run(complete(a, make_request(extra={"model": "other-model"})))

    def test_extra_stream_raises(self):
        a = make_adapter(lambda r: httpx.Response(200, content=SUCCESS_SSE))
        with pytest.raises(ValueError, match="protected keys"):
            run(complete(a, make_request(extra={"stream": False})))

    def test_extra_unrelated_keys_pass_through(self):
        captured = {}
        def handler(r):
            captured["body"] = json.loads(r.content)
            return httpx.Response(200, content=SUCCESS_SSE)

        a = make_adapter(handler)
        reply = run(complete(a, make_request(
            extra={"top_p": 0.9, "metadata": {"user_id": "x"}},
        )))
        assert reply.stop_reason == "end_turn"
        assert captured["body"]["top_p"] == 0.9
        assert captured["body"]["metadata"] == {"user_id": "x"}
