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
from typing import Any, AsyncIterator, ClassVar, Mapping

import httpx

from .estimate import ResourceEstimate
from .pricing import PriceTable
from .reply import Reply
from .request import Request, ToolSpec
from .streaming import Delta, Done, StreamEvent, ToolUseDelta
from .usage import Usage

__all__ = ["OpenAIResponsesAdapter"]


class OpenAIResponsesAdapter:
    """Translate normalized LIPAS requests to OpenAI's Responses API."""

    name: ClassVar[str] = "openai-responses"

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
        body = self._build_body(request, stream=True)
        try:
            async for event in self._post_sse(body):
                typ = event.get("type", "")
                if typ == "response.output_text.delta":
                    yield Delta(index=int(event.get("output_index", 0)), text=str(event.get("delta", "")))
                elif typ == "response.function_call_arguments.delta":
                    yield ToolUseDelta(index=int(event.get("output_index", 0)), partial_json=str(event.get("delta", "")))
                elif typ == "response.completed":
                    yield Done(self._reply_from_response(request, event.get("response", event)))
                    return
                elif typ in {"error", "response.failed", "response.incomplete"}:
                    yield Done(self._error_reply(request, event))
                    return
            yield Done(self._error_reply(request, {"type": "stream_protocol_error", "message": "stream ended without response.completed"}))
        except asyncio.CancelledError:
            raise
        except httpx.HTTPStatusError as exc:
            yield Done(self._error_reply(request, self._http_detail(exc)))
        except (httpx.TransportError, httpx.TimeoutException) as exc:
            yield Done(self._error_reply(request, {"type": "network_error", "message": str(exc), "provider_raw": type(exc).__name__}))
        except Exception as exc:
            yield Done(self._error_reply(request, {"type": "provider_error", "message": str(exc) or type(exc).__name__, "provider_raw": type(exc).__name__}))

    def _build_body(self, request: Request, *, stream: bool) -> dict[str, Any]:
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
        out = []
        for message in request.messages:
            role, content = message.get("role", "user"), message.get("content", "")
            if isinstance(content, str):
                out.append({"role": role, "content": content})
            else:
                # Responses accepts text parts and function call outputs. Keeping
                # unknown normalized blocks intact avoids lossy provider coupling.
                out.append({"role": role, "content": list(content)})
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
                try: arguments = json.loads(raw) if isinstance(raw, str) else dict(raw)
                except (TypeError, ValueError): arguments = {}
                blocks.append({"type": "tool_use", "id": item.get("call_id") or item.get("id") or f"call_{uuid.uuid4().hex}", "name": item.get("name", ""), "input": arguments})
        usage = response.get("usage") or {}
        stop = "tool_use" if any(b["type"] == "tool_use" for b in blocks) else "max_tokens" if response.get("status") == "incomplete" else "end_turn"
        return Reply(tuple(blocks), Usage(input=int(usage.get("input_tokens", 0) or 0), output=int(usage.get("output_tokens", 0) or 0), cache_read=int((usage.get("input_tokens_details") or {}).get("cached_tokens", 0) or 0)), stop, str(response.get("model") or request.model))

    def _error_reply(self, request: Request, detail: Mapping[str, Any]) -> Reply:
        return Reply((), Usage(), "error", request.model, error_detail=dict(detail))

    @staticmethod
    def _http_detail(exc: httpx.HTTPStatusError) -> dict[str, Any]:
        try: raw: Any = exc.response.json()
        except Exception: raw = exc.response.text
        return {"type": "http_error", "status_code": exc.response.status_code, "message": str(exc), "provider_raw": raw}
