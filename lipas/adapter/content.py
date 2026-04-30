"""Provider-neutral content blocks.

Messages exchanged with LLMs are sequences of ContentBlocks. There are
exactly three kinds at this layer:

- TextBlock        — plain text content
- ToolUseBlock     — a tool invocation requested by the model
- ToolResultBlock  — the result of a tool invocation, returned to model

ToolResultBlock.tool_call_id is a `str` per P1.2 option (a) — it is the
linkage to the originating ToolUseBlock.id. We deliberately do not
introduce a separate ToolCallId newtype at this layer; the discipline
is carried by field naming and adapter-level validation.

Hashability note
----------------
ToolUseBlock.input is typed as Mapping[str, Any] for ergonomic input.
Python `hash()` on a TextBlock is fine; on a ToolUseBlock it will fail
because dicts are unhashable. This is intentional. Claim-level identity
is established by canonical JSON hashing in the Claim envelope, NOT by
Python's __hash__. Do not use `set()` or dict-key semantics on
ToolUseBlock instances directly.
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Literal, Mapping, Sequence, Union


@dataclass(frozen=True, slots=True)
class TextBlock:
    text: str
    type: Literal["text"] = "text"


@dataclass(frozen=True, slots=True)
class ToolUseBlock:
    id: str
    name: str
    input: Mapping[str, Any]
    type: Literal["tool_use"] = "tool_use"


@dataclass(frozen=True, slots=True)
class ToolResultBlock:
    tool_call_id: str  # references the originating ToolUseBlock.id
    content: Union[str, Sequence[TextBlock]]
    is_error: bool = False
    type: Literal["tool_result"] = "tool_result"


ContentBlock = Union[TextBlock, ToolUseBlock, ToolResultBlock]
