"""
LIPAS · OllamaAdapter — local-Ollama backend for LLMHarness
===========================================================

Targets the Ollama HTTP API (default endpoint http://localhost:11434).
First cut is non-streaming under the hood: /api/chat with stream=false
runs to completion, then the adapter yields exactly one Done event.
This satisfies the Protocol contract identically to AnthropicAdapter.

Why Ollama before a real cloud API?
-----------------------------------
v0.0.3's goal is to validate the adapter contract end-to-end without
the cross-cutting risks of cloud calls (auth, billing, regional
network, rate limits). Ollama factors those out: a working Ollama
session is a strong signal that LLMHarness's pre-flight / fold /
replay machinery is correct.

Tool use (P3.1)
---------------
``request.tools`` (Anthropic-shape: name / description / input_schema)
is translated to Ollama's OpenAI-shape ``tools`` field on the way in,
and ``message.tool_calls`` in the response is translated back into
Anthropic-shape ``tool_use`` content blocks on the way out. The
adapter does NOT execute tools — that is ToolHarness's job. When the
response carries any tool_calls, ``stop_reason`` is forced to
``"tool_use"`` regardless of Ollama's ``done_reason`` (which is
typically ``"stop"`` even when a tool was requested).

Per-model caveat: not every Ollama model honours the tools field.
gemma4, qwen2.5, llama3.1 do; older / smaller variants may silently
ignore it. We do not try to detect this — if a model ignores tools,
the agent will simply observe an answer-without-tool-calls and the
ReAct loop will terminate.

What's deliberately out of scope
--------------------------------
- Real streaming. Same shape as AnthropicAdapter: synchronous under
  the hood, single Done event out. SSE can replace stream()'s body
  without touching estimate_cost or the translators.
- Multimodal content. Text only; images in inbound messages are
  flattened to placeholder text and a warning is logged.

Pricing
-------
Local inference has no USD cost, but operators may still want to
budget on tokens. The adapter accepts an optional ``prices``: when
None (default), ``estimate_cost`` reports max_cost_usd=Decimal(0)
and budget gating works on token buckets only.

Error contract (LOCKED — see protocol.py)
-----------------------------------------
All transport / Ollama-side failures emerge as a terminal Done
carrying Reply(stop_reason='error', error_detail=...). error_detail
conforms to one of the TypedDicts in lipas.adapter.errors so
classify() returns a real ErrorKind (not UNKNOWN) and retry.py can
act on it. Only CancelledError propagates.

Common surfaces:
  - Ollama daemon not running    → network_error (ConnectError)
  - Model not pulled             → http_error 404
  - Request timeout              → network_error (ReadTimeout)
  - 5xx                          → http_error
"""
from __future__ import annotations

import asyncio
import logging
import os
import uuid
from collections.abc import Mapping
from decimal import Decimal
from typing import Any, AsyncIterator, ClassVar

import httpx

from .types import (
    Done, PriceTable, Reply, Request, ResourceEstimate, StopReason,
    StreamEvent, Usage,
)


__all__ = ["OllamaAdapter"]

logger = logging.getLogger(__name__)


# Ollama done_reason → Reply.stop_reason. Closed set; new values fall
# back to "end_turn" with a warning so we notice rather than silently
# coerce. NOTE: when message.tool_calls is non-empty the caller
# overrides this to "tool_use" — Ollama still reports done_reason="stop"
# for tool-requesting turns.
_DONE_REASON_MAP: dict[str, StopReason] = {
    "stop":   "end_turn",
    "length": "max_tokens",
    "load":   "end_turn",
}

_DEFAULT_HOST = "http://localhost:11434"
_DEFAULT_TIMEOUT_S = 500.0


class OllamaAdapter:
    """Ollama HTTP adapter — implements LLMAdapter."""

    name: str = "ollama"

    def __init__(
        self,
        *,
        host: str | None = None,
        prices: PriceTable | None = None,
        timeout_s: float = _DEFAULT_TIMEOUT_S,
        client: httpx.AsyncClient | None = None,
        name: str = "ollama",
    ) -> None:
        self.host = (
            host or os.environ.get("OLLAMA_HOST") or _DEFAULT_HOST
        ).rstrip("/")
        self.prices = prices
        self.timeout_s = timeout_s
        self._client = client
        self.name = name

    # ── LLMAdapter.stream ──────────────────────────────────────

    async def stream(
        self, request: Request,
    ) -> AsyncIterator[StreamEvent]:
        """Single-shot under the hood; yields exactly one Done."""
        try:
            body = self._build_body(request)
            payload = await self._post_chat(body)
            reply = self._reply_from_payload(request, payload)
        except asyncio.CancelledError:
            raise
        except httpx.HTTPStatusError as exc:
            yield Done(reply=self._http_error_reply(request, exc))
            return
        except (httpx.TransportError, httpx.TimeoutException) as exc:
            yield Done(reply=self._network_error_reply(request, exc))
            return
        except Exception as exc:
            yield Done(reply=self._provider_error_reply(
                request,
                err_type=type(exc).__name__,
                message=str(exc) or type(exc).__name__,
            ))
            return

        yield Done(reply=reply)

    # ── LLMAdapter.estimate_cost ───────────────────────────────

    async def estimate_cost(self, request: Request) -> ResourceEstimate:
        input_tokens = self._count_input_tokens(request)
        max_output = request.max_tokens

        if self.prices is None:
            cost = Decimal("0")
        else:
            price = self.prices.for_model(request.model)
            cost = price.cost(Usage(input=input_tokens, output=max_output))

        return ResourceEstimate(
            model=request.model,
            input_tokens=input_tokens,
            max_output_tokens=max_output,
            max_cost_usd=cost,
        )

    # ── HTTP plumbing ─────────────────────────────────────────

    async def _post_chat(self, body: dict[str, Any]) -> dict[str, Any]:
        url = f"{self.host}/api/chat"
        if self._client is not None:
            resp = await self._client.post(
                url, json=body, timeout=self.timeout_s,
            )
        else:
            async with httpx.AsyncClient(timeout=self.timeout_s) as client:
                resp = await client.post(url, json=body)

        if resp.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"ollama returned HTTP {resp.status_code}",
                request=resp.request,
                response=resp,
            )
        return resp.json()

    # ── request translation ───────────────────────────────────

    def _build_body(self, request: Request) -> dict[str, Any]:
        """Translate a lipas Request to Ollama /api/chat body."""
        messages: list[dict[str, Any]] = []
        if request.system:
            messages.append({"role": "system", "content": request.system})
        for msg in request.messages:
            messages.extend(_normalize_messages(msg))

        options: dict[str, Any] = {
            "num_predict": request.max_tokens,
        }
        if request.temperature is not None:
            options["temperature"] = request.temperature
        if request.stop_sequences:
            options["stop"] = list(request.stop_sequences)

        body: dict[str, Any] = {
            "model":    request.model,
            "messages": messages,
            "stream":   False,
            "options":  options,
        }

        # Tool schemas (P3.1). Anthropic-shape in → OpenAI-shape out.
        if request.tools:
            body["tools"] = [_tool_to_ollama(t) for t in request.tools]

        for k, v in request.extra.items():
            body.setdefault(k, v)

        return body

    # ── reply translation ─────────────────────────────────────

    def _reply_from_payload(
        self, request: Request, payload: dict[str, Any],
    ) -> Reply:
        """Ollama JSON → Reply.

        Content blocks follow Anthropic shape so ReActAgent /
        ToolHarness can read them uniformly:
          - text      → {"type": "text", "text": "..."}
          - tool_use  → {"type": "tool_use", "id": ..., "name": ...,
                         "input": {...}}
        """
        message = payload.get("message") or {}
        text = (message.get("content") or "")
        tool_calls = message.get("tool_calls") or []

        blocks: list[dict[str, Any]] = []
        if text:
            blocks.append({"type": "text", "text": text})
        for tc in tool_calls:
            block = _ollama_tool_call_to_block(tc)
            if block is not None:
                blocks.append(block)

        # done_reason → stop_reason, with tool_use override.
        sr_raw = payload.get("done_reason") or "stop"
        usage = Usage(
            input=int(payload.get("prompt_eval_count") or 0),
            output=int(payload.get("eval_count") or 0),
        )
        model = payload.get("model") or request.model
        if tool_calls:
            stop_reason: StopReason = "tool_use"
        else:
            if sr_raw not in _DONE_REASON_MAP:
                logger.warning(
                    "ollama adapter: unknown done_reason=%r, "
                    "surfaced as a terminal provider error.", sr_raw,
                )
                return Reply(
                    content=tuple(blocks),
                    usage=usage,
                    stop_reason="error",
                    model=model,
                    error_detail={
                        "type": "provider_error",
                        "provider_error": {
                            "type": "unknown_done_reason",
                            "done_reason": sr_raw,
                        },
                    },
                )
            stop_reason = _DONE_REASON_MAP[sr_raw]

        return Reply(
            content=tuple(blocks),
            usage=usage,
            stop_reason=stop_reason,
            model=model,
            error_detail=None,
        )

    # ── error reply builders (per LOCKED contract) ────────────

    def _http_error_reply(
        self, request: Request, exc: httpx.HTTPStatusError,
    ) -> Reply:
        sc = exc.response.status_code
        try:
            body = exc.response.json() or {}
        except Exception:
            body = {"raw": exc.response.text}
        if not isinstance(body, dict):
            body = {"raw": body}

        logger.warning(
            "ollama adapter: HTTP %s -> http_error (body=%r)", sc, body,
        )
        return Reply(
            content=(),
            usage=Usage(),
            stop_reason="error",
            model=request.model,
            error_detail={
                "type":        "http_error",
                "status_code": sc,
                "body":        body,
            },
        )

    def _network_error_reply(
        self, request: Request, exc: Exception,
    ) -> Reply:
        cls = type(exc).__name__
        logger.warning("ollama adapter: %s -> network_error", cls)
        return Reply(
            content=(),
            usage=Usage(),
            stop_reason="error",
            model=request.model,
            error_detail={
                "type":           "network_error",
                "exception_type": cls,
                "message":        str(exc) or cls,
            },
        )

    def _provider_error_reply(
        self, request: Request, *, err_type: str, message: str,
    ) -> Reply:
        return Reply(
            content=(),
            usage=Usage(),
            stop_reason="error",
            model=request.model,
            error_detail={
                "type":           "provider_error",
                "provider_error": {"type": err_type, "message": message},
            },
        )

    # ── token estimation ──────────────────────────────────────

    _fallback_warned: ClassVar[bool] = False

    def _count_input_tokens(self, request: Request) -> int:
        if not type(self)._fallback_warned:
            logger.info(
                "ollama adapter: chars/4 fallback for input token "
                "counting; estimate_cost upper bound is imprecise."
            )
            type(self)._fallback_warned = True

        chars = len(request.system or "")
        for msg in request.messages:
            content = (
                msg.get("content", "")
                if isinstance(msg, dict)
                else getattr(msg, "content", "")
            )
            chars += _content_chars(content)

        # Tool schemas inflate the prompt non-trivially; charge for them.
        for t in request.tools or ():
            chars += _tool_chars(t)

        return max(1, chars // 4 + 16)


# ── module-level helpers ──────────────────────────────────────

def _tool_to_ollama(tool: Any) -> dict[str, Any]:
    """Anthropic-shape tool spec → Ollama (OpenAI-shape) tool spec.

    Anthropic shape (what lipas Request.tools carries):
        {"name": str, "description": str, "input_schema": {...}}

    Ollama (and OpenAI) shape:
        {"type": "function",
         "function": {"name": str, "description": str,
                      "parameters": {...}}}

    We accept either shape on input — if the tool is already in
    OpenAI shape (has "type": "function"), pass it through.
    """
    if isinstance(tool, dict):
        # Already OpenAI-shape? pass through.
        if tool.get("type") == "function" and "function" in tool:
            return tool
        name = tool.get("name", "")
        description = tool.get("description", "") or ""
        schema = (
            tool.get("input_schema")
            or tool.get("parameters")
            or {"type": "object", "properties": {}}
        )
    else:
        name = getattr(tool, "name", "")
        description = getattr(tool, "description", "") or ""
        schema = (
            getattr(tool, "input_schema", None)
            or getattr(tool, "parameters", None)
            or {"type": "object", "properties": {}}
        )

    return {
        "type": "function",
        "function": {
            "name":        name,
            "description": description,
            "parameters":  schema,
        },
    }


def _ollama_tool_call_to_block(tc: Any) -> dict[str, Any] | None:
    """Ollama tool_call entry → Anthropic-shape ``tool_use`` block.

    Ollama wire shape (from /api/chat):
        {"id": "call_xxx",
         "function": {"name": "add",
                      "arguments": {"a": 1, "b": 2}}}

    Provider ids are correlation ids, distinct from LIPAS Effect ids. Preserve
    a non-empty Ollama id so tool results round-trip faithfully; synthesize one
    only for models that omit it.
    """
    if not isinstance(tc, dict):
        logger.warning(
            "ollama adapter: skipping tool_call of type %r",
            type(tc).__name__,
        )
        return None

    fn = tc.get("function") or {}
    name = fn.get("name") or tc.get("name")
    if not name:
        logger.warning("ollama adapter: tool_call missing name: %r", tc)
        return None

    args = fn.get("arguments")
    if isinstance(args, str):
        import json
        try:
            args = json.loads(args) if args else {}
        except json.JSONDecodeError as exc:
            raise ValueError(
                "Ollama returned tool_call arguments that are not valid JSON",
            ) from exc
    elif args is None:
        args = {}
    if not isinstance(args, Mapping):
        raise ValueError("Ollama returned non-object tool_call arguments")

    call_id = tc.get("id") or f"call_{uuid.uuid4().hex[:12]}"

    return {
        "type":  "tool_use",
        "id":    call_id,
        "name":  name,
        "input": dict(args),
    }


def _normalize_messages(msg: Any) -> list[dict[str, Any]]:
    """Coerce a Message-or-dict into Ollama's wire shape.

    For tool-flow messages we preserve structure where Ollama
    understands it:
      - assistant turns with tool_use blocks → role=assistant with
        ``tool_calls`` field
      - tool_result blocks → role=tool messages with
        ``tool_call_id`` + content
    """
    if isinstance(msg, dict):
        role = msg.get("role", "user")
        content = msg.get("content", "")
    else:
        role = getattr(msg, "role", "user")
        content = getattr(msg, "content", "")

    if isinstance(content, str):
        return [{"role": role, "content": content}]

    if isinstance(content, (list, tuple)):
        # Split into text parts, tool_use (assistant), tool_result (user).
        text_parts: list[str] = []
        tool_calls: list[dict[str, Any]] = []
        tool_results: list[dict[str, Any]] = []

        for b in content:
            if isinstance(b, dict):
                btype = b.get("type")
                if btype == "text":
                    text_parts.append(b.get("text", "") or "")
                    continue
                if btype == "tool_use":
                    tool_calls.append({
                        "id": b.get("id", ""),
                        "function": {
                            "name":      b.get("name", ""),
                            "arguments": b.get("input") or {},
                        },
                    })
                    continue
                if btype == "tool_result":
                    c = b.get("content", "")
                    if isinstance(c, list):
                        c = "\n".join(_block_to_text(x) for x in c)
                    tool_results.append({
                        "role":         "tool",
                        "tool_call_id": (
                            b.get("tool_use_id")
                            or b.get("tool_call_id")
                            or ""
                        ),
                        "content":      c if isinstance(c, str) else str(c),
                    })
                    continue
            text_parts.append(_block_to_text(b))

        # Tool results are standalone role=tool messages. One normalized user
        # turn may therefore expand to several Ollama wire messages.
        if tool_results and role == "user":
            out_messages = list(tool_results)
            if text_parts:
                out_messages.append({
                    "role": "user",
                    "content": "\n".join(text_parts),
                })
            return out_messages

        out: dict[str, Any] = {
            "role": role,
            "content": "\n".join(p for p in text_parts if p),
        }
        if tool_calls and role == "assistant":
            out["tool_calls"] = tool_calls
        return [out]

    logger.warning(
        "ollama adapter: unrecognised message content type %r; "
        "stringified.", type(content).__name__,
    )
    return [{"role": role, "content": str(content)}]


def _block_to_text(block: Any) -> str:
    if isinstance(block, dict):
        btype = block.get("type")
        if btype == "text":
            return block.get("text", "") or ""
        if btype == "tool_use":
            return f"[tool_use {block.get('name', '?')}]"
        if btype == "tool_result":
            c = block.get("content", "")
            if isinstance(c, str):
                return c
            if isinstance(c, list):
                return "\n".join(_block_to_text(x) for x in c)
        return str(block)
    text_attr = getattr(block, "text", None)
    if isinstance(text_attr, str):
        return text_attr
    return str(block)


def _content_chars(content: Any) -> int:
    if isinstance(content, str):
        return len(content)
    if isinstance(content, (list, tuple)):
        n = 0
        for block in content:
            if isinstance(block, dict):
                btype = block.get("type")
                if btype == "text":
                    n += len(block.get("text", "") or "")
                elif btype == "tool_use":
                    n += len(str(block.get("input", {})))
                elif btype == "tool_result":
                    c = block.get("content", "")
                    if isinstance(c, str):
                        n += len(c)
                    elif isinstance(c, list):
                        for sub in c:
                            if isinstance(sub, dict):
                                n += len(sub.get("text", "") or "")
            else:
                text_attr = getattr(block, "text", None)
                if isinstance(text_attr, str):
                    n += len(text_attr)
        return n
    return len(str(content))


def _tool_chars(tool: Any) -> int:
    """Rough char count for a tool spec (name + desc + schema)."""
    if isinstance(tool, dict):
        n  = len(tool.get("name", "") or "")
        n += len(tool.get("description", "") or "")
        n += len(str(tool.get("input_schema") or tool.get("parameters") or {}))
        return n
    n  = len(getattr(tool, "name", "") or "")
    n += len(getattr(tool, "description", "") or "")
    n += len(str(
        getattr(tool, "input_schema", None)
        or getattr(tool, "parameters", None)
        or {}
    ))
    return n
