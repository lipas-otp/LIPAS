"""Request to an LLM adapter.

Provider-neutral request shape.

Message shape (LOCKED, v0.0.3)
------------------------------
``messages`` is a sequence of plain ``Mapping[str, Any]`` —
specifically ``{"role": "user"|"assistant", "content": str | list}``.
This matches both Anthropic's and Ollama's wire format and what
ReActAgent / DeclarativeAgent construct directly.

A ``Message`` dataclass is exported for callers who want a typed
builder; ``Request.__post_init__`` coerces ``Message`` instances to
dicts so adapters always see a single canonical shape. Mixing dicts
and Messages in the same call is allowed.
"""
from __future__ import annotations
import math
from dataclasses import dataclass, field
from typing import Any, Literal, Mapping, Sequence

from .content import ContentBlock


@dataclass(frozen=True, slots=True)
class Message:
    """Typed builder for a chat message. NOT the canonical wire
    shape — Request normalizes Message instances to dicts at
    construction time."""
    role: Literal["user", "assistant"]
    content: Sequence[ContentBlock] | str

    def as_dict(self) -> dict[str, Any]:
        if isinstance(self.content, str):
            return {"role": self.role, "content": self.content}
        # ContentBlock dataclasses → Anthropic-shape dicts.
        # If callers already pass dicts inside content, pass through.
        out_blocks: list[Any] = []
        for b in self.content:
            if isinstance(b, dict):
                out_blocks.append(b)
            elif hasattr(b, "as_dict"):
                out_blocks.append(b.as_dict())
            else:
                # Last resort: dataclass-to-dict via __dict__
                out_blocks.append({
                    k: v for k, v in vars(b).items()
                    if not k.startswith("_")
                })
        return {"role": self.role, "content": out_blocks}


@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    description: str
    input_schema: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class Request:
    model: str
    messages: Sequence[Mapping[str, Any]]
    max_tokens: int
    system: str = ""
    tools: Sequence[ToolSpec | Mapping[str, Any]] = ()
    temperature: float | None = None
    stop_sequences: Sequence[str] = ()
    extra: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.model:
            raise ValueError("model must be non-empty")

        # max_tokens: positive int (reject bool, an int subclass).
        if isinstance(self.max_tokens, bool) or not isinstance(self.max_tokens, int):
            raise ValueError(
                f"max_tokens must be int, got {type(self.max_tokens).__name__}"
            )
        if self.max_tokens <= 0:
            raise ValueError(f"max_tokens must be positive, got {self.max_tokens}")

        # temperature: structural only.
        if self.temperature is not None:
            if isinstance(self.temperature, bool) or not isinstance(
                self.temperature, (int, float)
            ):
                raise ValueError(
                    f"temperature must be a number or None, "
                    f"got {type(self.temperature).__name__}"
                )
            if not math.isfinite(self.temperature):
                raise ValueError(
                    f"temperature must be finite, got {self.temperature}"
                )

        # Normalize messages: Message → dict. Frozen dataclass, so
        # use object.__setattr__ to swap the field.
        normalized: list[Mapping[str, Any]] = []
        for m in self.messages:
            if isinstance(m, Message):
                normalized.append(m.as_dict())
            elif isinstance(m, Mapping):
                normalized.append(m)
            else:
                raise TypeError(
                    f"Request.messages entries must be Mapping or "
                    f"Message, got {type(m).__name__}"
                )
        object.__setattr__(self, "messages", normalized) # list, not tuple
