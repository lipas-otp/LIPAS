"""lipas core types and the 11 normative invariants.

This module is the public type surface of lipas. All adapter implementations
MUST satisfy the invariants below; the `lipas.testing.adapter_contract` test
suite encodes each as one or more executable assertions.

===============================================================================
INV-ROUND-TRIP
    For a Reply produced by provider P, appending `reply.as_message()` to the
    original messages list and sending via the same provider P's adapter
    yields an HTTP body whose assistant-message portion is BYTE-EQUIVALENT to
    the original response's content blocks: same block order, same fields,
    signatures preserved, cache_control markers preserved. This property is
    a NECESSARY (not sufficient) condition for prompt-cache hits. "Byte
    equivalence" applies only when the Reply / message is unmodified; the
    modification-detection contract is INV-LIPAS-HASH.

INV-LIPAS-TRANSPARENT
    Any valid OpenAI chat-messages list is a valid lipas input without any
    `_lipas` sidecar. Known degradations when `_lipas` is absent (v1, non-
    exhaustive — future providers may introduce additional `_lipas`-borne
    features that likewise degrade):
      (a) same-provider continuation does NOT guarantee prompt-cache hits;
      (b) Anthropic extended-thinking signatures are not round-tripped;
      (c) multimodal tool_result content degrades to a string.
    Core conversational functionality is unaffected.

INV-LIPAS-STRIP
    Adapters MUST deep-strip every `_lipas` key (at any nesting depth) from
    messages before serializing to a provider HTTP body. Compliance assertion
    (v1 guarantees no other key contains the substring `_lipas`):
        `b'"_lipas"' not in http_body_bytes`.
    Every adapter's test suite MUST include this assertion.

INV-LIPAS-HASH
    `_lipas.content_hash` is defined as
        hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()[:16]
    where
        payload = {"content": msg["content"],
                   "tool_calls": msg.get("tool_calls")}
        canonical_json = json.dumps(..., sort_keys=True,
                                    separators=(",", ":"),
                                    ensure_ascii=False)
    Before using `_lipas.content_blocks`, adapters MUST recompute and compare
    the hash; on mismatch, fall back to the OpenAI-format path and emit
    `LipasStaleNativeWarning`. Schema-version match is EXACT.

INV-STREAM-EQUIV
    For any call that completes successfully (the `Done` event was emitted,
    or `collect()` returned normally), the Reply from `llm(m, tools=t)` and
    `await llm.astream(m, tools=t).collect()` satisfy:
      (hard)           text, thinking, tool_calls, stop_reason,
                       provider_message: field-by-field equal;
      (provider-dep.)  usage, response_id: equal when the provider supports
                       streaming usage/id reporting, otherwise the streaming
                       path may have None or partial values;
      (semantic-diff.) latency_ms: non-stream = full-response wall time,
                       stream = first-byte to Done;
      (stream-only)    ttft_ms: filled only on the streaming path.

INV-PARALLEL-SAFE
    Concurrent invocation of two DIFFERENT ToolCall instances introduces no
    race conditions within lipas. NOT guaranteed: (a) thread/coroutine safety
    of the user's handler; (b) behavior of concurrent invocations of the
    SAME ToolCall instance — that is governed by INV-TOOLCALL-ONCE.

INV-TOOLCALL-PARSABLE
    Every ToolCall yielded in `reply.tool_calls` has an already-parsed
    `arguments: dict`. Adapters DROP tool calls with unparsable JSON
    arguments (such calls remain in `provider_message` for debugging).
    Therefore `not reply.tool_calls` is a sufficient check for "no
    executable tool calls".

INV-TOOLCALL-ONCE
    A ToolCall instance has a single shared invocation counter across both
    `invoke` and `ainvoke`. The counter is set on handler ENTRY (not
    completion). A second call — regardless of sync/async — raises
    `ToolAlreadyInvoked`. Handler exceptions do NOT unlock retry. Internal
    implementation invariant: every code path that sets `_invoked=True` and
    exits MUST also write a terminal `(outcome, error/result)` state — there
    is no observable "claimed but no terminal state". For retry: `call.copy()`.

INV-PROVIDEREVENT-FALLBACK
    For any native provider event in a stream, lipas emits EITHER a typed
    event (TextDelta / ThinkingDelta / ToolCallStart / ToolCallReady /
    Done) OR a `ProviderEvent` wrapping the raw payload — never both. A
    single stream MAY interleave typed events with ProviderEvents.

INV-STOP-REASON-NORMALIZED
    `Reply.stop_reason` is one of exactly these six strings:
        "end_turn", "tool_use", "max_tokens",
        "refusal", "content_filter", "pause_turn".
    The provider's raw value is preserved in `provider_message`. Unknown
    provider values are mapped to "end_turn" and emit
    `LipasUnknownStopReasonWarning`.

INV-FROZEN-REFS
    `Reply`, `Usage`, `ThinkingBlock` and all stream-event classes are
    `frozen=True` dataclasses. `Reply.tool_calls` and `Reply.thinking` use
    `tuple` to freeze the reference set. `ToolCall` is deliberately NOT
    frozen — its `outcome`, `result`, `error` fields are written after
    `invoke` / `ainvoke`. ToolCall is NOT hashable (`__hash__ is None`) to
    prevent accidental use as set/dict keys despite its mutability.
===============================================================================
"""
from __future__ import annotations

import inspect
import json
import threading
import warnings
from dataclasses import dataclass, field
from typing import Any, Callable, Literal, TypeAlias

from ._lipas_meta import KNOWN_PROVIDERS, attach_lipas
from .exceptions import (
    LipasError,
    LipasUnknownProviderWarning,
    ToolAlreadyInvoked,
)

# -----------------------------------------------------------------------------
# Messages are just dicts. The `_lipas` sidecar is optional (INV-LIPAS-TRANSPARENT).
# -----------------------------------------------------------------------------
Message: TypeAlias = dict[str, Any]

# -----------------------------------------------------------------------------
# Normalized stop reasons (INV-STOP-REASON-NORMALIZED).
# -----------------------------------------------------------------------------
StopReason: TypeAlias = Literal[
    "end_turn",
    "tool_use",
    "max_tokens",
    "refusal",
    "content_filter",
    "pause_turn",
]

STOP_REASONS: frozenset[str] = frozenset({
    "end_turn",
    "tool_use",
    "max_tokens",
    "refusal",
    "content_filter",
    "pause_turn",
})


# =============================================================================
# Usage
# =============================================================================
@dataclass(frozen=True, slots=True, kw_only=True)
class Usage:
    """Token accounting for one provider call (INV-FROZEN-REFS).

    Constructed keyword-only: the five integer fields are homogeneous and
    unreadable positionally. `Usage(100, 200, 50, 10, 5)` communicates
    nothing; `Usage(input_tokens=100, output_tokens=200, ...)` is obvious.

    A zero value means "provider reported zero OR lipas could not extract
    this metric"; for reasoning_tokens in particular, 0 does NOT mean the
    model did not reason — use `reply.thinking` truthiness for that.
    `reasoning_tokens` is an accounting/billing signal only.

    Source per provider for reasoning_tokens:
      OpenAI:    response.usage.completion_tokens_details.reasoning_tokens
      Anthropic: not directly reported; Anthropic adapters may report 0.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0


# =============================================================================
# ThinkingBlock
# =============================================================================
@dataclass(frozen=True, slots=True)
class ThinkingBlock:
    """One contiguous chunk of reasoning text from the model (INV-FROZEN-REFS).

    - Anthropic extended thinking: `signature` is populated (opaque token that
      the adapter must round-trip via `_lipas.content_blocks`).
    - OpenAI o-series: `signature` is None; reasoning is surfaced heuristically.

    A list of ThinkingBlocks preserves intra-thinking ORDER but loses the
    interleaving between thinking and tool_use. v1 intentionally does not
    surface interleaving; use `provider_message` / `_lipas.content_blocks` for
    a faithful view.
    """

    text: str
    signature: str | None = None


# =============================================================================
# ToolCall — mutable, structurally equal on user-facing fields, unhashable
# =============================================================================
@dataclass
class ToolCall:
    """A single tool invocation request emitted by the model.

    Lifecycle (INV-TOOLCALL-ONCE):
      - Constructed by the adapter with parsed `arguments` (INV-TOOLCALL-PARSABLE).
      - `invoke()` / `ainvoke()` atomically claims the call on entry.
      - On handler return: `outcome="ok"`, `result` is set.
      - On handler raise:  `outcome="error"`, `error` is set, exception re-raised.
      - A 2nd call (sync or async) always raises `ToolAlreadyInvoked`.

    Internal invariant: every code path that sets `_invoked=True` and exits
    MUST write a terminal `(outcome, error/result)` state. In particular,
    the coroutine-misuse branch of `invoke()` writes outcome="error" before
    raising, so no claimed-but-undefined state is observable.

    To retry: `call.copy()` — returns a fresh, un-invoked ToolCall carrying
    the same arguments and handler.

    Equality is structural over (id, name, arguments, outcome, result, error);
    handler binding, invocation state, and the internal lock do NOT participate.
    Intended use is test assertions against freshly-constructed expectations:

        assert reply.tool_calls[0] == ToolCall(
            id="call_abc", name="search", arguments={"q": "x"},
            outcome="ok", result=[...],
        )

    Caveat: `error` participates in eq, but Python's default exception
    equality is identity-based. Two ToolCalls with outcome="error" and
    "equivalent" exceptions will NOT compare equal unless the same exception
    instance is shared. Tests wanting to assert on the error state should
    inspect `tc.outcome` and `type(tc.error)` directly:

        assert tc.outcome == "error" and type(tc.error) is ValueError

    Hashing is disabled (`__hash__ = None`): lifecycle fields mutate after
    construction, so set/dict keying would silently break. Non-frozen
    @dataclass defaults __hash__ to None; we spell it out as a load-bearing
    decision.
    """

    # --- User-facing fields: participate in structural equality. -----------
    id: str
    name: str
    arguments: dict
    outcome: Literal["ok", "error"] | None = None
    result: Any = None
    error: BaseException | None = None

    # --- Internal bookkeeping: excluded from __eq__ and __repr__. ----------
    _handler: Callable[..., Any] | None = field(
        default=None, repr=False, compare=False,
    )
    _invoked: bool = field(default=False, repr=False, compare=False)
    _lock: threading.Lock = field(
        default_factory=threading.Lock, repr=False, compare=False,
    )

    # Explicit: structural equality, never hashable.
    __hash__ = None  # type: ignore[assignment]

    # ---------------------------------------------------------------------
    def _claim(self) -> None:
        """Atomically claim this ToolCall for invocation (INV-TOOLCALL-ONCE).

        The lock is held only for the check-and-set, NOT for handler
        execution, so parallel invocations of DIFFERENT ToolCalls are not
        serialized (INV-PARALLEL-SAFE).
        """
        with self._lock:
            if self._invoked:
                raise ToolAlreadyInvoked(
                    f"ToolCall id={self.id!r} name={self.name!r} "
                    "already invoked; call .copy() to retry."
                )
            self._invoked = True

    # ---------------------------------------------------------------------
    def invoke(self, **override: Any) -> Any:
        """Synchronously invoke the bound handler with `arguments`.

        Raises `LipasError` if no handler is bound or if the handler is a
        coroutine function — use `ainvoke()` for the latter. Both error
        exits set `outcome="error"` and `error`, per the internal invariant.
        """
        self._claim()
        if self._handler is None:
            err = LipasError(
                f"ToolCall {self.name!r} has no handler bound; cannot invoke."
            )
            self.error = err
            self.outcome = "error"
            raise err
        args = {**self.arguments, **override}
        try:
            result = self._handler(**args)
        except BaseException as e:
            self.error = e
            self.outcome = "error"
            raise
        if inspect.iscoroutine(result):
            result.close()
            err = LipasError(
                f"tool {self.name!r} returned a coroutine; "
                "use ainvoke() instead of invoke()."
            )
            self.error = err
            self.outcome = "error"
            raise err
        self.result = result
        self.outcome = "ok"
        return result

    # ---------------------------------------------------------------------
    async def ainvoke(self, **override: Any) -> Any:
        """Asynchronously invoke the bound handler.

        Accepts both sync and async handlers. INV-TOOLCALL-ONCE applies
        across both `invoke` and `ainvoke`.
        """
        self._claim()
        if self._handler is None:
            err = LipasError(
                f"ToolCall {self.name!r} has no handler bound; cannot ainvoke."
            )
            self.error = err
            self.outcome = "error"
            raise err
        args = {**self.arguments, **override}
        try:
            result = self._handler(**args)
            if inspect.iscoroutine(result):
                result = await result
        except BaseException as e:
            self.error = e
            self.outcome = "error"
            raise
        self.result = result
        self.outcome = "ok"
        return result

    # ---------------------------------------------------------------------
    def copy(self) -> "ToolCall":
        """Return a fresh, un-invoked copy — same id, name, arguments, handler."""
        return ToolCall(
            id=self.id,
            name=self.name,
            arguments=dict(self.arguments),
            _handler=self._handler,
            # _invoked and _lock are fresh via field defaults — intentional.
        )


# =============================================================================
# Reply — the central result type. kw_only to allow required `provider` field.
# =============================================================================
@dataclass(frozen=True, slots=True, kw_only=True)
class Reply:
    """A normalized assistant reply from any provider (INV-FROZEN-REFS).

    Construction is keyword-only — both because the field set is wide enough
    that positional args would be unreadable, and because making `provider`
    required forces adapter authors (and stub-test authors) to state it
    explicitly rather than rely on a sentinel.

    The frozen-ness applies to the REFERENCE SET: `thinking` and `tool_calls`
    are tuples so their membership is immutable. Individual `ToolCall` objects
    inside remain mutable — their `outcome` / `result` / `error` are written
    by `invoke()`. This is intentional: ToolCall lifecycle crosses the Reply
    construction moment.

    Provider-native escape hatches:
      - `provider_message`:       the full native response dict (adapter-facing).
      - `native_content_blocks`:  the native `content` portion, reused by
                                  `as_message()` to populate `_lipas.content_blocks`
                                  (INV-ROUND-TRIP).

    Note on ordering: the split into (text / thinking / tool_calls) flattens
    the provider's interleaved content-block sequence. For tracing or UIs
    that need the original order, read `provider_message` directly.
    """

    # Required.
    provider: str

    # Defaulted — keyword-only so order is documentation, not API.
    text: str = ""
    thinking: tuple[ThinkingBlock, ...] = ()
    tool_calls: tuple[ToolCall, ...] = ()
    stop_reason: StopReason = "end_turn"
    usage: Usage = field(default_factory=Usage)
    response_id: str | None = None
    provider_message: dict | None = None
    native_content_blocks: list | None = None
    latency_ms: float | None = None
    ttft_ms: float | None = None  # streaming-only; None on non-streaming path

    # ---------------------------------------------------------------------
    def as_message(self) -> Message:
        """Return an OpenAI-format assistant message for append-and-continue.

        Normalization rules:
          - `content` is `None` when `text == ""` (matches the OpenAI server's
            own assistant-message serialization; required for byte-equivalent
            round-trip under INV-ROUND-TRIP).
          - `tool_calls` are serialized per the OpenAI chat-completions schema
            (arguments re-serialized to a JSON string).
          - `_lipas` sidecar is attached iff `native_content_blocks` is set
            AND `provider` is in `KNOWN_PROVIDERS`. Unknown providers emit
            `LipasUnknownProviderWarning` — the most common cause is a typo
            (`"opeani"`), which otherwise silently disables round-trip.

        Safe to `json.dumps(...)`: the `_lipas` value is pure JSON-serializable
        data.
        """
        msg: Message = {
            "role": "assistant",
            "content": self.text if self.text else None,
        }
        if self.tool_calls:
            msg["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.name,
                        "arguments": json.dumps(tc.arguments, ensure_ascii=False),
                    },
                }
                for tc in self.tool_calls
            ]
        if self.native_content_blocks is not None:
            if self.provider in KNOWN_PROVIDERS:
                attach_lipas(
                    msg,
                    provider=self.provider,
                    content_blocks=list(self.native_content_blocks),
                )
            else:
                warnings.warn(
                    f"lipas: unknown provider {self.provider!r}; _lipas "
                    "sidecar not attached. If this is a typo, fix it; if "
                    "it is a new provider, add it to `lipas.KNOWN_PROVIDERS` "
                    "or attach the sidecar manually via `lipas.attach_lipas(...)`.",
                    LipasUnknownProviderWarning,
                    stacklevel=2,
                )
        return msg


# =============================================================================
# Stream events (INV-FROZEN-REFS)
# =============================================================================
class StreamEvent:
    """Base class for lipas streaming events. Not instantiated directly.

    `__slots__ = ()` is an intent marker only — dataclass subclasses must opt
    into slots themselves via `@dataclass(frozen=True, slots=True)`. v1 does
    so for all event subclasses for allocation-throughput on hot streams.
    """

    __slots__ = ()


@dataclass(frozen=True, slots=True)
class TextDelta(StreamEvent):
    """Incremental visible-text token(s)."""

    text: str


@dataclass(frozen=True, slots=True)
class ThinkingDelta(StreamEvent):
    """Incremental reasoning token(s).

    Rendered in a separate visual channel so users looking at extended
    thinking don't watch a blank screen. If the consumer doesn't care about
    the distinction:

        if isinstance(ev, (TextDelta, ThinkingDelta)): print(ev.text, end="")

    `signature` semantics (v1 — two fields share one event, by design, to
    keep the event taxonomy narrow):

      - `signature is None`  → mid-block delta. More deltas will follow
                               within the same thinking block.
      - `signature is not None` → TERMINAL delta of the current thinking
                               block. `text` may also be non-empty (trailing
                               tokens of the block); the block ends here.

    Downstream consumers that need a strict block-boundary event should
    coalesce by treating a non-None signature as "block close". The
    signature itself is opaque and round-tripped via `_lipas.content_blocks`.
    """

    text: str
    signature: str | None = None


@dataclass(frozen=True, slots=True)
class ToolCallStart(StreamEvent):
    """Emitted as soon as the model commits to a tool call (name + id known).

    Use this to show "calling search..." in a UI. Arguments are still
    streaming — the fully-parsed ToolCall arrives in ToolCallReady.
    """

    id: str
    name: str


@dataclass(frozen=True, slots=True)
class ToolCallReady(StreamEvent):
    """Emitted when a tool call's arguments have been fully received and parsed
    (INV-TOOLCALL-PARSABLE). The `call` is ready to `invoke()`.

    Calls with unparsable JSON arguments are dropped — they never produce a
    ToolCallReady event (but the corresponding ToolCallStart may have been
    emitted earlier in the stream).
    """

    call: ToolCall


@dataclass(frozen=True, slots=True)
class Done(StreamEvent):
    """Terminal event on a successful stream. Always the LAST event emitted.

    `reply` is field-equivalent to the Reply returned by the non-streaming
    path (INV-STREAM-EQUIV). On failure (provider error, network drop,
    timeout), Done is NOT emitted; `LipasStreamError` is raised out of the
    async generator.
    """

    reply: Reply


@dataclass(frozen=True, slots=True)
class ProviderEvent(StreamEvent):
    """Escape hatch for native provider events lipas does not recognize
    (INV-PROVIDEREVENT-FALLBACK).

    For each native event, lipas emits EITHER a typed event OR a
    ProviderEvent — never both. Typed and provider events may interleave
    within a single stream.
    """

    raw: dict
