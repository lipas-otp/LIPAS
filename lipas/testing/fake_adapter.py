"""LIPAS · FakeAdapter — deterministic LLMAdapter for tests.

Implements lipas.adapter.protocol.LLMAdapter without any I/O. Replays
a scripted sequence of Replies, optionally checking each incoming
request against a matcher. Designed so Supervisor / retry / replay
unit tests don't need a live model.

Two construction styles:

    # Style 1: scripted sequence (one Reply per call, in order).
    adapter = FakeAdapter.from_replies([
        Reply(content=({"type": "text", "text": "hi"},),
              usage=Usage(input=10, output=5),
              stop_reason="end_turn", model="fake", error_detail=None),
        ...
    ])

    # Style 2: programmable handler.
    adapter = FakeAdapter(handler=lambda req: build_reply_for(req))

Behavior:
  - estimate_cost: returns ResourceEstimate with input_tokens=0,
    max_cost_usd=0. Tests that gate on budget should bypass or use
    a real estimator.
  - stream: yields exactly one Done event carrying the next Reply.
  - exhaustion: raises RuntimeError on extra calls. Tests that want
    silent end-of-tape behaviour should construct a longer script.

Not for production. Lives under lipas.testing for that reason.
"""
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
    """FakeAdapter ran out of scripted Replies."""


_Handler = Callable[[Request], Reply]


@dataclass
class FakeAdapter:
    """Deterministic LLMAdapter for tests.

    Either ``replies`` (scripted sequence) or ``handler`` (function).
    Mutually exclusive at construction time.
    """

    name: ClassVar[str] = "fake"

    replies: Sequence[Reply] | None = None
    handler: _Handler | None = None

    # Audit trail of every request the adapter saw, in order. Useful
    # for assertions like "the second call had tools=[search]".
    seen_requests: list[Request] = field(default_factory=list, init=False)

    _pos: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        if (self.replies is None) == (self.handler is None):
            raise TypeError(
                "FakeAdapter requires exactly one of replies= or handler="
            )
        if self.replies is not None:
            self.replies = tuple(self.replies)

    # ── factories ──────────────────────────────────────────────

    @classmethod
    def from_replies(cls, replies: Sequence[Reply]) -> "FakeAdapter":
        return cls(replies=tuple(replies))

    @classmethod
    def from_handler(cls, handler: _Handler) -> "FakeAdapter":
        return cls(handler=handler)

    @classmethod
    def echoing(cls, model: str = "fake") -> "FakeAdapter":
        """Trivial: every call returns 'echo: <last user text>'."""
        def _h(req: Request) -> Reply:
            last = ""
            for m in reversed(req.messages):
                if m.get("role") == "user":
                    c = m.get("content", "")
                    last = c if isinstance(c, str) else str(c)
                    break
            return Reply(
                content=({"type": "text", "text": f"echo: {last}"},),
                usage=Usage(input=1, output=1),
                stop_reason="end_turn",
                model=model,
                error_detail=None,
            )
        return cls(handler=_h)

    # ── LLMAdapter protocol ────────────────────────────────────

    async def estimate_cost(self, request: Request) -> ResourceEstimate:
        return ResourceEstimate(
            model=request.model,
            input_tokens=0,
            max_output_tokens=request.max_tokens,
            max_cost_usd=Decimal("0"),
        )

    async def stream(
        self, request: Request,
    ) -> AsyncIterator[StreamEvent]:
        self.seen_requests.append(request)
        reply = self._next_reply(request)
        yield Done(reply=reply)

    # ── internals ──────────────────────────────────────────────

    def _next_reply(self, request: Request) -> Reply:
        if self.handler is not None:
            return self.handler(request)
        assert self.replies is not None
        if self._pos >= len(self.replies):
            raise FakeExhausted(
                f"FakeAdapter exhausted after {self._pos} call(s); "
                f"script length = {len(self.replies)}"
            )
        r = self.replies[self._pos]
        self._pos += 1
        return r

    @property
    def calls_made(self) -> int:
        return len(self.seen_requests)
