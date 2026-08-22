"""OpenAI Responses API adapter.

The adapter uses the HTTP API directly, so installing ``openai`` is optional.
An ``httpx.AsyncClient`` can be injected for tests or controlled transports.
"""
from __future__ import annotations

import asyncio
import json
import os
import uuid
from decimal import Decimal
from typing import Any, AsyncIterator, Mapping

import httpx

from .types import (
    Delta, Done, PriceTable, Reply, Request, ResourceEstimate, StopReason, StreamEvent,
    ToolSpec, ToolUseDelta, Usage,
)

__all__ = ["OpenAIResponsesAdapter"]


class OpenAIResponsesAdapter:
    """Translate normalized LIPAS requests to OpenAI's Responses API."""

    name: str = "openai-responses"

    def __init__(self, *, api_key: str | None = None, prices: PriceTable | None = None,
                 base_url: str = "https://api.openai.com/v1", timeout_s: float = 120.0,
                 client: httpx.AsyncClient | None = None, name: str = "openai-responses") -> None:
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY")
        self.prices = prices
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s
        self._client = client
        self.name = name

    async def estimate_cost(self, request: Request) -> ResourceEstimate:
        # This deliberately estimates tokens locally; Responses has no cheap
        # preflight count endpoint and admission must not make a billable call.
        tokens = max(1, sum(len(str(m.get("content", ""))) for m in request.messages) // 4)
        if request.system:
            tokens += len(request.system) // 4
        cost = Decimal("0") if self.prices is None else self.prices.for_model(request.model).cost(
            Usage(input=tokens, output=request.max_tokens)
        )
        return ResourceEstimate(request.model, tokens, request.max_tokens, cost)

    async def stream(self, request: Request) -> AsyncIterator[StreamEvent]:
        try:
            body = self._build_body(request, stream=True)
            async for event in self._post_sse(body):
                typ = event.get("type", "")
                if typ == "response.output_text.delta":
                    yield Delta(index=int(event.get("output_index", 0)), text=str(event.get("delta", "")))
                elif typ == "response.function_call_arguments.delta":
                    yield ToolUseDelta(index=int(event.get("output_index", 0)), partial_json=str(event.get("delta", "")))
                elif typ == "response.completed":
                    yield Done(self._reply_from_response(request, event.get("response", event)))
                    return
                elif typ == "response.incomplete":
                    response = event.get("response", event)
                    reason = (response.get("incomplete_details") or {}).get("reason")
                    if reason not in {None, "max_output_tokens"}:
                        yield Done(self._provider_error_reply(request, {
                            "type": str(reason),
                            "reason": reason,
                            "response": response,
                        }))
                    else:
                        yield Done(self._reply_from_response(request, response))
                    return
                elif typ in {"error", "response.failed"}:
                    yield Done(self._provider_error_reply(request, event))
                    return
            yield Done(self._provider_error_reply(request, {
                "type": "stream_protocol_error",
                "message": "stream ended without a terminal response event",
            }))
        except asyncio.CancelledError:
            raise
        except httpx.HTTPStatusError as exc:
            yield Done(self._error_reply(request, self._http_detail(exc)))
        except (httpx.TransportError, httpx.TimeoutException) as exc:
            yield Done(self._error_reply(request, {
                "type": "network_error",
                "exception_type": type(exc).__name__,
                "message": str(exc) or type(exc).__name__,
            }))
        except Exception as exc:
            yield Done(self._provider_error_reply(request, {
                "type": type(exc).__name__,
                "message": str(exc) or type(exc).__name__,
            }))

    def _build_body(self, request: Request, *, stream: bool) -> dict[str, Any]:
        if request.stop_sequences:
            raise ValueError(
                "OpenAI Responses API does not support stop_sequences; "
                "remove them or choose an adapter that supports this field"
            )
        body: dict[str, Any] = {"model": request.model, "input": self._input(request), "max_output_tokens": request.max_tokens, "stream": stream}
        if request.system:
            body["instructions"] = request.system
        if request.temperature is not None:
            body["temperature"] = request.temperature
        if request.tools:
            body["tools"] = [{"type": "function", "name": t.name, "description": t.description, "parameters": dict(t.input_schema)} if isinstance(t, ToolSpec) else {"type": "function", "name": t["name"], "description": t["description"], "parameters": dict(t.get("input_schema", t.get("parameters", {})))} for t in request.tools]
        body.update({k: v for k, v in request.extra.items() if k not in body})
        return body

    @staticmethod
    def _input(request: Request) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for message in request.messages:
            role, content = message.get("role", "user"), message.get("content", "")
            if isinstance(content, str):
                out.append({"role": role, "content": content})
                continue

            text_parts: list[str] = []

            def flush_text(
                text_parts: list[str] = text_parts,
                role: Any = role,
            ) -> None:
                if text_parts:
                    out.append({"role": role, "content": "".join(text_parts)})
                    text_parts.clear()

            for block in content:
                if not isinstance(block, Mapping):
                    text_parts.append(str(block))
                    continue
                block_type = block.get("type")
                if block_type == "text":
                    text_parts.append(str(block.get("text", "")))
                    continue
                if block_type == "tool_use":
                    flush_text()
                    arguments = block.get("input") or {}
                    out.append({
                        "type": "function_call",
                        "call_id": str(block.get("id", "")),
                        "name": str(block.get("name", "")),
                        "arguments": json.dumps(
                            arguments,
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ),
                    })
                    continue
                if block_type == "tool_result":
                    flush_text()
                    output = block.get("content", "")
                    if not isinstance(output, str):
                        output = json.dumps(output, ensure_ascii=False, default=str)
                    out.append({
                        "type": "function_call_output",
                        "call_id": str(
                            block.get("tool_use_id")
                            or block.get("tool_call_id")
                            or ""
                        ),
                        "output": output,
                    })
                    continue
                text_parts.append(str(block))
            flush_text()
        return out

    async def _post_sse(self, body: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        if self._client is not None:
            async with self._client.stream("POST", f"{self.base_url}/responses", json=body, headers=headers, timeout=self.timeout_s) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if line.startswith("data: ") and line[6:] != "[DONE]":
                        yield json.loads(line[6:])
            return
        async with httpx.AsyncClient(timeout=self.timeout_s) as client:
            async with client.stream("POST", f"{self.base_url}/responses", json=body, headers=headers) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if line.startswith("data: ") and line[6:] != "[DONE]":
                        yield json.loads(line[6:])

    def _reply_from_response(self, request: Request, response: Mapping[str, Any]) -> Reply:
        blocks: list[dict[str, Any]] = []
        for item in response.get("output", ()) or ():
            if item.get("type") == "message":
                for part in item.get("content", ()):
                    if part.get("type") in {"output_text", "text"}:
                        blocks.append({"type": "text", "text": part.get("text", "")})
            elif item.get("type") == "function_call":
                raw = item.get("arguments", "{}")
                try:
                    arguments = json.loads(raw) if isinstance(raw, str) else dict(raw)
                except (TypeError, ValueError) as exc:
                    raise ValueError(
                        "OpenAI returned function_call arguments that are not valid JSON",
                    ) from exc
                if not isinstance(arguments, Mapping):
                    raise ValueError(
                        "OpenAI returned non-object function_call arguments",
                    )
                blocks.append({"type": "tool_use", "id": item.get("call_id") or item.get("id") or f"call_{uuid.uuid4().hex}", "name": item.get("name", ""), "input": arguments})
        usage = response.get("usage") or {}
        input_total = int(usage.get("input_tokens", 0) or 0)
        cache_read = int(
            (usage.get("input_tokens_details") or {}).get("cached_tokens", 0) or 0,
        )
        # OpenAI reports cached tokens as a subset of input_tokens. LIPAS Usage
        # buckets are disjoint so ModelPrice does not charge them twice.
        input_uncached = max(0, input_total - cache_read)
        stop: StopReason = (
            "tool_use"
            if any(b["type"] == "tool_use" for b in blocks)
            else "max_tokens"
            if response.get("status") == "incomplete"
            else "end_turn"
        )
        return Reply(
            tuple(blocks),
            Usage(
                input=input_uncached,
                output=int(usage.get("output_tokens", 0) or 0),
                cache_read=cache_read,
            ),
            stop,
            str(response.get("model") or request.model),
        )

    def _error_reply(self, request: Request, detail: Mapping[str, Any]) -> Reply:
        return Reply((), Usage(), "error", request.model, error_detail=dict(detail))

    def _provider_error_reply(
        self,
        request: Request,
        detail: Mapping[str, Any],
    ) -> Reply:
        return self._error_reply(request, {
            "type": "provider_error",
            "provider_error": dict(detail),
        })

    @staticmethod
    def _http_detail(exc: httpx.HTTPStatusError) -> dict[str, Any]:
        try:
            raw: Any = exc.response.json()
        except Exception:
            raw = exc.response.text
        return {
            "type": "http_error",
            "status_code": exc.response.status_code,
            "body": raw if isinstance(raw, dict) else {"raw": raw},
        }
