"""Phase 2.1 shape invariants.

Focus: structural properties we want to hold regardless of how adapters
are later implemented. Provider-specific concerns are out of scope here.
"""
from __future__ import annotations

import dataclasses
import pytest

from lipas.adapter import (
    Usage,
    ErrorKind,
    TextBlock,
    ToolUseBlock,
    ToolResultBlock,
    Reply,
    Delta,
    ToolUseDelta,
    Thinking,
    Done,
)


# --------------------------------------------------------------------- Usage

class TestUsage:
    def test_default_is_zero(self):
        u = Usage()
        assert (u.input, u.output, u.cache_read, u.cache_write) == (0, 0, 0, 0)
        assert u.total == 0

    def test_total_is_derived(self):
        u = Usage(input=10, output=20, cache_read=5, cache_write=3)
        assert u.total == 38

    def test_frozen(self):
        u = Usage(input=10)
        with pytest.raises(dataclasses.FrozenInstanceError):
            u.input = 20  # type: ignore[misc]

    @pytest.mark.parametrize("field", ["input", "output", "cache_read", "cache_write"])
    def test_negative_rejected(self, field):
        with pytest.raises(ValueError):
            Usage(**{field: -1})

    def test_bool_rejected(self):
        # bool is a subclass of int in Python; we don't want True/False sneaking in.
        with pytest.raises(ValueError):
            Usage(input=True)  # type: ignore[arg-type]

    def test_float_rejected(self):
        with pytest.raises(ValueError):
            Usage(input=1.5)  # type: ignore[arg-type]

    def test_addition_is_componentwise(self):
        a = Usage(input=10, output=20)
        b = Usage(input=5, cache_read=3)
        c = a + b
        assert c == Usage(input=15, output=20, cache_read=3, cache_write=0)

    def test_addition_associative(self):
        a = Usage(input=1, output=2)
        b = Usage(output=3, cache_read=4)
        c = Usage(cache_write=5)
        assert (a + b) + c == a + (b + c)

    def test_addition_commutative(self):
        a = Usage(input=1, output=2, cache_read=3, cache_write=4)
        b = Usage(input=5, output=6, cache_read=7, cache_write=8)
        assert a + b == b + a

    def test_zero_is_identity(self):
        a = Usage(input=10, output=20, cache_read=5, cache_write=3)
        zero = Usage()
        assert a + zero == a
        assert zero + a == a

    def test_equality_by_value(self):
        assert Usage(input=10) == Usage(input=10)
        assert Usage(input=10) != Usage(input=11)


# ----------------------------------------------------------------- ErrorKind

class TestErrorKind:
    def test_is_str_enum(self):
        assert ErrorKind.RATE_LIMIT == "rate_limit"
        assert isinstance(ErrorKind.RATE_LIMIT.value, str)

    def test_transient_classification(self):
        assert ErrorKind.RATE_LIMIT.is_transient
        assert ErrorKind.TIMEOUT.is_transient
        assert ErrorKind.NETWORK.is_transient
        assert ErrorKind.SERVER_ERROR.is_transient

    def test_permanent_classification(self):
        assert not ErrorKind.AUTH.is_transient
        assert not ErrorKind.INVALID_REQUEST.is_transient
        assert not ErrorKind.CONTEXT_LENGTH.is_transient
        assert not ErrorKind.CONTENT_FILTER.is_transient

    def test_unknown_is_not_transient(self):
        # When in doubt, do not retry.
        assert not ErrorKind.UNKNOWN.is_transient


# -------------------------------------------------------------- ContentBlock

class TestContentBlocks:
    def test_text_block(self):
        b = TextBlock(text="hello")
        assert b.type == "text"
        assert b.text == "hello"

    def test_tool_use_block(self):
        b = ToolUseBlock(id="call_1", name="search", input={"q": "lipas"})
        assert b.type == "tool_use"
        assert b.id == "call_1"
        assert b.name == "search"
        assert b.input == {"q": "lipas"}

    def test_tool_result_block_str_content(self):
        b = ToolResultBlock(tool_call_id="call_1", content="42")
        assert b.type == "tool_result"
        assert b.tool_call_id == "call_1"
        assert b.content == "42"
        assert b.is_error is False

    def test_tool_result_block_structured_content(self):
        b = ToolResultBlock(
            tool_call_id="call_1",
            content=[TextBlock(text="line1"), TextBlock(text="line2")],
        )
        assert isinstance(b.content, list) or hasattr(b.content, "__iter__")
        assert len(list(b.content)) == 2

    def test_tool_result_block_error_flag(self):
        b = ToolResultBlock(tool_call_id="call_1", content="boom", is_error=True)
        assert b.is_error is True

    def test_blocks_are_frozen(self):
        b = TextBlock(text="hi")
        with pytest.raises(dataclasses.FrozenInstanceError):
            b.text = "bye"  # type: ignore[misc]


# -------------------------------------------------------------------- Reply

class TestReply:
    def _make_reply(self, **overrides):
        defaults = dict(
            content=[TextBlock(text="hello")],
            usage=Usage(input=10, output=5),
            stop_reason="end_turn",
            model="test-model",
        )
        defaults.update(overrides)
        return Reply(**defaults)  # type: ignore[arg-type]

    def test_minimal_reply(self):
        r = self._make_reply()
        assert r.stop_reason == "end_turn"
        assert r.usage.total == 15
        assert r.model == "test-model"

    def test_reply_with_tool_use(self):
        r = self._make_reply(
            content=[
                TextBlock(text="let me check"),
                ToolUseBlock(id="t1", name="search", input={"q": "x"}),
            ],
            stop_reason="tool_use",
        )
        assert r.stop_reason == "tool_use"
        assert len(list(r.content)) == 2

    def test_error_stop_reason_is_valid(self):
        # Partial content + error termination is a valid Reply.
        r = self._make_reply(
            content=[TextBlock(text="partial...")],
            stop_reason="error",
            error_detail={"type": "provider_error", "message": "partial failure"},
        )
        assert r.stop_reason == "error"
        assert r.content  # partial content preserved

    def test_reply_is_frozen(self):
        r = self._make_reply()
        with pytest.raises(dataclasses.FrozenInstanceError):
            r.model = "other"  # type: ignore[misc]


# ---------------------------------------------------------------- Streaming

class TestStreamEvents:
    def test_delta(self):
        e = Delta(index=0, text="hel")
        assert e.type == "delta"

    def test_tool_use_delta(self):
        e = ToolUseDelta(index=1, partial_json='{"q":"x')
        assert e.type == "tool_use_delta"

    def test_thinking(self):
        e = Thinking(text="...")
        assert e.type == "thinking"

    def test_done_carries_reply(self):
        reply = Reply(
            content=[TextBlock(text="ok")],
            usage=Usage(input=1, output=1),
            stop_reason="end_turn",
            model="m",
        )
        e = Done(reply=reply)
        assert e.type == "done"
        assert e.reply is reply

    def test_events_are_frozen(self):
        e = Delta(index=0, text="x")
        with pytest.raises(dataclasses.FrozenInstanceError):
            e.text = "y"  # type: ignore[misc]


# --------------------------------------------------- type-discriminator sanity

class TestDiscriminators:
    """The `type` field on each tagged union member must be unique within
    its union, so consumers can safely dispatch by it without ambiguity."""

    def test_content_block_type_tags_unique(self):
        tags = {TextBlock(text="").type, ToolUseBlock(id="", name="", input={}).type,
                ToolResultBlock(tool_call_id="", content="").type}
        assert tags == {"text", "tool_use", "tool_result"}

    def test_stream_event_type_tags_unique(self):
        reply = Reply(content=[], usage=Usage(), stop_reason="end_turn", model="m")
        tags = {Delta(index=0, text="").type,
                ToolUseDelta(index=0, partial_json="").type,
                Thinking(text="").type,
                Done(reply=reply).type}
        assert tags == {"delta", "tool_use_delta", "thinking", "done"}
