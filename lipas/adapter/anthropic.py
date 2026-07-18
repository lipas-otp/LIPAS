"""Anthropic Messages adapter.

It implements the normalized LLMAdapter protocol with an injected Anthropic
client. The current transport is single-shot: ``messages.create()`` completes
before the adapter yields its one terminal ``Done`` event. Request and reply
content use the provider-neutral, dictionary-shaped blocks defined by
``lipas.adapter.types``.

Provider, transport, and runtime failures become a terminal
``Reply(stop_reason='error', error_detail=...)``; only cancellation
propagates. This keeps the effect lifecycle auditable regardless of provider.
"""
from __future__ import annotations

import asyncio
import inspect
from copy import deepcopy
import logging
from collections.abc import Mapping
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

    name: str = "anthropic"

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
        try:
            params = self._build_params(request)
            response = await self.client.messages.create(**params)
            reply = self._reply_from_response(request, response)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            yield Done(reply=self._error_reply(request, exc))
            return

        yield Done(reply=reply)

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
        messages = self._anthropic_messages(request.messages)

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
                dict(t) if isinstance(t, Mapping) else {
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

    @staticmethod
    def _anthropic_messages(messages: Any) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for message in messages:
            role = message.get("role", "user")
            content = message.get("content", "")
            if isinstance(content, str):
                out.append({"role": role, "content": content})
                continue
            blocks: list[Any] = []
            for raw in content:
                if not isinstance(raw, Mapping):
                    blocks.append(raw)
                    continue
                block = deepcopy(dict(raw))
                if block.get("type") == "tool_result":
                    tool_id = block.pop("tool_call_id", None)
                    if "tool_use_id" not in block and tool_id is not None:
                        block["tool_use_id"] = tool_id
                blocks.append(block)
            out.append({"role": role, "content": blocks})
        return out

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
                tool_id = getattr(block, "id", None)
                tool_name = getattr(block, "name", None)
                tool_input = getattr(block, "input", None)
                if (
                    not isinstance(tool_id, str)
                    or not tool_id
                    or not isinstance(tool_name, str)
                    or not tool_name
                    or not isinstance(tool_input, Mapping)
                ):
                    raise ValueError("Anthropic returned a malformed tool_use block")
                content.append({
                    "type":  "tool_use",
                    "id":    tool_id,
                    "name":  tool_name,
                    "input": dict(tool_input),
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
        usage = self._usage_from_response(response)
        model = getattr(response, "model", request.model) or request.model
        if sr_raw not in _STOP_REASON_MAP:
            logger.warning(
                "anthropic adapter: unknown stop_reason=%r, "
                "surfaced as a terminal provider error.", sr_raw,
            )
            return Reply(
                content=tuple(content),
                usage=usage,
                stop_reason="error",
                model=model,
                error_detail={
                    "type": "provider_error",
                    "provider_error": {
                        "type": "unknown_stop_reason",
                        "stop_reason": sr_raw,
                    },
                },
            )
        stop_reason: StopReason = _STOP_REASON_MAP.get(sr_raw, "end_turn")

        return Reply(
            content=tuple(content),
            usage=usage,
            stop_reason=stop_reason,
            model=model,
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
                        raw_body = json_meth()
                        body_dict = (
                            dict(raw_body) if isinstance(raw_body, Mapping) else {}
                        )
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
                counted = ct(**params)
                resp = await counted if inspect.isawaitable(counted) else counted
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
            if isinstance(t, Mapping):
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
