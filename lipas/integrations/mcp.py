"""MCP server and first-party client boundaries.

The client deliberately keeps MCP transport-neutral.  Applications may use
the included HTTP transport or provide a stdio/in-process ``send`` callable;
MCP session state never becomes a second LIPAS execution authority.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import sys
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Mapping, TextIO, Protocol
from urllib.parse import urlsplit

from .._version import __version__
from ..gateway import ActionGateway
from ..http_client import EgressPolicy
from ..operations import OperationJournal, OperationStateError

__all__ = ["MCPActionServer", "MCPClient", "MCPClientError", "MCPHttpClient"]


def _finite_number(value: Any, name: str, *, positive: bool = False) -> float:
    """Validate MCP transport numbers without exposing conversion errors."""
    try:
        valid = (
            not isinstance(value, bool)
            and isinstance(value, (int, float))
            and math.isfinite(float(value))
            and (not positive or value > 0)
        )
    except (OverflowError, TypeError, ValueError):
        valid = False
    if not valid:
        qualifier = "finite and positive" if positive else "finite"
        raise ValueError(f"{name} must be {qualifier}")
    return float(value)


def _valid_rpc_id(value: Any) -> bool:
    """JSON-RPC request/response ids may be string, number, or null."""
    return value is None or (
        isinstance(value, (str, int, float))
        and not isinstance(value, bool)
        and (not isinstance(value, float) or math.isfinite(value))
    )


def _strict_json_copy(value: Any, name: str) -> Any:
    """Detach a JSON value and reject coercive/non-finite Python values."""
    active: set[int] = set()

    def validate(item: Any, path: str) -> None:
        if item is None or isinstance(item, (bool, int, str)):
            return
        if isinstance(item, float):
            if not math.isfinite(item):
                raise ValueError(f"{path} contains a non-finite number")
            return
        if not isinstance(item, (list, tuple, Mapping)):
            raise TypeError(f"{path} contains unsupported {type(item).__name__}")
        marker = id(item)
        if marker in active:
            raise ValueError(f"{path} contains a reference cycle")
        active.add(marker)
        try:
            if isinstance(item, Mapping):
                for key, child in item.items():
                    if not isinstance(key, str):
                        raise ValueError(f"{path} must use string object keys")
                    validate(child, f"{path}.{key}")
            else:
                for index, child in enumerate(item):
                    validate(child, f"{path}[{index}]")
        finally:
            active.remove(marker)

    try:
        validate(value, name)
        encoded = json.dumps(
            value, sort_keys=True, separators=(",", ":"),
            ensure_ascii=False, allow_nan=False,
        )
        return json.loads(encoded)
    except (TypeError, ValueError, RecursionError) as exc:
        raise MCPClientError(f"{name} must be strict JSON") from exc


class MCPClientError(RuntimeError):
    """MCP transport or JSON-RPC failure."""


class MCPTransport(Protocol):
    async def send(
        self, message: Mapping[str, Any],
    ) -> Mapping[str, Any] | None: ...


# A notification has no JSON-RPC response.  Keep this alias explicit so
# in-process adapters can faithfully return ``None`` for notifications while
# request/response transports still return a mapping.
MCPTransportCallable = Callable[
    [Mapping[str, Any]], Awaitable[Mapping[str, Any] | None]
]


@dataclass
class MCPClient:
    """Small JSON-RPC MCP client suitable for connectors and tests."""

    transport: MCPTransport | MCPTransportCallable
    protocol_version: str = "2025-06-18"
    _next_id: int = 0
    # Keep JSON-RPC ids compact for compatibility, while making provider
    # correlation identities unique across client restarts.
    _client_instance_id: str = field(
        default_factory=lambda: uuid.uuid4().hex,
        init=False,
        repr=False,
    )

    async def request(self, method: str, params: Mapping[str, Any] | None = None) -> Any:
        if not isinstance(method, str) or not method.strip():
            raise ValueError("MCP method must be a non-empty string")
        method = method.strip()
        if params is not None and not isinstance(params, Mapping):
            raise TypeError("MCP params must be a mapping or None")
        self._next_id += 1
        normalized_params = (
            None if params is None else _strict_json_copy(dict(params), "MCP params")
        )
        message = {
            "jsonrpc": "2.0", "id": self._next_id,
            "method": method, "params": normalized_params or {},
        }
        try:
            json.dumps(
                message,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            )
        except (TypeError, ValueError, RecursionError) as exc:
            raise MCPClientError("MCP request must be strict JSON") from exc
        sender = self.transport.send if hasattr(self.transport, "send") else self.transport
        response = await sender(message)  # type: ignore[misc]
        if not isinstance(response, Mapping):
            raise MCPClientError("MCP response must be an object")
        response = _strict_json_copy(dict(response), "MCP response")
        if not isinstance(response, Mapping):  # defensive type narrowing
            raise MCPClientError("MCP response must be an object")
        if response.get("jsonrpc") != "2.0":
            raise MCPClientError("MCP response must use JSON-RPC 2.0")
        response_id = response.get("id")
        if not _valid_rpc_id(response_id) or response_id != message["id"]:
            raise MCPClientError(
                f"MCP {method} response id {response_id!r} does not match "
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
        if arguments is not None and not isinstance(arguments, Mapping):
            raise TypeError("MCP tool arguments must be a mapping or None")
        if not isinstance(request_id, str) or not request_id.strip():
            raise ValueError("MCP tool calls require a non-empty request_id")
        request_id = request_id.strip()
        params: dict[str, Any] = {"name": name, "arguments": dict(arguments or {})}
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
    max_response_bytes: int = 10 * 1024 * 1024
    journal: OperationJournal | None = None
    _client_instance_id: str = field(
        default_factory=lambda: uuid.uuid4().hex,
        init=False,
        repr=False,
    )

    def __post_init__(self) -> None:
        if not isinstance(self.url, str) or not self.url.strip():
            raise ValueError("MCP HTTP url must be a non-empty string")
        parsed = urlsplit(self.url)
        if (
            parsed.scheme.lower() not in {"https", "http"}
            or not parsed.hostname
            or parsed.query
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
        ):
            raise MCPClientError(
                "MCP HTTP url must be an absolute HTTP(S) URL without credentials"
            )
        object.__setattr__(
            self, "timeout_s", _finite_number(self.timeout_s, "timeout_s", positive=True),
        )
        if (
            isinstance(self.max_response_bytes, bool)
            or not isinstance(self.max_response_bytes, int)
            or self.max_response_bytes < 1
        ):
            raise ValueError("max_response_bytes must be a positive int")
        if self.headers is not None and not isinstance(self.headers, Mapping):
            raise TypeError("MCP headers must be a mapping or None")
        for key, value in dict(self.headers or {}).items():
            if not isinstance(key, str) or not key.strip():
                raise ValueError("MCP header names must be non-empty strings")
            if not isinstance(value, str):
                raise TypeError("MCP header values must be strings")
            if "\r" in key or "\n" in key or "\r" in value or "\n" in value:
                raise ValueError("MCP headers must not contain CR/LF")
        if self.egress is None:
            # Match HttpClient's fail-closed default: HTTPS is required and
            # only the configured endpoint host is reachable.
            object.__setattr__(
                self, "egress", EgressPolicy(frozenset({parsed.hostname.lower()})),
            )
        if self.journal is not None and not isinstance(self.journal, OperationJournal):
            raise TypeError("journal must be an OperationJournal or None")

    async def send(self, message: Mapping[str, Any]) -> Mapping[str, Any]:
        if not isinstance(message, Mapping):
            raise TypeError("MCP message must be a mapping")
        message = _strict_json_copy(dict(message), "MCP message")
        if not isinstance(message, Mapping):
            raise MCPClientError("MCP message must be a mapping")
        if message.get("jsonrpc") != "2.0":
            raise MCPClientError("MCP message must use JSON-RPC 2.0")
        method = message.get("method")
        if not isinstance(method, str) or not method.strip():
            raise MCPClientError("MCP message method must be a non-empty string")
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
        request_identity = requested_id.strip() if isinstance(requested_id, str) else None
        # Read-only/control MCP requests still receive a process-local
        # correlation id so logs and provider traces remain joinable.  A
        # tools/call write must carry an explicit host-owned identity: a
        # synthetic id would make retries indistinguishable from a new write.
        if request_identity is None and method.strip() != "tools/call":
            message_id = message.get("id")
            if message_id is not None:
                request_identity = f"mcp:{self._client_instance_id}:{message_id}"
        if method.strip() == "tools/call" and request_identity is None:
            raise MCPClientError(
                "MCP tools/call requires params._lipas_request_id for safe replay",
            )
        operation = None
        if method.strip() == "tools/call" and self.journal is not None:
            try:
                request_digest = hashlib.sha256(
                    json.dumps(
                        dict(message), sort_keys=True, separators=(",", ":"),
                        ensure_ascii=False, allow_nan=False,
                    ).encode("utf-8"),
                ).hexdigest()
            except (TypeError, ValueError) as exc:
                raise MCPClientError("MCP message must be strict JSON") from exc
            operation, owns_submission = self.journal._prepare(
                key=request_identity or "",
                kind="mcp_tools_call",
                request={
                    "url": self.url,
                    "method": method.strip(),
                    "message_sha256": request_digest,
                    "provider_request_id": request_identity,
                },
                effect_id=None,
                provider_request_id=request_identity,
            )
            if operation.state == "succeeded":
                if not isinstance(operation.result, Mapping):
                    raise MCPClientError("journalled MCP result is malformed")
                return dict(operation.result)
            if not owns_submission:
                raise MCPClientError(
                    f"MCP operation {operation.key!r} is {operation.state}; reconcile first",
                )
        if request_identity is not None:
            for key in tuple(request_headers):
                if key.lower() in {"x-request-id", "idempotency-key"}:
                    del request_headers[key]
            request_headers["X-Request-ID"] = str(request_identity)
            if method.strip() == "tools/call":
                request_headers["Idempotency-Key"] = str(request_identity)
        try:
            if self.client is not None:
                response = await self.client.post(
                    self.url, json=message, headers=request_headers,
                    timeout=self.timeout_s, follow_redirects=False,
                )
            else:
                try:
                    import httpx
                except ImportError as exc:  # pragma: no cover - optional dependency
                    raise MCPClientError("install lipas[compatible] for MCPHttpClient") from exc
                async with httpx.AsyncClient(timeout=self.timeout_s) as client:
                    response = await client.post(
                        self.url, json=message, headers=request_headers,
                        follow_redirects=False,
                    )
        except asyncio.CancelledError as exc:
            if operation is not None:
                self._mark_uncertain(operation.key, exc)
            raise
        except BaseException as exc:
            if operation is not None:
                self._mark_uncertain(operation.key, exc)
            if isinstance(exc, MCPClientError):
                raise
            raise MCPClientError(f"MCP HTTP transport failed: {exc}") from exc
        # Third-party transports are deliberately duck-typed.  Do not read
        # ``status_code`` blindly: a malformed response object must still
        # converge a journalled write to ``uncertain`` instead of leaking an
        # AttributeError while leaving the operation pending.
        status_code = getattr(response, "status_code", None)
        if not isinstance(status_code, int) or isinstance(
            status_code, bool,
        ) or not 100 <= status_code <= 599:
            cause = MCPClientError("MCP HTTP response has invalid status")
            if operation is not None:
                self._mark_uncertain(operation.key, cause)
            raise cause
        if status_code >= 400:
            if operation is not None and self.journal is not None:
                self.journal.fail(
                    operation.key,
                    error={"type": "mcp_http_error", "status_code": status_code},
                )
            raise MCPClientError(f"MCP HTTP status {status_code}")
        if 300 <= status_code < 400:
            cause = MCPClientError(
                f"MCP HTTP request was redirected (status {status_code})",
            )
            if operation is not None:
                self._mark_uncertain(operation.key, cause)
            raise cause
        try:
            returned_url = str(getattr(response, "url", self.url))
            if not self._same_origin(self.url, returned_url):
                raise MCPClientError(
                    "MCP HTTP response origin differs from requested origin",
                )
            content = getattr(response, "content", b"")
            if not isinstance(content, (bytes, bytearray)):
                raise MCPClientError("MCP HTTP response content must be bytes")
            if len(content) > self.max_response_bytes:
                raise MCPClientError("MCP HTTP response exceeds max_response_bytes")
            if status_code in {202, 204} or not content:
                if operation is not None:
                    cause = MCPClientError(
                        "MCP write response has no JSON-RPC result; outcome is uncertain",
                    )
                    self._mark_uncertain(operation.key, cause)
                    raise cause
                payload: Mapping[str, Any] = {}
                return payload
            content_type = self._header(response.headers, "content-type").lower()
            if "text/event-stream" in content_type:
                payload = self._sse_payload(bytes(content), response_id=message.get("id"))
            else:
                try:
                    payload = response.json()
                except ValueError as exc:
                    raise MCPClientError("MCP HTTP response is not JSON") from exc
            if not isinstance(payload, Mapping):
                raise MCPClientError("MCP HTTP response must be an object")
            payload = _strict_json_copy(dict(payload), "MCP HTTP response")
            if not isinstance(payload, Mapping):
                raise MCPClientError("MCP HTTP response must be an object")
            if payload.get("jsonrpc") != "2.0":
                raise MCPClientError("MCP HTTP response must use JSON-RPC 2.0")
            request_id = message.get("id")
            if request_id is not None:
                if not _valid_rpc_id(request_id) or payload.get("id") != request_id:
                    raise MCPClientError("MCP HTTP response id does not match request")
            if operation is not None and self.journal is not None:
                self.journal.settle(
                    operation.key,
                    result=dict(payload),
                    provider_reference=self._header(response.headers, "x-request-id") or None,
                )
            return payload
        except asyncio.CancelledError as exc:
            if operation is not None:
                self._mark_uncertain(operation.key, exc)
            raise
        except BaseException as exc:
            if operation is not None:
                self._mark_uncertain(operation.key, exc)
            if isinstance(exc, MCPClientError):
                raise
            raise MCPClientError(f"MCP HTTP response processing failed: {exc}") from exc

    def _mark_uncertain(self, key: str, cause: BaseException) -> None:
        if self.journal is None:
            return
        try:
            self.journal.mark_uncertain(
                key,
                error={"type": type(cause).__name__, "message": str(cause)},
            )
        except OperationStateError:
            # A concurrent reconciler/settler has the authoritative outcome.
            return

    @staticmethod
    def _header(headers: Mapping[str, Any], name: str) -> str:
        wanted = name.lower()
        for key, value in headers.items():
            if str(key).lower() == wanted:
                return "" if value is None else str(value).strip()
        return ""

    @staticmethod
    def _same_origin(requested: str, returned: str) -> bool:
        try:
            left = urlsplit(requested)
            right = urlsplit(returned)
            left_port = left.port or {"http": 80, "https": 443}.get(left.scheme.lower())
            right_port = right.port or {"http": 80, "https": 443}.get(right.scheme.lower())
        except (TypeError, ValueError):
            return False
        return (
            left.scheme.lower() == right.scheme.lower()
            and (left.hostname or "").lower() == (right.hostname or "").lower()
            and left_port == right_port
        )

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
                        events.append(_strict_json_copy(json.loads(
                            "\n".join(data),
                            parse_constant=lambda raw: (_ for _ in ()).throw(
                                ValueError(f"non-JSON numeric constant {raw!r}")
                            ),
                        ), "MCP SSE event"))
                    except ValueError as exc:
                        raise MCPClientError("MCP SSE event is not JSON") from exc
                    data.clear()
                continue
            if line.startswith("data:"):
                data.append(line[5:].lstrip())
        if data:
            try:
                events.append(_strict_json_copy(json.loads(
                    "\n".join(data),
                    parse_constant=lambda raw: (_ for _ in ()).throw(
                        ValueError(f"non-JSON numeric constant {raw!r}")
                    ),
                ), "MCP SSE event"))
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
        if not isinstance(message, Mapping):
            return self._error(None, -32600, "JSON-RPC message must be an object")
        try:
            message = _strict_json_copy(dict(message), "JSON-RPC message")
        except (TypeError, ValueError, MCPClientError) as exc:
            return self._error(message.get("id"), -32600, str(exc))
        if not isinstance(message, Mapping):
            return self._error(None, -32600, "JSON-RPC message must be an object")
        if message.get("jsonrpc") != "2.0":
            return self._error(message.get("id"), -32600, "JSON-RPC version must be 2.0")
        method = message.get("method")
        if not isinstance(method, str) or not method.strip():
            return self._error(message.get("id"), -32600, "method must be a non-empty string")
        request_id = message.get("id")
        if "id" in message and not _valid_rpc_id(request_id):
            return self._error(request_id, -32600, "JSON-RPC id is invalid")
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
            supplied_request_id = params.get("_lipas_request_id")
            if request_id is None and supplied_request_id is None:
                # A notification has no replayable JSON-RPC identity. Never
                # turn it into a shared synthetic operation key: a write
                # redelivery would otherwise be indistinguishable from the
                # first submission.
                return self._error(
                    request_id, -32600,
                    "tools/call requires a non-null JSON-RPC id or "
                    "_lipas_request_id",
                )
            if request_id is not None and not _valid_rpc_id(request_id):
                return self._error(request_id, -32600, "JSON-RPC id is invalid")
            name = params.get("name")
            arguments = params.get("arguments", {})
            if not isinstance(name, str) or not isinstance(arguments, Mapping):
                return self._error(request_id, -32602, "invalid tool name or arguments")
            if supplied_request_id is not None and (
                not isinstance(supplied_request_id, str)
                or not supplied_request_id.strip()
            ):
                return self._error(
                    request_id, -32602,
                    "_lipas_request_id must be a non-empty string",
                )
            gateway_request_id = (
                supplied_request_id.strip()
                if isinstance(supplied_request_id, str)
                else f"mcp:{request_id}:{name}"
            )
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
