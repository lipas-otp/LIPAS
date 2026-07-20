"""Experimental OpenClaw/OpenCrew action backend contract."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from ..gateway import ActionGateway

__all__ = ["OpenClawActionBackend"]


@dataclass
class OpenClawActionBackend:
    """Translate an OpenClaw/OpenCrew action envelope into a LIPAS Effect.

    The host should generate one stable ``request_id`` per logical delegated
    action. ``trust_caller_approval`` is deliberately false by default; use it
    only when the calling OpenClaw surface authenticates approval decisions.
    """

    gateway: ActionGateway
    trust_caller_approval: bool = False

    async def execute(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        tool_name = payload.get("tool_name")
        arguments = payload.get("arguments", {})
        request_id = payload.get("request_id")
        if not isinstance(tool_name, str) or not tool_name:
            raise ValueError("OpenClaw action requires tool_name")
        if not isinstance(arguments, Mapping):
            raise TypeError("OpenClaw action arguments must be a mapping")
        if not isinstance(request_id, str) or not request_id:
            raise ValueError(
                "OpenClaw action requires a stable request_id for redelivery",
            )
        approved = (
            self.trust_caller_approval
            and payload.get("approved") is True
        )
        result = await self.gateway.call(
            tool_name,
            arguments,
            request_id=request_id,
            approved=approved,
            caused_by=str(payload.get("task_id") or request_id),
        )
        return {
            **result.as_dict(),
            "closeout": {
                "action_recorded": True,
                "safe_to_redeliver": result.status in {
                    "ok", "error", "rejected", "approval_required",
                },
                "requires_reconciliation": result.status == "uncertain",
            },
        }

    def tool_manifest(self) -> dict[str, Any]:
        """Generic tool manifest suitable for an OpenClaw skill/plugin shim."""
        return {
            "name": "lipas_action",
            "description": (
                "Execute a real action through LIPAS approval, audit, budget, "
                "idempotency, and recovery policy."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "tool_name": {"type": "string"},
                    "arguments": {"type": "object"},
                    "request_id": {"type": "string"},
                    "task_id": {"type": "string"},
                    "approved": {"type": "boolean"},
                },
                "required": ["tool_name", "arguments", "request_id"],
            },
        }
