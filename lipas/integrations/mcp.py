"""Experimental MCP stdio compatibility server backed by ActionGateway."""
from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import dataclass
from typing import Any, Mapping, TextIO

from .._version import __version__
from ..gateway import ActionGateway

__all__ = ["MCPActionServer"]


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
            result = await self.gateway.call(
                name,
                arguments,
                request_id=f"mcp:{request_id}:{name}",
                approved=self.gateway.allow_writes,
                caused_by=f"mcp:{request_id}",
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
