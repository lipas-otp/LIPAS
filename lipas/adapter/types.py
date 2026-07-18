"""Canonical provider-neutral values exchanged by LIPAS adapters.

These types intentionally live together: they form one interchange contract,
not a collection of independent runtime subsystems. Provider modules translate
to and from these values; the Agent runtime never needs provider-specific
request, reply, usage, pricing, or streaming types.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field, is_dataclass
from decimal import Decimal
from typing import Any, Literal, Mapping, Sequence, Union, cast

__all__ = [
    "Usage",
    "TextBlock", "ToolUseBlock", "ToolResultBlock", "ContentBlock",
    "Message", "ToolSpec", "Request",
    "StopReason", "Reply",
    "Delta", "ToolUseDelta", "Thinking", "Done", "StreamEvent",
    "ResourceEstimate",
    "ModelPrice", "PriceTable", "UnknownModelError",
]


@dataclass(frozen=True, slots=True)
class Usage:
    """Canonical token usage for one completed model call."""

    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0

    def __post_init__(self) -> None:
        for name in ("input", "output", "cache_read", "cache_write"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(f"Usage.{name} must be a non-negative int, got {value!r}")

    @property
    def total(self) -> int:
        return self.input + self.output + self.cache_read + self.cache_write

    def __add__(self, other: "Usage") -> "Usage":
        if not isinstance(other, Usage):
            return NotImplemented
        return Usage(
            input=self.input + other.input,
            output=self.output + other.output,
            cache_read=self.cache_read + other.cache_read,
            cache_write=self.cache_write + other.cache_write,
        )


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
    tool_call_id: str
    content: Union[str, Sequence[TextBlock]]
    is_error: bool = False
    type: Literal["tool_result"] = "tool_result"


# Provider adapters and the runtime deliberately normalize wire blocks to
# dictionary-shaped values.  The dataclasses above remain convenient typed
# builders, while Mapping is part of the actual interchange contract.
ContentBlock = Union[
    TextBlock,
    ToolUseBlock,
    ToolResultBlock,
    Mapping[str, Any],
]


@dataclass(frozen=True, slots=True)
class Message:
    """Typed builder; Request normalizes it to the canonical dict shape."""

    role: Literal["user", "assistant"]
    content: Sequence[ContentBlock] | str

    def as_dict(self) -> dict[str, Any]:
        if isinstance(self.content, str):
            return {"role": self.role, "content": self.content}
        blocks: list[Any] = []
        for block in self.content:
            if isinstance(block, Mapping):
                blocks.append(block)
            elif is_dataclass(block):
                blocks.append(asdict(cast(Any, block)))
            else:
                raise TypeError(
                    "Message.content entries must be mappings or content "
                    f"block dataclasses, got {type(block).__name__}"
                )
        return {"role": self.role, "content": blocks}


@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    description: str
    input_schema: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class Request:
    """Canonical model request shared by all providers."""

    model: str
    messages: Sequence[Mapping[str, Any]]
    max_tokens: int
    system: str = ""
    tools: Sequence[ToolSpec | Mapping[str, Any]] = ()
    temperature: float | None = None
    stop_sequences: Sequence[str] = ()
    extra: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.model, str) or not self.model.strip():
            raise ValueError("model must be a non-empty string")
        if not isinstance(self.system, str):
            raise TypeError("system must be a string")
        if isinstance(self.max_tokens, bool) or not isinstance(self.max_tokens, int):
            raise ValueError(f"max_tokens must be int, got {type(self.max_tokens).__name__}")
        if self.max_tokens <= 0:
            raise ValueError(f"max_tokens must be positive, got {self.max_tokens}")
        if self.temperature is not None:
            if isinstance(self.temperature, bool) or not isinstance(self.temperature, (int, float)):
                raise ValueError(
                    "temperature must be a number or None, "
                    f"got {type(self.temperature).__name__}"
                )
            if not math.isfinite(self.temperature):
                raise ValueError(f"temperature must be finite, got {self.temperature}")

        messages: list[Mapping[str, Any]] = []
        for message in self.messages:
            if isinstance(message, Message):
                messages.append(message.as_dict())
            elif isinstance(message, Mapping):
                messages.append(message)
            else:
                raise TypeError(
                    "Request.messages entries must be Mapping or "
                    f"Message, got {type(message).__name__}"
                )
        object.__setattr__(self, "messages", messages)


StopReason = Literal["end_turn", "max_tokens", "stop_sequence", "tool_use", "error"]


@dataclass(frozen=True, slots=True)
class Reply:
    """One terminal, normalized model reply."""

    content: Sequence[ContentBlock]
    usage: Usage
    stop_reason: StopReason
    model: str
    error_detail: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.model, str) or not self.model.strip():
            raise ValueError("Reply.model must be a non-empty string")
        if not isinstance(self.usage, Usage):
            raise TypeError("Reply.usage must be Usage")
        if self.stop_reason not in {
            "end_turn", "max_tokens", "stop_sequence", "tool_use", "error",
        }:
            raise ValueError(f"Reply.stop_reason is not recognized: {self.stop_reason!r}")
        if self.stop_reason == "error" and self.error_detail is None:
            raise ValueError("Reply with stop_reason='error' must populate error_detail")
        if self.stop_reason != "error" and self.error_detail is not None:
            raise ValueError(
                f"Reply with stop_reason={self.stop_reason!r} must not "
                f"populate error_detail (got {self.error_detail!r})"
            )


@dataclass(frozen=True, slots=True)
class Delta:
    index: int
    text: str
    type: Literal["delta"] = "delta"


@dataclass(frozen=True, slots=True)
class ToolUseDelta:
    index: int
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


@dataclass(frozen=True, slots=True)
class ResourceEstimate:
    """A pre-flight upper bound for one model call."""

    model: str
    input_tokens: int
    max_output_tokens: int
    max_cost_usd: Decimal

    def __post_init__(self) -> None:
        if not isinstance(self.model, str) or not self.model.strip():
            raise ValueError("ResourceEstimate.model must be a non-empty string")
        for name in ("input_tokens", "max_output_tokens"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(f"{name} must be a non-negative int, got {value!r}")
        if not isinstance(self.max_cost_usd, Decimal):
            raise TypeError("max_cost_usd must be Decimal")
        if not self.max_cost_usd.is_finite() or self.max_cost_usd < 0:
            raise ValueError(
                f"max_cost_usd must be a finite non-negative Decimal, got {self.max_cost_usd!r}"
            )


class UnknownModelError(KeyError):
    """A model is absent from a PriceTable."""

    def __init__(self, model: str):
        super().__init__(model)
        self.model = model


_MILLION = Decimal(1_000_000)


@dataclass(frozen=True, slots=True)
class ModelPrice:
    """Per-million-token price in USD."""

    input_per_mtok: Decimal
    output_per_mtok: Decimal
    cache_read_per_mtok: Decimal = Decimal("0")
    cache_write_per_mtok: Decimal = Decimal("0")

    def __post_init__(self) -> None:
        for name in (
            "input_per_mtok", "output_per_mtok",
            "cache_read_per_mtok", "cache_write_per_mtok",
        ):
            value = getattr(self, name)
            if not isinstance(value, Decimal):
                raise TypeError(f"{name} must be Decimal")
            if not value.is_finite() or value < 0:
                raise ValueError(
                    f"{name} must be a finite non-negative Decimal, got {value!r}"
                )

    def cost(self, usage: Usage) -> Decimal:
        return (
            self.input_per_mtok * usage.input
            + self.output_per_mtok * usage.output
            + self.cache_read_per_mtok * usage.cache_read
            + self.cache_write_per_mtok * usage.cache_write
        ) / _MILLION


@dataclass(frozen=True, slots=True)
class PriceTable:
    prices: Mapping[str, ModelPrice]

    def for_model(self, model: str) -> ModelPrice:
        if model not in self.prices:
            raise UnknownModelError(model)
        return self.prices[model]
