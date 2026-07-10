"""Contract tests for the current injected-client Anthropic adapter."""
from __future__ import annotations

import asyncio
from decimal import Decimal
from types import SimpleNamespace

from lipas.adapter import LLMAdapter, Message, ModelPrice, PriceTable, Request, TextBlock, complete
from lipas.adapter.anthropic import AnthropicAdapter


PRICE_TABLE = PriceTable(prices={"claude-test": ModelPrice(Decimal("3"), Decimal("15"))})


class Messages:
    def __init__(self, result=None, error: Exception | None = None): self.result, self.error = result, error
    async def create(self, **kwargs):
        if self.error: raise self.error
        return self.result


def adapter(*, result=None, error=None) -> AnthropicAdapter:
    return AnthropicAdapter(SimpleNamespace(messages=Messages(result, error)), PRICE_TABLE)


def request(**overrides) -> Request:
    fields = {"model": "claude-test", "messages": [Message("user", [TextBlock("hi")])], "max_tokens": 100}
    fields.update(overrides)
    return Request(**fields)


def response():
    return SimpleNamespace(model="claude-test", stop_reason="end_turn", content=[SimpleNamespace(type="text", text="hello")], usage=SimpleNamespace(input_tokens=10, output_tokens=5, cache_read_input_tokens=0, cache_creation_input_tokens=0))


def test_protocol_and_normalized_reply():
    a = adapter(result=response())
    assert isinstance(a, LLMAdapter)
    reply = asyncio.run(complete(a, request()))
    assert reply.stop_reason == "end_turn"
    assert reply.content == ({"type": "text", "text": "hello"},)
    assert reply.usage.input == 10 and reply.usage.output == 5


def test_estimate_is_price_table_based():
    estimate = asyncio.run(adapter(result=response()).estimate_cost(request()))
    assert estimate.model == "claude-test"
    assert estimate.max_output_tokens == 100
    assert isinstance(estimate.max_cost_usd, Decimal)


def test_provider_errors_are_terminal_replies():
    reply = asyncio.run(complete(adapter(error=ConnectionError("offline")), request()))
    assert reply.stop_reason == "error"
    assert reply.error_detail is not None


def test_tool_use_is_normalized():
    r = response()
    r.stop_reason = "tool_use"
    r.content = [SimpleNamespace(type="tool_use", id="call_1", name="lookup", input={"id": "x"})]
    reply = asyncio.run(complete(adapter(result=r), request()))
    assert reply.stop_reason == "tool_use"
    assert reply.content[0] == {"type": "tool_use", "id": "call_1", "name": "lookup", "input": {"id": "x"}}
