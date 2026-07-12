"""Deterministic adapter fixture shared by repository tests only."""
from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import AsyncIterator, ClassVar

from lipas.adapter import (
    Done, Reply, Request, ResourceEstimate, StreamEvent, Usage,
)

__all__ = ["FakeAdapter", "FakeExhausted"]


class FakeExhausted(RuntimeError):
    """FakeAdapter ran out of scripted replies."""


_Handler = Callable[[Request], Reply]


@dataclass
class FakeAdapter:
    """A no-I/O LLMAdapter fake for runtime tests."""

    name: ClassVar[str] = "fake"
    replies: Sequence[Reply] | None = None
    handler: _Handler | None = None
    seen_requests: list[Request] = field(default_factory=list, init=False)
    _pos: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        if (self.replies is None) == (self.handler is None):
            raise TypeError("FakeAdapter requires exactly one of replies= or handler=")
        if self.replies is not None:
            self.replies = tuple(self.replies)

    @classmethod
    def from_replies(cls, replies: Sequence[Reply]) -> "FakeAdapter":
        return cls(replies=tuple(replies))

    @classmethod
    def from_handler(cls, handler: _Handler) -> "FakeAdapter":
        return cls(handler=handler)

    @classmethod
    def echoing(cls, model: str = "fake") -> "FakeAdapter":
        def reply_for(request: Request) -> Reply:
            text = ""
            for message in reversed(request.messages):
                if message.get("role") == "user":
                    content = message.get("content", "")
                    text = content if isinstance(content, str) else str(content)
                    break
            return Reply(
                content=({"type": "text", "text": f"echo: {text}"},),
                usage=Usage(input=1, output=1),
                stop_reason="end_turn",
                model=model,
                error_detail=None,
            )
        return cls(handler=reply_for)

    async def estimate_cost(self, request: Request) -> ResourceEstimate:
        return ResourceEstimate(
            model=request.model,
            input_tokens=0,
            max_output_tokens=request.max_tokens,
            max_cost_usd=Decimal("0"),
        )

    async def stream(self, request: Request) -> AsyncIterator[StreamEvent]:
        self.seen_requests.append(request)
        yield Done(reply=self._next_reply(request))

    def _next_reply(self, request: Request) -> Reply:
        if self.handler is not None:
            return self.handler(request)
        assert self.replies is not None
        if self._pos >= len(self.replies):
            raise FakeExhausted(
                f"FakeAdapter exhausted after {self._pos} call(s); "
                f"script length = {len(self.replies)}"
            )
        reply = self.replies[self._pos]
        self._pos += 1
        return reply

    @property
    def calls_made(self) -> int:
        return len(self.seen_requests)
