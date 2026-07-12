"""
LIPAS · P3.3 — Anthropic adapter.

Implements LLMAdapter (streaming Protocol) against Anthropic's
Messages API. First cut is non-streaming under the hood: messages.create()
runs to completion, then the adapter yields exactly one Done event.
This satisfies the Protocol contract; a true SSE variant can replace
stream()'s body without touching estimate_cost or the translators.

Runtime content shape
---------------------
Both inbound (Request.messages[*].content) and outbound
(Reply.content) blocks are Anthropic-shaped dicts at runtime, NOT
the dataclasses declared in content.py. This matches what
ReActAgent actually passes and parses this shape. See module
backlog: B-?? "reconcile content type signatures with runtime shape".

Error contract (LOCKED — see protocol.py)
-----------------------------------------
All provider/transport/runtime failures emerge as a terminal Done
carrying Reply(stop_reason='error', error_detail=...). error_detail
conforms to one of the TypedDicts in lipas.adapter.errors so
classify() returns a real ErrorKind (not UNKNOWN) and retry.py
can act on it. Only CancelledError propagates.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, AsyncIterator, ClassVar

from .types import (
    Done, PriceTable, Reply, Request, ResourceEstimate, StopReason,
    StreamEvent, Usage,
)


__all__ = ["AnthropicAdapter"]

logger = logging.getLogger(__name__)


# Anthropic stop_reason -> StopReason (1:1 today, explicit so a new
# Anthropic value fails loud rather than silently coercing).
_STOP_REASON_MAP: dict[str, StopReason] = {
    "end_turn":      "end_turn",
    "max_tokens":    "max_tokens",
    "stop_sequence": "stop_sequence",
    "tool_use":      "tool_use",
}

# Exception class names treated as network/timeout when the SDK
# exception lacks a status_code. Match by name to avoid hard-importing
# httpx/anthropic at module level.
_NETWORK_EXC_NAMES = frozenset({
    "APIConnectionError", "APITimeoutError",
    "ConnectError", "ConnectTimeout",
    "ReadTimeout", "WriteTimeout", "PoolTimeout",
    "TimeoutException",
})


class AnthropicAdapter:
    """Anthropic Messages API adapter — implements LLMAdapter."""

    name: ClassVar[str] = "anthropic"

    def __init__(
        self,
        client: Any,
        prices: PriceTable,
        *,
        name: str = "anthropic",
    ) -> None:
        if not hasattr(client, "messages") or not hasattr(
            client.messages, "create"
        ):
            raise TypeError(
                f"AnthropicAdapter expects an async Anthropic client "
                f"with .messages.create; got {type(client).__name__}"
            )
        self.client = client
        self.prices = prices
        self.name = name  # per-instance override of class default

    # ── LLMAdapter.stream ──────────────────────────────────────

    async def stream(
        self, request: Request,
    ) -> AsyncIterator[StreamEvent]:
        """Single-shot under the hood; yields exactly one Done.

        Per the LOCKED error contract: provider/transport failures
        surface as Done(reply=Reply(stop_reason='error', ...)).
        Only CancelledError propagates.
        """
        params = self._build_params(request)

        try:
            response = await self.client.messages.create(**params)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            yield Done(reply=self._error_reply(request, exc))
            return

        yield Done(reply=self._reply_from_response(request, response))

    # ── LLMAdapter.estimate_cost ───────────────────────────────

    async def estimate_cost(self, request: Request) -> ResourceEstimate:
        """Worst-case upper bound: counted input + max_tokens output,
        priced via the injected PriceTable.

        UnknownModelError propagates; admission policy decides what
        to do with it.
        """
        price = self.prices.for_model(request.model)
        input_tokens = await self._count_input_tokens(request)
        max_output = request.max_tokens

        # Worst case: all output is non-cached output tokens.
        worst = Usage(input=input_tokens, output=max_output)
        return ResourceEstimate(
            model=request.model,
            input_tokens=input_tokens,
            max_output_tokens=max_output,
            max_cost_usd=price.cost(worst),
        )

    # ── request translation ───────────────────────────────────

    def _build_params(self, request: Request) -> dict[str, Any]:
        # Request.messages is already provider-neutral, dict-shaped data.
        messages = list(request.messages)

        params: dict[str, Any] = {
            "model":      request.model,
            "messages":   messages,
            "max_tokens": request.max_tokens,
        }
        # Request.system defaults to "" (not None); skip empty.
        if request.system:
            params["system"] = request.system
        if request.tools:
            # Request.tools is Sequence[ToolSpec | dict]; ReActAgent
            # currently passes dicts via _get_tool_descriptors, so
            # accept both.
            params["tools"] = [
                t if isinstance(t, dict) else {
                    "name":         t.name,
                    "description":  t.description,
                    "input_schema": dict(t.input_schema),
                }
                for t in request.tools
            ]
        if request.temperature is not None:
            params["temperature"] = request.temperature
        if request.stop_sequences:
            params["stop_sequences"] = list(request.stop_sequences)

        # Provider-specific extras; caller owns SDK-version compat.
        for k, v in request.extra.items():
            params.setdefault(k, v)

        return params

    # ── reply translation ─────────────────────────────────────

    def _reply_from_response(
        self, request: Request, response: Any,
    ) -> Reply:
        """SDK response → Reply. Content blocks normalized to
        Anthropic-shape dicts (see module docstring for why)."""
        content: list[dict[str, Any]] = []
        for block in getattr(response, "content", ()) or ():
            btype = getattr(block, "type", None)
            if btype == "text":
                content.append({
                    "type": "text",
                    "text": getattr(block, "text", "") or "",
                })
            elif btype == "tool_use":
                content.append({
                    "type":  "tool_use",
                    "id":    getattr(block, "id", "") or "",
                    "name":  getattr(block, "name", "") or "",
                    "input": dict(getattr(block, "input", {}) or {}),
                })
            else:
                logger.warning(
                    "anthropic adapter: unrecognised content block "
                    "type=%r — passing through opaquely. Add first-"
                    "class handling if this becomes load-bearing.",
                    btype,
                )
                content.append({"type": btype or "unknown",
                                "raw":  repr(block)})

        sr_raw = getattr(response, "stop_reason", None) or ""
        stop_reason: StopReason = _STOP_REASON_MAP.get(sr_raw, "end_turn")
        if sr_raw and sr_raw not in _STOP_REASON_MAP:
            logger.warning(
                "anthropic adapter: unknown stop_reason=%r, "
                "coerced to 'end_turn'.", sr_raw,
            )

        return Reply(
            content=tuple(content),
            usage=self._usage_from_response(response),
            stop_reason=stop_reason,
            model=getattr(response, "model", request.model) or request.model,
            error_detail=None,
        )

    @staticmethod
    def _usage_from_response(response: Any) -> Usage:
        u = getattr(response, "usage", None)
        if u is None:
            return Usage()

        def _g(name: str) -> int:
            v = getattr(u, name, None)
            return int(v) if isinstance(v, int) and v >= 0 else 0

        return Usage(
            input=_g("input_tokens"),
            output=_g("output_tokens"),
            cache_read=_g("cache_read_input_tokens"),
            cache_write=_g("cache_creation_input_tokens"),
        )

    def _error_reply(self, request: Request, exc: Exception) -> Reply:
        """Build stop_reason='error' Reply per the LOCKED contract.

        error_detail MUST conform to one of the TypedDicts consumed
        by lipas.adapter.errors.classify() — http_error /
        network_error / provider_error — otherwise classify() returns
        UNKNOWN and the call is treated as non-retryable.
        """
        cls = type(exc).__name__
        sc = getattr(exc, "status_code", None)

        detail: dict[str, Any]
        if isinstance(sc, int):
            body_dict: dict[str, Any] = {}
            resp = getattr(exc, "response", None)
            if resp is not None:
                json_meth = getattr(resp, "json", None)
                if callable(json_meth):
                    try:
                        body_dict = json_meth() or {}
                    except Exception:  # pragma: no cover
                        body_dict = {}
            # Some SDKs attach the parsed body directly.
            body_attr = getattr(exc, "body", None)
            if not body_dict and isinstance(body_attr, dict):
                body_dict = body_attr
            detail = {
                "type":        "http_error",
                "status_code": sc,
                "body":        body_dict,
            }
        elif cls in _NETWORK_EXC_NAMES:
            detail = {
                "type":           "network_error",
                "exception_type": cls,
                "message":        str(exc) or cls,
            }
        else:
            # Unknown exception with no status_code: try to surface
            # any structured provider payload so classify() can pick
            # up rate_limit_error / overloaded_error / etc.
            provider_err: dict[str, Any] = {
                "type":    cls,
                "message": str(exc) or cls,
            }
            body_attr = getattr(exc, "body", None)
            if isinstance(body_attr, dict):
                err_section = body_attr.get("error")
                if isinstance(err_section, dict):
                    provider_err = err_section
            detail = {
                "type":           "provider_error",
                "provider_error": provider_err,
            }

        logger.warning(
            "anthropic adapter: %s%s -> %s",
            cls,
            f" (status={sc})" if sc is not None else "",
            detail["type"],
        )

        return Reply(
            content=(),
            usage=Usage(),
            stop_reason="error",
            model=request.model,
            error_detail=detail,
        )

    # ── token counting ────────────────────────────────────────

    _fallback_warned: ClassVar[bool] = False

    async def _count_input_tokens(self, request: Request) -> int:
        """Prefer SDK count_tokens; fall back to chars/4 with a
        once-per-process warning. estimate_cost callers depend on
        knowing whether the upper bound is real or coarse."""
        ct = getattr(self.client.messages, "count_tokens", None)
        if callable(ct):
            try:
                params = self._build_params(request)
                params.pop("max_tokens", None)
                resp = await ct(**params)
                tokens = getattr(resp, "input_tokens", None)
                if isinstance(tokens, int) and tokens >= 0:
                    return tokens
            except Exception as exc:  # pragma: no cover
                logger.warning(
                    "anthropic count_tokens failed (%s); using "
                    "char-based fallback for this call.", exc,
                )

        return self._fallback_token_estimate(request)

    def _fallback_token_estimate(self, request: Request) -> int:
        if not type(self)._fallback_warned:
            logger.warning(
                "anthropic adapter: chars/4 fallback active for "
                "input token counting; estimate_cost upper bound is "
                "imprecise. Wire a real count_tokens for production."
            )
            type(self)._fallback_warned = True

        chars = len(request.system or "")
        for msg in request.messages:
            content = msg.get("content", "") if isinstance(msg, dict) else ""
            if isinstance(content, str):
                chars += len(content)
            elif isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    btype = block.get("type")
                    if btype == "text":
                        chars += len(block.get("text", "") or "")
                    elif btype == "tool_use":
                        chars += len(str(block.get("input", {})))
                    elif btype == "tool_result":
                        c = block.get("content", "")
                        if isinstance(c, str):
                            chars += len(c)
                        elif isinstance(c, list):
                            for sub in c:
                                if isinstance(sub, dict):
                                    chars += len(sub.get("text", "") or "")

        for t in request.tools or ():
            if isinstance(t, dict):
                chars += (
                    len(t.get("name", "")) + len(t.get("description", ""))
                    + len(str(t.get("input_schema", {})))
                )
            else:
                chars += (
                    len(t.name) + len(t.description)
                    + len(str(dict(t.input_schema)))
                )

        # +16 for structural tokens (role markers, JSON braces).
        return max(1, chars // 4 + 16)
