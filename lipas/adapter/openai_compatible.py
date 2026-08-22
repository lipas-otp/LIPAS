"""OpenAI-compatible Chat Completions adapter.

Many providers implement the de-facto ``/chat/completions`` HTTP contract
without implementing OpenAI's newer Responses API.  This adapter deliberately
targets that smaller wire contract and keeps provider identity outside the
runtime: callers supply an endpoint, model name, and API key explicitly.

The non-streaming request path is the compatibility-first default.  Real SSE
streaming is available explicitly and is reported as a distinct adapter
capability so applications do not accidentally require a feature their chosen
endpoint has not been configured to use.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
from collections.abc import AsyncIterator, Mapping, Sequence
from decimal import Decimal
from typing import Any
from urllib.parse import urlsplit

import httpx

from .types import (
    Delta,
    Done,
    PriceTable,
    Reply,
    Request,
    ResourceEstimate,
    StopReason,
    StreamEvent,
    Thinking,
    ToolSpec,
    ToolUseDelta,
    Usage,
)

__all__ = ["OpenAICompatibleAdapter"]


_RESERVED_EXTRA_FIELDS = frozenset({
    "model",
    "messages",
    "stream",
    "max_tokens",
    "max_completion_tokens",
    "tools",
    "temperature",
    "stop",
    "stream_options",
})
_FORBIDDEN_HEADERS = frozenset({
    "accept",
    "authorization",
    "content-length",
    "content-type",
    "host",
    "transfer-encoding",
})


class _ProviderPayloadError(ValueError):
    def __init__(self, detail: Mapping[str, Any]) -> None:
        super().__init__("provider returned an error payload")
        self.detail = dict(detail)


class OpenAICompatibleAdapter:
    """Connect LIPAS to an OpenAI-compatible Chat Completions endpoint.

    ``base_url`` may be either a versioned API root such as
    ``https://example.com/v1`` or the complete ``.../chat/completions`` URL.
    Query strings, fragments, and embedded credentials are rejected so the
    endpoint remains unambiguous and secrets cannot leak through URLs.

    API keys use Bearer authentication.  An explicit ``api_key`` wins over
    ``api_key_env``; set ``require_api_key=False`` for a trusted no-auth local
    endpoint.  Injected HTTP clients remain caller-owned.
    """

    name: str = "openai-compatible"

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str | None = None,
        api_key_env: str | None = "OPENAI_API_KEY",
        require_api_key: bool = True,
        prices: PriceTable | None = None,
        timeout_s: float = 120.0,
        streaming: bool = False,
        include_usage: bool = False,
        max_tokens_field: str = "max_tokens",
        headers: Mapping[str, str] | None = None,
        client: httpx.AsyncClient | None = None,
        name: str | None = None,
    ) -> None:
        self.url = self._chat_completions_url(base_url)
        self.api_key_env = self._validate_api_key_env(api_key_env)
        self.api_key = self._resolve_api_key(api_key, self.api_key_env)
        if not isinstance(require_api_key, bool):
            raise TypeError("require_api_key must be bool")
        if require_api_key and self.api_key is None:
            source = self.api_key_env or "an explicit api_key"
            raise ValueError(
                "OpenAI-compatible endpoint requires an API key; pass "
                f"api_key=... or set {source}",
            )
        if (
            isinstance(timeout_s, bool)
            or not isinstance(timeout_s, (int, float))
            or not math.isfinite(float(timeout_s))
            or timeout_s <= 0
        ):
            raise ValueError("timeout_s must be a finite positive number")
        if not isinstance(streaming, bool):
            raise TypeError("streaming must be bool")
        if not isinstance(include_usage, bool):
            raise TypeError("include_usage must be bool")
        if include_usage and not streaming:
            raise ValueError("include_usage is only meaningful with streaming=True")
        if max_tokens_field not in {"max_tokens", "max_completion_tokens"}:
            raise ValueError(
                "max_tokens_field must be 'max_tokens' or "
                "'max_completion_tokens'",
            )
        self.prices = prices
        self.timeout_s = float(timeout_s)
        self.streaming = streaming
        self.include_usage = include_usage
        self.max_tokens_field = max_tokens_field
        self.headers = self._validate_headers(headers or {})
        self._client = client
        if name is None:
            self.name = (
                "openai-compatible-stream" if streaming else "openai-compatible"
            )
        elif not isinstance(name, str) or not name.strip():
            raise ValueError("name must be a non-empty string or None")
        else:
            self.name = name.strip()

    async def estimate_cost(self, request: Request) -> ResourceEstimate:
        """Estimate locally; admission never performs a provider call."""
        tokens = max(
            1,
            sum(len(str(message.get("content", ""))) for message in request.messages)
            // 4,
        )
        if request.system:
            tokens += max(1, len(request.system) // 4)
        cost = (
            Decimal("0")
            if self.prices is None
            else self.prices.for_model(request.model).cost(
                Usage(input=tokens, output=request.max_tokens),
            )
        )
        return ResourceEstimate(request.model, tokens, request.max_tokens, cost)

    async def stream(self, request: Request) -> AsyncIterator[StreamEvent]:
        accumulator = _ChatStreamAccumulator(request)
        try:
            body = self._build_body(request)
            if not self.streaming:
                payload = await self._post_json(body)
                yield Done(self._reply_from_payload(request, payload))
                return
            async for payload in self._post_sse(body):
                for event in accumulator.consume(payload):
                    yield event
            yield Done(accumulator.finish())
        except asyncio.CancelledError:
            raise
        except _ProviderPayloadError as exc:
            yield Done(self._provider_error_reply(
                request,
                self._redact(exc.detail),
                content=accumulator.partial_content(),
                usage=accumulator.usage,
            ))
        except httpx.HTTPStatusError as exc:
            yield Done(self._error_reply(
                request,
                self._http_detail(exc),
                content=accumulator.partial_content(),
                usage=accumulator.usage,
            ))
        except (httpx.TransportError, httpx.TimeoutException) as exc:
            yield Done(self._error_reply(
                request,
                {
                    "type": "network_error",
                    "exception_type": type(exc).__name__,
                    "message": self._redact_text(
                        str(exc) or type(exc).__name__,
                    ),
                },
                content=accumulator.partial_content(),
                usage=accumulator.usage,
            ))
        except Exception as exc:
            yield Done(self._provider_error_reply(
                request,
                {
                    "type": type(exc).__name__,
                    "message": self._redact_text(
                        str(exc) or type(exc).__name__,
                    ),
                },
                content=accumulator.partial_content(),
                usage=accumulator.usage,
            ))

    def _build_body(self, request: Request) -> dict[str, Any]:
        if not isinstance(request.extra, Mapping):
            raise TypeError("request.extra must be a mapping")
        invalid_extra_keys = [
            key for key in request.extra if not isinstance(key, str) or not key
        ]
        if invalid_extra_keys:
            raise ValueError("request.extra keys must be non-empty strings")
        collisions = sorted(set(request.extra).intersection(_RESERVED_EXTRA_FIELDS))
        if collisions:
            raise ValueError(
                "request.extra cannot override adapter-owned fields: "
                + ", ".join(collisions),
            )
        body: dict[str, Any] = {
            "model": request.model,
            "messages": self._messages(request),
            self.max_tokens_field: request.max_tokens,
            "stream": self.streaming,
        }
        if request.temperature is not None:
            body["temperature"] = request.temperature
        if request.stop_sequences:
            body["stop"] = list(request.stop_sequences)
        if request.tools:
            body["tools"] = [self._tool_spec(tool) for tool in request.tools]
        if self.streaming and self.include_usage:
            body["stream_options"] = {"include_usage": True}
        body.update(request.extra)
        return body

    @classmethod
    def _messages(cls, request: Request) -> list[dict[str, Any]]:
        output: list[dict[str, Any]] = []
        if request.system:
            output.append({"role": "system", "content": request.system})
        for raw_message in request.messages:
            role = raw_message.get("role", "user")
            if not isinstance(role, str) or not role:
                raise ValueError("message role must be a non-empty string")
            content = raw_message.get("content", "")
            if isinstance(content, str):
                output.append({"role": role, "content": content})
                continue
            if not isinstance(content, Sequence) or isinstance(
                content,
                (bytes, bytearray),
            ):
                raise TypeError("message content must be a string or block sequence")
            blocks = list(content)
            if role == "assistant":
                output.append(cls._assistant_message(blocks))
            else:
                output.extend(cls._non_assistant_messages(role, blocks))
        return output

    @classmethod
    def _assistant_message(cls, blocks: Sequence[Any]) -> dict[str, Any]:
        text: list[str] = []
        tool_calls: list[dict[str, Any]] = []
        for block in blocks:
            if not isinstance(block, Mapping):
                raise TypeError("assistant content blocks must be mappings")
            block_type = block.get("type")
            if block_type == "text":
                text.append(str(block.get("text", "")))
            elif block_type == "tool_use":
                tool_calls.append(cls._tool_call_from_block(block))
            elif block_type == "tool_result":
                raise ValueError("assistant messages cannot contain tool_result blocks")
            else:
                raise ValueError(
                    f"unsupported assistant content block: {block_type!r}",
                )
        message: dict[str, Any] = {
            "role": "assistant",
            "content": "".join(text) if text else None,
        }
        if tool_calls:
            message["tool_calls"] = tool_calls
        return message

    @classmethod
    def _non_assistant_messages(
        cls,
        role: str,
        blocks: Sequence[Any],
    ) -> list[dict[str, Any]]:
        output: list[dict[str, Any]] = []
        text: list[str] = []

        def flush_text() -> None:
            if text:
                output.append({"role": role, "content": "".join(text)})
                text.clear()

        for block in blocks:
            if not isinstance(block, Mapping):
                raise TypeError("message content blocks must be mappings")
            block_type = block.get("type")
            if block_type == "text":
                text.append(str(block.get("text", "")))
            elif block_type == "tool_result":
                flush_text()
                tool_call_id = (
                    block.get("tool_call_id") or block.get("tool_use_id")
                )
                if not isinstance(tool_call_id, str) or not tool_call_id:
                    raise ValueError("tool_result requires a non-empty tool call id")
                output.append({
                    "role": "tool",
                    "tool_call_id": tool_call_id,
                    "content": cls._tool_result_content(block.get("content", "")),
                })
            elif block_type == "tool_use":
                raise ValueError("tool_use blocks require role='assistant'")
            else:
                raise ValueError(f"unsupported content block: {block_type!r}")
        flush_text()
        if not output:
            output.append({"role": role, "content": ""})
        return output

    @staticmethod
    def _tool_call_from_block(block: Mapping[str, Any]) -> dict[str, Any]:
        call_id = block.get("id")
        name = block.get("name")
        arguments = block.get("input", {})
        if not isinstance(call_id, str) or not call_id:
            raise ValueError("tool_use requires a non-empty id")
        if not isinstance(name, str) or not name:
            raise ValueError("tool_use requires a non-empty name")
        if not isinstance(arguments, Mapping):
            raise TypeError("tool_use input must be a mapping")
        return {
            "id": call_id,
            "type": "function",
            "function": {
                "name": name,
                "arguments": json.dumps(
                    arguments,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            },
        }

    @staticmethod
    def _tool_result_content(value: Any) -> str:
        if isinstance(value, str):
            return value
        if isinstance(value, Sequence) and not isinstance(
            value,
            (bytes, bytearray),
        ):
            text: list[str] = []
            for part in value:
                if isinstance(part, Mapping) and part.get("type") == "text":
                    text.append(str(part.get("text", "")))
                else:
                    return json.dumps(value, ensure_ascii=False, default=str)
            return "".join(text)
        return json.dumps(value, ensure_ascii=False, default=str)

    @staticmethod
    def _tool_spec(tool: ToolSpec | Mapping[str, Any]) -> dict[str, Any]:
        name: Any
        description: Any
        parameters: Any
        if isinstance(tool, ToolSpec):
            name = tool.name
            description = tool.description
            parameters = tool.input_schema
        elif tool.get("type") == "function" and isinstance(
            tool.get("function"),
            Mapping,
        ):
            function = tool["function"]
            name = function.get("name")
            description = function.get("description", "")
            parameters = function.get("parameters", {})
        else:
            name = tool.get("name")
            description = tool.get("description", "")
            parameters = tool.get("input_schema", tool.get("parameters", {}))
        if not isinstance(name, str) or not name:
            raise ValueError("tool descriptor requires a non-empty name")
        if not isinstance(description, str):
            raise TypeError("tool description must be a string")
        if not isinstance(parameters, Mapping):
            raise TypeError("tool parameters must be a mapping")
        return {
            "type": "function",
            "function": {
                "name": name,
                "description": description,
                "parameters": dict(parameters),
            },
        }

    async def _post_json(self, body: Mapping[str, Any]) -> Mapping[str, Any]:
        if self._client is not None:
            response = await self._client.post(
                self.url,
                json=body,
                headers=self._request_headers(streaming=False),
                timeout=self.timeout_s,
            )
        else:
            async with httpx.AsyncClient(timeout=self.timeout_s) as client:
                response = await client.post(
                    self.url,
                    json=body,
                    headers=self._request_headers(streaming=False),
                )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, Mapping):
            raise ValueError("Chat Completions response must be a JSON object")
        return payload

    async def _post_sse(
        self,
        body: Mapping[str, Any],
    ) -> AsyncIterator[Mapping[str, Any]]:
        if self._client is not None:
            async with self._client.stream(
                "POST",
                self.url,
                json=body,
                headers=self._request_headers(streaming=True),
                timeout=self.timeout_s,
            ) as response:
                response.raise_for_status()
                async for payload in self._sse_payloads(response):
                    yield payload
            return
        async with httpx.AsyncClient(timeout=self.timeout_s) as client:
            async with client.stream(
                "POST",
                self.url,
                json=body,
                headers=self._request_headers(streaming=True),
            ) as response:
                response.raise_for_status()
                async for payload in self._sse_payloads(response):
                    yield payload

    @staticmethod
    async def _sse_payloads(
        response: httpx.Response,
    ) -> AsyncIterator[Mapping[str, Any]]:
        data_lines: list[str] = []

        def decode() -> Mapping[str, Any] | None:
            if not data_lines:
                return None
            raw = "\n".join(data_lines)
            data_lines.clear()
            if raw.strip() == "[DONE]":
                return None
            payload = json.loads(raw)
            if not isinstance(payload, Mapping):
                raise ValueError("SSE data must contain a JSON object")
            return payload

        async for raw_line in response.aiter_lines():
            line = raw_line.rstrip("\r")
            if not line:
                payload = decode()
                if payload is not None:
                    yield payload
                continue
            if line.startswith(":"):
                continue
            if line.startswith("data:"):
                data_lines.append(line[5:].lstrip(" "))
        payload = decode()
        if payload is not None:
            yield payload

    def _reply_from_payload(
        self,
        request: Request,
        payload: Mapping[str, Any],
    ) -> Reply:
        error = payload.get("error")
        if isinstance(error, Mapping):
            raise _ProviderPayloadError(error)
        choices = payload.get("choices")
        if not isinstance(choices, Sequence) or isinstance(
            choices,
            (str, bytes, bytearray),
        ):
            raise ValueError("Chat Completions response requires choices")
        if len(choices) != 1:
            raise ValueError(
                "LIPAS requires exactly one Chat Completions choice; "
                f"received {len(choices)}",
            )
        choice = choices[0]
        if not isinstance(choice, Mapping):
            raise ValueError("Chat Completions choice must be an object")
        message = choice.get("message")
        if not isinstance(message, Mapping):
            raise ValueError("Chat Completions choice requires a message object")
        text = self._message_text(message.get("content"))
        calls = self._response_tool_calls(
            message,
            response_identity=str(payload.get("id", "")),
        )
        return self._make_reply(
            request=request,
            model=payload.get("model"),
            text=text,
            tool_calls=calls,
            finish_reason=choice.get("finish_reason"),
            usage=self._usage(payload.get("usage")),
        )

    @staticmethod
    def _message_text(content: Any) -> str:
        if content is None:
            return ""
        if isinstance(content, str):
            return content
        if not isinstance(content, Sequence) or isinstance(
            content,
            (bytes, bytearray),
        ):
            raise ValueError("assistant message content must be text or text parts")
        text: list[str] = []
        for part in content:
            if not isinstance(part, Mapping) or part.get("type") not in {
                "text",
                "output_text",
            }:
                raise ValueError("unsupported assistant message content part")
            text.append(str(part.get("text", "")))
        return "".join(text)

    @classmethod
    def _response_tool_calls(
        cls,
        message: Mapping[str, Any],
        *,
        response_identity: str,
    ) -> list[dict[str, Any]]:
        raw_calls = message.get("tool_calls")
        if raw_calls is None and isinstance(message.get("function_call"), Mapping):
            raw_calls = [message["function_call"]]
        if raw_calls is None:
            return []
        if not isinstance(raw_calls, Sequence) or isinstance(
            raw_calls,
            (str, bytes, bytearray),
        ):
            raise ValueError("tool_calls must be a sequence")
        calls: list[dict[str, Any]] = []
        for index, raw_call in enumerate(raw_calls):
            if not isinstance(raw_call, Mapping):
                raise ValueError("tool call must be an object")
            function = raw_call.get("function", raw_call)
            if not isinstance(function, Mapping):
                raise ValueError("tool call function must be an object")
            name = function.get("name")
            if not isinstance(name, str) or not name:
                raise ValueError("tool call requires a non-empty function name")
            arguments = cls._arguments(function.get("arguments", "{}"))
            call_id = raw_call.get("id")
            if not isinstance(call_id, str) or not call_id:
                call_id = cls._derived_call_id(
                    response_identity,
                    index,
                    name,
                    function.get("arguments", "{}"),
                )
            calls.append({
                "type": "tool_use",
                "id": call_id,
                "name": name,
                "input": arguments,
            })
        return calls

    @staticmethod
    def _arguments(raw: Any) -> dict[str, Any]:
        if isinstance(raw, str):
            try:
                value = json.loads(raw or "{}")
            except json.JSONDecodeError as exc:
                raise ValueError("tool call arguments are not valid JSON") from exc
        elif isinstance(raw, Mapping):
            value = raw
        else:
            raise ValueError("tool call arguments must be JSON text or an object")
        if not isinstance(value, Mapping):
            raise ValueError("tool call arguments must decode to an object")
        return dict(value)

    @staticmethod
    def _derived_call_id(
        response_identity: str,
        index: int,
        name: str,
        arguments: Any,
    ) -> str:
        material = json.dumps(
            [response_identity, index, name, arguments],
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        ).encode("utf-8")
        return "call_lipas_" + hashlib.sha256(material).hexdigest()[:20]

    @classmethod
    def _make_reply(
        cls,
        *,
        request: Request,
        model: Any,
        text: str,
        tool_calls: Sequence[Mapping[str, Any]],
        finish_reason: Any,
        usage: Usage,
    ) -> Reply:
        blocks: list[Mapping[str, Any]] = []
        if text:
            blocks.append({"type": "text", "text": text})
        blocks.extend(tool_calls)
        if finish_reason == "content_filter":
            return Reply(
                blocks,
                usage,
                "error",
                str(model or request.model),
                error_detail={
                    "type": "provider_error",
                    "provider_error": {
                        "type": "content_filter",
                        "finish_reason": finish_reason,
                    },
                },
            )
        stop_reason = cls._stop_reason(
            request,
            finish_reason,
            has_tool_calls=bool(tool_calls),
        )
        return Reply(
            blocks,
            usage,
            stop_reason,
            str(model or request.model),
        )

    @staticmethod
    def _stop_reason(
        request: Request,
        finish_reason: Any,
        *,
        has_tool_calls: bool,
    ) -> StopReason:
        if finish_reason in {"tool_calls", "function_call"} or has_tool_calls:
            return "tool_use"
        if finish_reason == "length":
            return "max_tokens"
        if finish_reason == "stop":
            return "stop_sequence" if request.stop_sequences else "end_turn"
        raise ValueError(f"unsupported finish_reason: {finish_reason!r}")

    @classmethod
    def _usage(cls, raw: Any) -> Usage:
        if raw is None:
            return Usage()
        if not isinstance(raw, Mapping):
            raise ValueError("usage must be a JSON object")
        input_total = cls._token_count(
            raw.get("prompt_tokens", raw.get("input_tokens", 0)),
            "prompt_tokens",
        )
        output = cls._token_count(
            raw.get("completion_tokens", raw.get("output_tokens", 0)),
            "completion_tokens",
        )
        details = raw.get("prompt_tokens_details", raw.get("input_tokens_details", {}))
        if details is None:
            details = {}
        if not isinstance(details, Mapping):
            raise ValueError("prompt token details must be a JSON object")
        cached = cls._token_count(details.get("cached_tokens", 0), "cached_tokens")
        if cached > input_total:
            raise ValueError("cached_tokens cannot exceed prompt_tokens")
        return Usage(input=input_total - cached, output=output, cache_read=cached)

    @staticmethod
    def _token_count(value: Any, field: str) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"{field} must be a non-negative integer")
        return value

    def _request_headers(self, *, streaming: bool) -> dict[str, str]:
        headers = {
            "Accept": "text/event-stream" if streaming else "application/json",
            "Content-Type": "application/json",
            **self.headers,
        }
        if self.api_key is not None:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _error_reply(
        self,
        request: Request,
        detail: Mapping[str, Any],
        *,
        content: Sequence[Mapping[str, Any]] = (),
        usage: Usage | None = None,
    ) -> Reply:
        return Reply(
            tuple(content),
            usage or Usage(),
            "error",
            request.model,
            error_detail=dict(detail),
        )

    def _provider_error_reply(
        self,
        request: Request,
        detail: Mapping[str, Any],
        *,
        content: Sequence[Mapping[str, Any]] = (),
        usage: Usage | None = None,
    ) -> Reply:
        return self._error_reply(
            request,
            {"type": "provider_error", "provider_error": dict(detail)},
            content=content,
            usage=usage,
        )

    def _http_detail(self, exc: httpx.HTTPStatusError) -> dict[str, Any]:
        try:
            raw: Any = exc.response.json()
        except Exception:
            raw = {"raw": exc.response.text[:4096]}
        if not isinstance(raw, Mapping):
            raw = {"data": raw}
        return {
            "type": "http_error",
            "status_code": exc.response.status_code,
            "body": self._redact(dict(raw)),
        }

    def _redact(self, value: Any) -> Any:
        if isinstance(value, Mapping):
            return {str(key): self._redact(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self._redact(item) for item in value]
        if isinstance(value, tuple):
            return tuple(self._redact(item) for item in value)
        if isinstance(value, str):
            return self._redact_text(value)
        return value

    def _redact_text(self, value: str) -> str:
        if self.api_key:
            return value.replace(self.api_key, "<redacted>")
        return value

    @staticmethod
    def _chat_completions_url(base_url: str) -> str:
        if not isinstance(base_url, str) or not base_url.strip():
            raise ValueError("base_url must be a non-empty absolute URL")
        value = base_url.strip()
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("base_url must use absolute http:// or https://")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("base_url must not embed credentials")
        if parsed.query or parsed.fragment:
            raise ValueError("base_url must not contain a query string or fragment")
        normalized = value.rstrip("/")
        if normalized.endswith("/chat/completions"):
            return normalized
        return normalized + "/chat/completions"

    @staticmethod
    def _validate_api_key_env(value: str | None) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str) or not value.strip():
            raise ValueError("api_key_env must be a non-empty string or None")
        return value.strip()

    @staticmethod
    def _resolve_api_key(
        explicit: str | None,
        environment_name: str | None,
    ) -> str | None:
        if explicit is not None:
            if not isinstance(explicit, str) or not explicit.strip():
                raise ValueError("api_key must be a non-empty string or None")
            return explicit.strip()
        if environment_name is None:
            return None
        environment_value = os.environ.get(environment_name)
        if environment_value is None:
            return None
        if not environment_value.strip():
            raise ValueError(f"environment variable {environment_name} is empty")
        return environment_value.strip()

    @staticmethod
    def _validate_headers(headers: Mapping[str, str]) -> dict[str, str]:
        if not isinstance(headers, Mapping):
            raise TypeError("headers must be a mapping")
        output: dict[str, str] = {}
        for name, value in headers.items():
            if not isinstance(name, str) or not name.strip():
                raise ValueError("header names must be non-empty strings")
            if not isinstance(value, str):
                raise TypeError("header values must be strings")
            normalized = name.strip()
            if normalized.lower() in _FORBIDDEN_HEADERS:
                raise ValueError(f"header {normalized!r} is adapter-owned or unsafe")
            output[normalized] = value
        return output


class _ChatStreamAccumulator:
    def __init__(self, request: Request) -> None:
        self.request = request
        self.text: list[str] = []
        self.reasoning: list[str] = []
        self.tool_calls: dict[int, dict[str, str]] = {}
        self.finish_reason: Any = None
        self.model: str | None = None
        self.response_identity = ""
        self.usage = Usage()

    def consume(self, payload: Mapping[str, Any]) -> list[StreamEvent]:
        error = payload.get("error")
        if isinstance(error, Mapping):
            raise _ProviderPayloadError(error)
        if payload.get("model") is not None:
            self.model = str(payload["model"])
        if payload.get("id") is not None:
            self.response_identity = str(payload["id"])
        if payload.get("usage") is not None:
            self.usage = OpenAICompatibleAdapter._usage(payload["usage"])
        choices = payload.get("choices", ())
        if not isinstance(choices, Sequence) or isinstance(
            choices,
            (str, bytes, bytearray),
        ):
            raise ValueError("stream chunk choices must be a sequence")
        events: list[StreamEvent] = []
        for choice in choices:
            if not isinstance(choice, Mapping):
                raise ValueError("stream choice must be an object")
            index = choice.get("index", 0)
            if isinstance(index, bool) or not isinstance(index, int) or index != 0:
                raise ValueError("LIPAS supports only streaming choice index 0")
            delta = choice.get("delta", {})
            if not isinstance(delta, Mapping):
                raise ValueError("stream choice delta must be an object")
            content = delta.get("content")
            if content is not None:
                if not isinstance(content, str):
                    raise ValueError("stream content delta must be a string")
                if content:
                    self.text.append(content)
                    events.append(Delta(index=0, text=content))
            reasoning = delta.get("reasoning_content", delta.get("reasoning"))
            if reasoning is not None:
                if not isinstance(reasoning, str):
                    raise ValueError("stream reasoning delta must be a string")
                if reasoning:
                    self.reasoning.append(reasoning)
                    events.append(Thinking(text=reasoning))
            raw_calls = delta.get("tool_calls")
            if raw_calls is not None:
                self._consume_tool_calls(raw_calls, events)
            legacy_call = delta.get("function_call")
            if legacy_call is not None:
                self._consume_tool_calls([legacy_call], events, legacy=True)
            finish = choice.get("finish_reason")
            if finish is not None:
                if self.finish_reason is not None and finish != self.finish_reason:
                    raise ValueError("stream returned conflicting finish reasons")
                self.finish_reason = finish
        return events

    def _consume_tool_calls(
        self,
        raw_calls: Any,
        events: list[StreamEvent],
        *,
        legacy: bool = False,
    ) -> None:
        if not isinstance(raw_calls, Sequence) or isinstance(
            raw_calls,
            (str, bytes, bytearray),
        ):
            raise ValueError("stream tool_calls must be a sequence")
        for position, raw_call in enumerate(raw_calls):
            if not isinstance(raw_call, Mapping):
                raise ValueError("stream tool call must be an object")
            index = 0 if legacy else raw_call.get("index", position)
            if isinstance(index, bool) or not isinstance(index, int) or index < 0:
                raise ValueError("stream tool call index must be non-negative")
            function = raw_call.get("function", raw_call)
            if not isinstance(function, Mapping):
                raise ValueError("stream tool call function must be an object")
            current = self.tool_calls.setdefault(
                index,
                {"id": "", "name": "", "arguments": ""},
            )
            call_id = raw_call.get("id")
            if call_id is not None:
                if not isinstance(call_id, str):
                    raise ValueError("stream tool call id must be a string")
                if call_id != current["id"]:
                    current["id"] += call_id
            name = function.get("name")
            if name is not None:
                if not isinstance(name, str):
                    raise ValueError("stream function name must be a string")
                if name != current["name"]:
                    current["name"] += name
            arguments = function.get("arguments")
            if arguments is not None:
                if not isinstance(arguments, str):
                    raise ValueError("stream function arguments must be a string")
                current["arguments"] += arguments
                if arguments:
                    events.append(ToolUseDelta(index=index, partial_json=arguments))

    def finish(self) -> Reply:
        calls: list[dict[str, Any]] = []
        for index in sorted(self.tool_calls):
            raw = self.tool_calls[index]
            name = raw["name"]
            if not name:
                raise ValueError("streamed tool call has no function name")
            call_id = raw["id"] or OpenAICompatibleAdapter._derived_call_id(
                self.response_identity,
                index,
                name,
                raw["arguments"],
            )
            calls.append({
                "type": "tool_use",
                "id": call_id,
                "name": name,
                "input": OpenAICompatibleAdapter._arguments(raw["arguments"]),
            })
        return OpenAICompatibleAdapter._make_reply(
            request=self.request,
            model=self.model,
            text="".join(self.text),
            tool_calls=calls,
            finish_reason=self.finish_reason,
            usage=self.usage,
        )

    def partial_content(self) -> tuple[Mapping[str, Any], ...]:
        if not self.text:
            return ()
        return ({"type": "text", "text": "".join(self.text)},)
