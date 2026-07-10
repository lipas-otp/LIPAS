"""
LIPAS · llm.py — sketch-style surface over LLMHarness + ToolHarness.

HARD RULE: this file is SURFACE. It imports from harness / tool_harness /
adapter / tools / replay / replay_tools / guard / rows. Those modules
MUST NOT import from this one. surface ≠ stack.

This module:
  - computes nothing about idempotency, retry, replay, budget, guards, or spend
  - folds no claims
  - holds no audit-trail state

All of that is done by LLMHarness / ToolHarness internally. We provide:

  LLM       — callable wrapper enabling
                reply = await llm(messages, tools=[...])
                for c in reply.tool_calls: await c.invoke()
  Reply     — projection of adapter.Reply (.text / .tool_calls / ...).
  ToolCall  — one tool_use block + back-ref to the bound ToolHarness.

Per-call policy (locked):
  B7 — `tools=` per-call fully REPLACES the construction-time default
       (tools=None  → use default;
        tools=[]    → explicit no-tools call;
        tools=[...] → replace).
  B8 — adapter returns tool_use blocks but no ToolHarness was wired
       → raise RuntimeError (configuration bug, not silent).
  B9 — reply.stop_reason == "error" → tool_calls returns ().
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

from lipas.adapter import Reply as AdapterReply, Request
from lipas.adapter.content import TextBlock, ToolUseBlock
from lipas.adapter.errors import DEFAULT_POLICY, ErrorKind, RetryPolicy
from lipas.adapter.protocol import LLMAdapter
from lipas.adapter.streaming import StreamEvent
from lipas.guard import Guard
from lipas.harness import BucketExtractor, LLMHarness, default_bucket_extractor
from lipas.replay import ReplayCursor
from lipas.replay_tools import ToolReplayer
from lipas.rows import RowSet
from lipas.tool_harness import ToolHarness
from lipas.tools import Tool, ToolRegistry

__all__ = ["LLM", "Reply", "ToolCall"]


# =====================================================================
# Block helpers — defensive over (ContentBlock dataclass | dict)
# =====================================================================

def _block_type(b: Any) -> str:
    if isinstance(b, Mapping):
        return b.get("type", "")
    return getattr(b, "type", "")


def _block_get(b: Any, key: str, default: Any = None) -> Any:
    if isinstance(b, Mapping):
        return b.get(key, default)
    return getattr(b, key, default)


def _block_to_dict(b: Any) -> dict:
    """Pure shape conversion: ContentBlock dataclass → wire-shape dict.
    Dicts pass through as a defensive copy. Forward-compat path goes
    through vars() for unknown block types."""
    if isinstance(b, Mapping):
        return dict(b)
    if isinstance(b, TextBlock):
        return {"type": "text", "text": b.text}
    if isinstance(b, ToolUseBlock):
        return {
            "type":  "tool_use",
            "id":    b.id,
            "name":  b.name,
            "input": dict(b.input),
        }
    return {k: v for k, v in vars(b).items() if not k.startswith("_")}


# =====================================================================
# Reply / ToolCall — projections, not new state
# =====================================================================

@dataclass(frozen=True)
class ToolCall:
    """One tool_use block bound to the ToolHarness that will execute it.

    `id`/`name`/`arguments` are extracted verbatim from the tool_use
    block. `invoke()` routes through ToolHarness.call(...) with
    `effect_id=self.id`, so tool_result.tool_use_id round-trips.
    """
    id:        str
    name:      str
    arguments: Mapping[str, Any]
    _harness:  ToolHarness = field(repr=False, compare=False)

    async def invoke(self, *, compensates: str | None = None) -> Any:
        """Execute through the bound ToolHarness. The return value is
        whatever ToolHarness.call returns (typically the tool_result
        block payload to feed back to the LLM). Replay / idempotency /
        spend are entirely the harness's business."""
        return await self._harness.call(
            tool_name=self.name,
            arguments=dict(self.arguments),
            effect_id=self.id,
            compensates=compensates,
        )


@dataclass(frozen=True)
class Reply:
    """Sketch-style view over an adapter.Reply.

    The underlying Reply is preserved verbatim on `.raw`. Nothing
    here triggers any further fold / call.
    """
    _raw:          AdapterReply        = field(repr=False)
    _tool_harness: ToolHarness | None  = field(repr=False, compare=False)

    # ── content projections ────────────────────────────────────

    @property
    def text(self) -> str:
        """Concatenation of every text block's `.text`. Empty string
        if no text blocks (including the all-tool_use case)."""
        return "".join(
            _block_get(b, "text", "")
            for b in self._raw.content
            if _block_type(b) == "text"
        )

    @property
    def tool_calls(self) -> tuple[ToolCall, ...]:
        """Tool-use blocks projected as ToolCall, bound to this
        Reply's ToolHarness.

        B9: error replies → ().
        B8: tool_use found but no harness → RuntimeError.
        """
        if self._raw.stop_reason == "error":
            return ()
        uses = [b for b in self._raw.content if _block_type(b) == "tool_use"]
        if not uses:
            return ()
        if self._tool_harness is None:
            raise RuntimeError(
                f"adapter returned {len(uses)} tool_use block(s) but this "
                f"LLM was constructed without tools. Pass tools=[...] to "
                f"LLM(...) at construction or as a per-call override."
            )
        return tuple(
            ToolCall(
                id        = _block_get(u, "id"),
                name      = _block_get(u, "name"),
                arguments = _block_get(u, "input", {}) or {},
                _harness  = self._tool_harness,
            )
            for u in uses
        )

    # ── pass-through fields ────────────────────────────────────

    @property
    def stop_reason(self)  -> str:                         return self._raw.stop_reason
    @property
    def is_error(self)     -> bool:                        return self._raw.stop_reason == "error"
    @property
    def error_detail(self) -> Mapping[str, Any] | None:    return self._raw.error_detail
    @property
    def usage(self):                                       return self._raw.usage
    @property
    def model(self)        -> str:                         return self._raw.model
    @property
    def raw(self)          -> AdapterReply:                return self._raw

    # ── conversation-building convenience ──────────────────────

    def as_assistant_message(self) -> dict:
        """Turn this reply into a `{"role": "assistant", "content": [...]}`
        dict suitable for the next call's `messages`. Pure shape
        conversion; no folding."""
        return {
            "role":    "assistant",
            "content": [_block_to_dict(b) for b in self._raw.content],
        }


# =====================================================================
# LLM — the callable wrapper
# =====================================================================

@dataclass
class LLM:
    """Sketch-style callable over (LLMHarness, ToolHarness).

    Construction wires both harnesses over the SAME rowset.

    Example:
        @tool(side_effect=SideEffectClass.READ_ONLY)
        def search(query: str) -> str:
            \"\"\"Search the web.\"\"\"
            ...

        llm = LLM(adapter=adapter, rowset=rowset, model="claude-...",
                  tools=[search])

        reply = await llm(messages)
        for c in reply.tool_calls:
            result = await c.invoke()
            ...
    """

    # ── stack handles ──────────────────────────────────────────
    adapter:    LLMAdapter
    rowset:     RowSet
    model:      str
    max_tokens: int = 4096

    # ── Request defaults (per-call overridable) ────────────────
    system:         str           = ""
    tools:          Sequence[Tool] = ()
    temperature:    float | None  = None
    stop_sequences: Sequence[str] = ()

    # ── forwarded to LLMHarness ────────────────────────────────
    guards:           Sequence[Guard]                 = ()
    retry_policy:     Mapping[ErrorKind, RetryPolicy] = field(
        default_factory=lambda: DEFAULT_POLICY,
    )
    bucket_extractor: BucketExtractor                 = field(
        default=default_bucket_extractor,
    )
    replay_cursor:    ReplayCursor | None             = None

    # ── forwarded to ToolHarness ───────────────────────────────
    tool_guards:   Sequence[Guard]      = ()
    tool_replayer: ToolReplayer | None  = None

    # ── internal ───────────────────────────────────────────────
    _llm_harness: LLMHarness         = field(init=False, repr=False)
    _default_th:  ToolHarness | None = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._llm_harness = LLMHarness(
            adapter          = self.adapter,
            rowset           = self.rowset,
            guards           = self.guards,
            retry_policy     = self.retry_policy,
            bucket_extractor = self.bucket_extractor,
            replay_cursor    = self.replay_cursor,
        )
        self._default_th = self._build_tool_harness(self.tools)

    # ── public API ─────────────────────────────────────────────

    async def __call__(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        tools:          Sequence[Tool] | None = None,
        model:          str | None            = None,
        max_tokens:     int | None            = None,
        system:         str | None            = None,
        temperature:    float | None          = None,
        stop_sequences: Sequence[str] | None  = None,
        compensates:    str | None            = None,
    ) -> Reply:
        """One LLM call.

        Steps (every step is pure assembly / projection):
          1. Resolve effective tools (B7: per-call fully replaces).
          2. Pick / build the matching ToolHarness.
          3. Build a Request from messages + effective fields.
          4. Delegate to LLMHarness.call(request, compensates=...).
          5. Wrap the adapter.Reply into our Reply projection.

        No fold, no replay decision, no idempotency, no spend, no
        retry done at this layer.
        """
        # B7 — explicit None vs explicit []:
        #   tools is None → use construction-time default
        #   tools is [...] (incl. []) → replace, build a fresh harness
        if tools is None:
            effective_tools = self.tools
            harness         = self._default_th
        else:
            effective_tools = tools
            harness         = self._build_tool_harness(tools)

        request = self._build_request(
            messages,
            effective_tools = effective_tools,
            model           = model,
            max_tokens      = max_tokens,
            system          = system,
            temperature     = temperature,
            stop_sequences  = stop_sequences,
        )
        adapter_reply = await self._llm_harness.call(
            request, compensates=compensates,
        )
        return Reply(_raw=adapter_reply, _tool_harness=harness)

    async def stream(
        self, messages: Sequence[Mapping[str, Any]], **kwargs: Any,
    ) -> AsyncIterator[StreamEvent]:
        """Yield token/tool deltas to the caller and durably fold final Done.

        Keyword arguments mirror ``__call__`` except ``compensates`` is also
        accepted. Streaming does not retry after a visible event.
        """
        tools = kwargs.pop("tools", None)
        effective = self.tools if tools is None else tools
        request = self._build_request(messages, effective_tools=effective,
            model=kwargs.pop("model", None), max_tokens=kwargs.pop("max_tokens", None),
            system=kwargs.pop("system", None), temperature=kwargs.pop("temperature", None),
            stop_sequences=kwargs.pop("stop_sequences", None))
        if kwargs:
            raise TypeError(f"unexpected stream keyword(s): {', '.join(kwargs)}")
        async for event in self._llm_harness.stream(request):
            yield event

    # ── internals ──────────────────────────────────────────────

    def _build_request(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        effective_tools: Sequence[Tool],
        model:           str | None,
        max_tokens:      int | None,
        system:          str | None,
        temperature:     float | None,
        stop_sequences:  Sequence[str] | None,
    ) -> Request:
        return Request(
            model       = model       if model       is not None else self.model,
            messages    = list(messages),
            max_tokens  = max_tokens  if max_tokens  is not None else self.max_tokens,
            system      = system      if system      is not None else self.system,
            tools       = tuple(self._tool_to_spec(t) for t in effective_tools),
            temperature = temperature if temperature is not None else self.temperature,
            stop_sequences = (
                tuple(stop_sequences) if stop_sequences is not None
                else tuple(self.stop_sequences)
            ),
        )

    def _build_tool_harness(
        self, tools: Sequence[Tool],
    ) -> ToolHarness | None:
        """Build a ToolHarness over a fresh ToolRegistry, or return
        None when no tools are wired (empty registry would still work,
        but None lets Reply.tool_calls give a precise B8 error)."""
        if not tools:
            return None
        return ToolHarness(
            tools         = ToolRegistry(tools),
            rowset        = self.rowset,
            guards        = self.tool_guards,
            tool_replayer = self.tool_replayer,
        )

    @staticmethod
    def _tool_to_spec(t: Tool) -> dict:
        """Tool → Anthropic-shape tool spec dict.
        Request.tools accepts Mapping, so we emit dicts directly
        rather than going through ToolSpec."""
        return {
            "name":         t.name,
            "description":  t.description,
            "input_schema": t.parameters_schema,
        }
