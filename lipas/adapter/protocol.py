"""LLMAdapter Protocol — the structural contract every LLM provider
integration satisfies.

Adapters translate Request <-> provider native format and emit a
provider-neutral StreamEvent sequence terminating in exactly one Done.
"""
from __future__ import annotations
import inspect
from collections.abc import Awaitable, Callable
from typing import AsyncIterator, Protocol, runtime_checkable

from .types import Done, Reply, Request, ResourceEstimate, StreamEvent

StreamSink = Callable[[StreamEvent], Awaitable[None] | None]


@runtime_checkable
class LLMAdapter(Protocol):
    """LLM provider adapter.

    Error contract (LOCKED, P2.1)
    -----------------------------
    Implementations MAY raise exceptions ONLY for programmer errors:
        - malformed Request that slipped past dataclass validation
        - violations of this Protocol's invariants
        - genuine bugs in the implementation

    ALL provider-side, transport, and runtime failures — including
    network errors, DNS / TLS / connection failures, rate limits,
    authentication errors, content policy rejections, malformed
    provider responses, and provider-internal 5xx — MUST be surfaced
    as a terminal Done event whose Reply has:

        stop_reason   = "error"
        content       = whatever was received before failure (possibly empty)
        usage         = actual consumption observed (possibly zero)
        error_detail  = provider-raw diagnostic info; required when
                        stop_reason='error' (enforced by Reply itself)

    This invariant lets the harness record an effect-result claim
    unconditionally — no try/except boundary at the call site, no
    branching between "Reply path" and "exception path". ErrorKind
    classification happens one layer up, consuming `reply.error_detail`.

    Lifecycle (P2.1)
    ----------------
    No close() / __aenter__. Adapter implementations MUST survive
    repeated construct-use-discard cycles without resource growth;
    enforced by tests/adapter/_leak.py. If a future provider requires
    explicit lifecycle management, a Closeable sub-protocol will be
    introduced; adapters will then implement both, and existing call
    sites will continue to work without modification.
    """

    @property
    def name(self) -> str: ...

    async def estimate_cost(self, request: Request) -> ResourceEstimate: ...

    def stream(self, request: Request) -> AsyncIterator[StreamEvent]:
        """Drive one model call.

        Error contract
        --------------
        Provider/transport/runtime failures MUST surface as a
        terminal Done(reply=Reply(stop_reason='error',
        error_detail=<TypedDict>, usage=<as-billed>)). See
        ``lipas.adapter.types.Reply`` for the full usage-on-error
        and error_detail contract. Only ``asyncio.CancelledError``
        propagates.
        """
        ...

class StreamProtocolError(RuntimeError):
    """Adapter stream violated its contract — e.g. ended without a
    terminal Done event. Always an adapter bug; surfaced loudly so
    callers don't silently get incomplete results."""


async def complete(
    adapter: LLMAdapter,
    request: Request,
    *,
    on_event: StreamSink | None = None,
) -> Reply:
    """Consume an adapter stream and return the assembled Reply.

    Per the LLMAdapter error contract, this function does NOT raise
    on provider-side failures — those arrive as Reply(stop_reason=
    "error", error_detail=...). The only exception this raises is
    StreamProtocolError, which signals an adapter bug (stream ended
    without Done).
    """
    stream = adapter.stream(request)
    try:
        async for event in stream:
            if on_event is not None:
                delivered = on_event(event)
                if inspect.isawaitable(delivered):
                    await delivered
            if isinstance(event, Done):
                return event.reply
    finally:
        close = getattr(stream, "aclose", None)
        if close is not None:
            await close()
    raise StreamProtocolError(
        f"adapter {getattr(adapter, 'name', '?')!r} "
        f"stream ended without Done event"
    )
