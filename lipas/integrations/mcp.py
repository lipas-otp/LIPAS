"""MCP server and first-party client boundaries.

The client deliberately keeps MCP transport-neutral.  Applications may use
the included HTTP transport or provide a stdio/in-process ``send`` callable;
MCP session state never becomes a second LIPAS execution authority.
"""
from __future__ import annotations

import asyncio
import json
import math
import sys
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Mapping, TextIO, Protocol
from urllib.parse import urlsplit

from .._version import __version__
from ..gateway import ActionGateway
from ..http_client import EgressPolicy

__all__ = ["MCPActionServer", "MCPClient", "MCPClientError", "MCPHttpClient"]


class MCPClientError(RuntimeError):
    """MCP transport or JSON-RPC failure."""


class MCPTransport(Protocol):
    async def send(self, message: Mapping[str, Any]) -> Mapping[str, Any]: ...


@dataclass
class MCPClient:
    """Small JSON-RPC MCP client suitable for connectors and tests."""

    transport: MCPTransport | Callable[[Mapping[str, Any]], Awaitable[Mapping[str, Any]]]
    protocol_version: str = "2025-06-18"
    _next_id: int = 0

    async def request(self, method: str, params: Mapping[str, Any] | None = None) -> Any:
        if not isinstance(method, str) or not method.strip():
            raise ValueError("MCP method must be a non-empty string")
        if params is not None and not isinstance(params, Mapping):
            raise TypeError("MCP params must be a mapping or None")
        self._next_id += 1
        message = {
            "jsonrpc": "2.0", "id": self._next_id,
            "method": method, "params": dict(params or {}),
        }
        sender = self.transport.send if hasattr(self.transport, "send") else self.transport
        response = await sender(message)  # type: ignore[misc]
        if not isinstance(response, Mapping):
            raise MCPClientError("MCP response must be an object")
        if response.get("id") != message["id"]:
            raise MCPClientError(
                f"MCP {method} response id {response.get('id')!r} does not match "
                f"request id {message['id']!r}"
            )
        if isinstance(response.get("error"), Mapping):
            error = response["error"]
            raise MCPClientError(
                f"MCP {method} failed ({error.get('code')}): {error.get('message')}"
            )
        if "result" not in response:
            raise MCPClientError("MCP response has no result")
        return response["result"]

    async def initialize(self) -> Mapping[str, Any]:
        result = await self.request(
            "initialize", {"protocolVersion": self.protocol_version, "capabilities": {}}
        )
        if not isinstance(result, Mapping):
            raise MCPClientError("MCP initialize result must be an object")
        sender = self.transport.send if hasattr(self.transport, "send") else self.transport
        delivered = sender({
            "jsonrpc": "2.0", "method": "notifications/initialized",
        })  # type: ignore[misc]
        if delivered is not None:
            await delivered
        return result

    async def list_tools(self) -> tuple[Mapping[str, Any], ...]:
        result = await self.request("tools/list")
        values = result.get("tools") if isinstance(result, Mapping) else None
        if not isinstance(values, list):
            raise MCPClientError("MCP tools/list result has no tools array")
        return tuple(value for value in values if isinstance(value, Mapping))

    async def call_tool(
        self,
        name: str,
        arguments: Mapping[str, Any] | None = None,
        *,
        request_id: str | None = None,
    ) -> Mapping[str, Any]:
        if not isinstance(name, str) or not name.strip():
            raise ValueError("MCP tool name must be non-empty")
        if request_id is not None and (
            not isinstance(request_id, str) or not request_id.strip()
        ):
            raise ValueError("MCP request_id must be a non-empty string or None")
        params: dict[str, Any] = {"name": name, "arguments": dict(arguments or {})}
        if request_id is not None:
            params["_lipas_request_id"] = request_id
        result = await self.request("tools/call", params)
        if not isinstance(result, Mapping):
            raise MCPClientError("MCP tools/call result must be an object")
        return result


@dataclass
class MCPHttpClient:
    """Streamable JSON-RPC-over-HTTP MCP transport."""

    url: str
    headers: Mapping[str, str] | None = None
    timeout_s: float = 30.0
    client: Any | None = None
    egress: EgressPolicy | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.url, str) or not self.url.strip():
            raise ValueError("MCP HTTP url must be a non-empty string")
        parsed = urlsplit(self.url)
        if (
            parsed.scheme not in {"https", "http"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
        ):
            raise MCPClientError(
                "MCP HTTP url must be an absolute HTTP(S) URL without credentials"
            )
        if (
            isinstance(self.timeout_s, bool)
            or not isinstance(self.timeout_s, (int, float))
            or not math.isfinite(float(self.timeout_s))
            or self.timeout_s <= 0
        ):
            raise ValueError("timeout_s must be finite and positive")
        if self.egress is None:
            # Match HttpClient's fail-closed default: HTTPS is required and
            # only the configured endpoint host is reachable.
            object.__setattr__(
                self, "egress", EgressPolicy(frozenset({parsed.hostname.lower()})),
            )

    async def send(self, message: Mapping[str, Any]) -> Mapping[str, Any]:
        assert self.egress is not None
        self.egress.check(self.url)
        request_headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            **dict(self.headers or {}),
        }
        params = message.get("params")
        requested_id = (
            params.get("_lipas_request_id")
            if isinstance(params, Mapping) else None
        )
        if requested_id is not None and (
            not isinstance(requested_id, str) or not requested_id.strip()
        ):
            raise MCPClientError("_lipas_request_id must be a non-empty string")
        request_identity = requested_id or (
            f"mcp:{message['id']}" if message.get("id") is not None else None
        )
        if request_identity is not None:
            for key in tuple(request_headers):
                if key.lower() in {"x-request-id", "idempotency-key"}:
                    del request_headers[key]
            request_headers["X-Request-ID"] = str(request_identity)
            if message.get("method") == "tools/call":
                request_headers["Idempotency-Key"] = str(request_identity)
        if self.client is not None:
            response = await self.client.post(
                self.url, json=message, headers=request_headers, timeout=self.timeout_s,
            )
        else:
            try:
                import httpx
            except ImportError as exc:  # pragma: no cover - optional dependency
                raise MCPClientError("install lipas[compatible] for MCPHttpClient") from exc
            async with httpx.AsyncClient(timeout=self.timeout_s) as client:
                response = await client.post(
                    self.url, json=message, headers=request_headers,
                )
        if response.status_code >= 400:
            raise MCPClientError(f"MCP HTTP status {response.status_code}")
        if response.status_code in {202, 204} or not response.content:
            return {}
        content_type = self._header(response.headers, "content-type").lower()
        if "text/event-stream" in content_type:
            payload = self._sse_payload(response.content, response_id=message.get("id"))
        else:
            try:
                payload = response.json()
            except ValueError as exc:
                raise MCPClientError("MCP HTTP response is not JSON") from exc
        if not isinstance(payload, Mapping):
            raise MCPClientError("MCP HTTP response must be an object")
        return payload

    @staticmethod
    def _header(headers: Mapping[str, Any], name: str) -> str:
        wanted = name.lower()
        for key, value in headers.items():
            if str(key).lower() == wanted:
                return str(value)
        return ""

    @staticmethod
    def _sse_payload(content: bytes, *, response_id: Any = None) -> Any:
        """Decode the matching JSON-RPC event from a streamable response."""
        events: list[Any] = []
        data: list[str] = []
        for raw_line in content.decode("utf-8", errors="replace").splitlines():
            line = raw_line.rstrip("\r")
            if not line:
                if data:
                    try:
                        events.append(json.loads("\n".join(data)))
                    except ValueError as exc:
                        raise MCPClientError("MCP SSE event is not JSON") from exc
                    data.clear()
                continue
            if line.startswith("data:"):
                data.append(line[5:].lstrip())
        if data:
            try:
                events.append(json.loads("\n".join(data)))
            except ValueError as exc:
                raise MCPClientError("MCP SSE event is not JSON") from exc
        if not events:
            raise MCPClientError("MCP SSE response contained no data event")
        if response_id is not None:
            for event in events:
                if isinstance(event, Mapping) and event.get("id") == response_id:
                    return event
            raise MCPClientError(
                f"MCP SSE response contained no event for request id {response_id!r}",
            )
        return events[-1]


@dataclass
class MCPActionServer:
    gateway: ActionGateway
    name: str = "lipas-action-gateway"
    version: str = __version__

    async def handle(self, message: Mapping[str, Any]) -> Mapping[str, Any] | None:
        method = message.get("method")
        request_id = message.get("id")
        if method == "notifications/initialized":
            return None
        if method == "initialize":
            params = message.get("params")
            requested = params.get("protocolVersion") if isinstance(params, Mapping) else None
            return self._ok(request_id, {
                "protocolVersion": requested or "2025-06-18",
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": self.name, "version": self.version},
                "instructions": (
                    "LIPAS audits every action. Write tools fail closed unless "
                    "the operator started this server with write authority."
                ),
            })
        if method == "ping":
            return self._ok(request_id, {})
        if method == "tools/list":
            return self._ok(request_id, {
                "tools": [self._tool_shape(value) for value in self.gateway.specs()],
            })
        if method == "tools/call":
            params = message.get("params")
            if not isinstance(params, Mapping):
                return self._error(request_id, -32602, "tools/call params must be an object")
            name = params.get("name")
            arguments = params.get("arguments", {})
            if not isinstance(name, str) or not isinstance(arguments, Mapping):
                return self._error(request_id, -32602, "invalid tool name or arguments")
            supplied_request_id = params.get("_lipas_request_id")
            if supplied_request_id is not None and (
                not isinstance(supplied_request_id, str)
                or not supplied_request_id.strip()
            ):
                return self._error(
                    request_id, -32602,
                    "_lipas_request_id must be a non-empty string",
                )
            gateway_request_id = supplied_request_id or f"mcp:{request_id}:{name}"
            result = await self.gateway.call(
                name,
                arguments,
                request_id=gateway_request_id,
                approved=self.gateway.allow_writes,
                caused_by=f"mcp:{gateway_request_id}",
            )
            return self._ok(request_id, {
                "content": [{
                    "type": "text",
                    "text": json.dumps(result.as_dict(), ensure_ascii=False),
                }],
                "isError": result.is_error,
                "structuredContent": result.as_dict(),
            })
        return self._error(request_id, -32601, f"method not found: {method}")

    async def serve_stdio(
        self,
        stdin: TextIO | None = None,
        stdout: TextIO | None = None,
    ) -> None:
        source = stdin or sys.stdin
        sink = stdout or sys.stdout
        while True:
            line = await asyncio.to_thread(source.readline)
            if not line:
                return
            try:
                message = json.loads(line)
                if not isinstance(message, Mapping):
                    raise TypeError("JSON-RPC message must be an object")
                response = await self.handle(message)
            except Exception as exc:
                response = self._error(None, -32700, f"invalid request: {exc}")
            if response is not None:
                sink.write(json.dumps(response, ensure_ascii=False) + "\n")
                sink.flush()

    @staticmethod
    def _tool_shape(spec: Any) -> dict[str, Any]:
        read_only = spec.side_effect in {"pure", "read_only"}
        return {
            "name": spec.name,
            "description": spec.description,
            "inputSchema": dict(spec.input_schema),
            "annotations": {
                "readOnlyHint": read_only,
                "destructiveHint": spec.side_effect == "external_write",
                "idempotentHint": spec.side_effect != "external_write",
                "openWorldHint": spec.side_effect == "external_write",
            },
        }

    @staticmethod
    def _ok(request_id: Any, result: Any) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    @staticmethod
    def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
        return {
            "jsonrpc": "2.0", "id": request_id,
            "error": {"code": code, "message": message},
        }
