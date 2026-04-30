"""Streaming events from an LLM adapter call.

Streaming events are intentionally NOT folded as Claims and are NOT
reproduced during replay (per §4.1). They exist for live consumption —
UI streaming, token counting, observability — but the durable causal
record is the single call_result Claim emitted at Done, carrying the
assembled Reply.

Event types
-----------
- Delta         — incremental text for content_block[index]
- ToolUseDelta  — incremental JSON for content_block[index].input
- Thinking      — extended-thinking text (Anthropic-style reasoning)
- Done          — terminal event carrying the final assembled Reply

A well-formed stream emits exactly one Done as its last event. Adapters
MUST guarantee this even on error paths: a Reply with
stop_reason="error" is emitted inside Done so downstream consumers
always see a terminal event.
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Literal, Union

from .reply import Reply


@dataclass(frozen=True, slots=True)
class Delta:
    index: int     # which content block this delta belongs to
    text: str
    type: Literal["delta"] = "delta"


@dataclass(frozen=True, slots=True)
class ToolUseDelta:
    index: int     # which content block this delta belongs to
    partial_json: str
    type: Literal["tool_use_delta"] = "tool_use_delta"


@dataclass(frozen=True, slots=True)
class Thinking:
    text: str
    type: Literal["thinking"] = "thinking"


@dataclass(frozen=True, slots=True)
class Done:
    reply: Reply
    type: Literal["done"] = "done"


StreamEvent = Union[Delta, ToolUseDelta, Thinking, Done]
