"""
Tests for lipas/llm.py — surface-level behavior only.

Fakes (NOT mocks) stand in for LLMHarness / ToolHarness / RowSet so we
verify wiring + B7/B8/B9 semantics without depending on the full stack.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence

import pytest

from lipas.adapter import Reply as AdapterReply
from lipas.adapter.content import TextBlock, ToolUseBlock
from lipas.llm import LLM, Reply, ToolCall


# =====================================================================
# Fakes
# =====================================================================

@dataclass
class FakeRowSet:
    store: Any = None  # not exercised at this layer


@dataclass
class FakeAdapter:
    """Returns a pre-loaded sequence of AdapterReplies."""
    scripted: list[AdapterReply] = field(default_factory=list)
    seen_requests: list[Any] = field(default_factory=list)

    async def call(self, request, **_):  # adapter protocol stub
        self.seen_requests.append(request)
        return self.scripted.pop(0)


@dataclass
class FakeLLMHarness:
    """Minimal stand-in: just forwards to the adapter."""
    adapter: Any
    rowset: Any
    guards: Any = ()
    retry_policy: Any = None
    bucket_extractor: Any = None
    replay_cursor: Any = None
    last_compensates: str | None = None

    async def call(self, request, *, compensates=None):
        self.last_compensates = compensates
        return await self.adapter.call(request)


@dataclass
class FakeToolHarness:
    tools: Any
    rowset: Any
    guards: Any = ()
    tool_replayer: Any = None
    invocations: list[dict] = field(default_factory=list)

    async def call(self, *, tool_name, arguments, effect_id, compensates=None):
        rec = {
            "tool_name":   tool_name,
            "arguments":   dict(arguments),
            "effect_id":   effect_id,
            "compensates": compensates,
        }
        self.invocations.append(rec)
        return f"ok:{tool_name}:{effect_id}"


@dataclass
class FakeToolRegistry:
    tools: Sequence[Any]


# tiny tool stub that satisfies LLM._tool_to_spec
@dataclass
class FakeTool:
    name: str
    description: str = "fake"
    parameters_schema: dict = field(default_factory=lambda: {"type": "object"})


# =====================================================================
# Fixture: monkey-patch harness/registry classes used inside lipas.llm
# =====================================================================

@pytest.fixture
def patched(monkeypatch):
    import lipas.llm as llm_mod

    monkeypatch.setattr(llm_mod, "LLMHarness",   FakeLLMHarness)
    monkeypatch.setattr(llm_mod, "ToolHarness",  FakeToolHarness)
    monkeypatch.setattr(llm_mod, "ToolRegistry", FakeToolRegistry)
    return llm_mod


# =====================================================================
# AdapterReply factories
# =====================================================================

def reply_text(s: str) -> AdapterReply:
    return AdapterReply(
        content     = (TextBlock(text=s),),
        stop_reason = "end_turn",
        model       = "fake-model",
        usage       = None,
        error_detail= None,
    )


def reply_tool(tool_id: str, name: str, args: dict) -> AdapterReply:
    return AdapterReply(
        content     = (ToolUseBlock(id=tool_id, name=name, input=args),),
        stop_reason = "tool_use",
        model       = "fake-model",
        usage       = None,
        error_detail= None,
    )


def reply_error() -> AdapterReply:
    return AdapterReply(
        content     = (ToolUseBlock(id="t1", name="f", input={}),),
        stop_reason = "error",
        model       = "fake-model",
        usage       = None,
        error_detail= {"kind": "rate_limit"},
    )


# =====================================================================
# Reply projection
# =====================================================================

class TestReplyProjection:
    def test_text_concat(self):
        r = AdapterReply(
            content=(TextBlock(text="hello "), TextBlock(text="world")),
            stop_reason="end_turn", model="m", usage=None, error_detail=None,
        )
        assert Reply(_raw=r, _tool_harness=None).text == "hello world"

    def test_text_empty_when_only_tool_use(self):
        r = reply_tool("t1", "search", {"q": "x"})
        assert Reply(_raw=r, _tool_harness=FakeToolHarness(None, None)).text == ""

    def test_b9_error_yields_no_tool_calls(self):
        # error stop_reason → tool_calls = (), even if blocks contain tool_use
        rep = Reply(_raw=reply_error(), _tool_harness=FakeToolHarness(None, None))
        assert rep.tool_calls == ()
        assert rep.is_error is True

    def test_b8_tool_use_without_harness_raises(self):
        rep = Reply(_raw=reply_tool("t1", "search", {}), _tool_harness=None)
        with pytest.raises(RuntimeError, match="without tools"):
            _ = rep.tool_calls

    def test_tool_calls_bind_harness_and_invoke_routes(self, monkeypatch):
        import asyncio
        th = FakeToolHarness(tools=None, rowset=None)
        rep = Reply(
            _raw=reply_tool("call_42", "search", {"q": "lipas"}),
            _tool_harness=th,
        )
        (call,) = rep.tool_calls
        assert isinstance(call, ToolCall)
        assert call.id == "call_42"
        assert call.name == "search"
        assert call.arguments == {"q": "lipas"}

        result = asyncio.run(call.invoke(compensates="prev_call"))
        assert result == "ok:search:call_42"
        assert th.invocations == [{
            "tool_name":   "search",
            "arguments":   {"q": "lipas"},
            "effect_id":   "call_42",
            "compensates": "prev_call",
        }]

    def test_as_assistant_message_round_trips_block_shapes(self):
        r = AdapterReply(
            content=(
                TextBlock(text="hi"),
                ToolUseBlock(id="t1", name="f", input={"a": 1}),
            ),
            stop_reason="tool_use", model="m", usage=None, error_detail=None,
        )
        msg = Reply(_raw=r, _tool_harness=FakeToolHarness(None, None)).as_assistant_message()
        assert msg["role"] == "assistant"
        assert msg["content"] == [
            {"type": "text", "text": "hi"},
            {"type": "tool_use", "id": "t1", "name": "f", "input": {"a": 1}},
        ]


# =====================================================================
# LLM.__call__ — B7 (per-call tools fully replaces)
# =====================================================================

class TestLLMPerCallTools:
    def _make(self, patched, *, ctor_tools=()):
        adapter = FakeAdapter()
        llm = LLM(
            adapter = adapter,
            rowset  = FakeRowSet(),
            model   = "m",
            tools   = ctor_tools,
        )
        return llm, adapter

    @pytest.mark.asyncio
    async def test_tools_none_uses_default(self, patched):
        default = [FakeTool("a"), FakeTool("b")]
        llm, adapter = self._make(patched, ctor_tools=default)
        adapter.scripted.append(reply_text("ok"))

        reply = await llm([{"role": "user", "content": "hi"}])
        req = adapter.seen_requests[0]
        # default tools forwarded
        assert [t["name"] for t in req.tools] == ["a", "b"]
        # default harness reused (same instance)
        assert reply._tool_harness is llm._default_th
        assert reply.text == "ok"

    @pytest.mark.asyncio
    async def test_tools_empty_list_replaces_to_no_tools(self, patched):
        llm, adapter = self._make(patched, ctor_tools=[FakeTool("a")])
        adapter.scripted.append(reply_text("ok"))

        reply = await llm([{"role": "user", "content": "hi"}], tools=[])
        req = adapter.seen_requests[0]
        assert req.tools == ()
        # explicit no-tools call → harness must be None for B8 to bite
        assert reply._tool_harness is None

    @pytest.mark.asyncio
    async def test_tools_list_replaces(self, patched):
        llm, adapter = self._make(patched, ctor_tools=[FakeTool("a")])
        adapter.scripted.append(reply_text("ok"))

        override = [FakeTool("z")]
        reply = await llm([{"role": "user", "content": "hi"}], tools=override)
        req = adapter.seen_requests[0]
        assert [t["name"] for t in req.tools] == ["z"]
        # fresh harness, not the default one
        assert reply._tool_harness is not llm._default_th
        assert reply._tool_harness is not None

    @pytest.mark.asyncio
    async def test_compensates_forwarded_to_llm_harness(self, patched):
        llm, adapter = self._make(patched)
        adapter.scripted.append(reply_text("ok"))

        await llm([{"role": "user", "content": "hi"}], compensates="prev")
        assert llm._llm_harness.last_compensates == "prev"

    @pytest.mark.asyncio
    async def test_per_call_overrides_request_fields(self, patched):
        llm, adapter = self._make(patched)
        adapter.scripted.append(reply_text("ok"))

        await llm(
            [{"role": "user", "content": "hi"}],
            model          = "other",
            max_tokens     = 7,
            system         = "be brief",
            temperature    = 0.0,
            stop_sequences = ["</done>"],
        )
        req = adapter.seen_requests[0]
        assert req.model          == "other"
        assert req.max_tokens     == 7
        assert req.system         == "be brief"
        assert req.temperature    == 0.0
        assert req.stop_sequences == ("</done>",)
