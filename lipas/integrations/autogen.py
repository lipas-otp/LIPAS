"""Dependency-light AutoGen boundary adapters.

The adapters expose LIPAS as one scoped tool/handoff. They do not import
AutoGen so applications can use them with any AutoGen generation without
coupling LIPAS core state to AutoGen's team or conversation models.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from ..coordination import AgentCoordinator
from ..gateway import ActionGateway

__all__ = ["AutoGenHandoffHandler", "AutoGenToolAdapter"]


@dataclass
class AutoGenHandoffHandler:
    """Handle one AutoGen message as a durable LIPAS handoff."""

    coordinator: AgentCoordinator
    recipient: str
    sender: str = "autogen"

    async def handle(
        self,
        message: Any,
        *,
        conversation_id: str,
        request_id: str,
        metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not isinstance(conversation_id, str) or not conversation_id.strip():
            raise ValueError("AutoGen handoff requires conversation_id")
        if not isinstance(request_id, str) or not request_id.strip():
            raise ValueError("AutoGen handoff requires stable request_id")
        outcome = await self.coordinator.handoff(
            self.recipient,
            message,
            sender=self.sender,
            coordination_id=conversation_id,
            handoff_id=request_id,
            metadata={
                "framework": "autogen",
                **dict(metadata or {}),
            },
        )
        return {
            "content": outcome.value,
            "request_id": request_id,
            "run_id": outcome.run_id,
            "replayed": outcome.replayed,
        }

    async def __call__(self, message: Any, **kwargs: Any) -> dict[str, Any]:
        return await self.handle(message, **kwargs)


class AutoGenToolAdapter:
    """Callable AutoGen-compatible wrapper around an ActionGateway Tool."""

    def __init__(
        self,
        gateway: ActionGateway,
        tool_name: str,
        *,
        approved: bool = False,
        timeout_s: float | None = None,
    ) -> None:
        self.gateway = gateway
        self.tool_name = tool_name
        self.approved = approved
        self.timeout_s = timeout_s
        tool = gateway.tools.get(tool_name)
        self.name = tool.name
        self.description = tool.description
        self.input_schema = dict(tool.parameters_schema)

    async def arun(
        self,
        arguments: Mapping[str, Any],
        *,
        request_id: str,
        **_: Any,
    ) -> dict[str, Any]:
        if not isinstance(arguments, Mapping):
            raise TypeError("AutoGen tool arguments must be a mapping")
        return (
            await self.gateway.call(
                self.tool_name,
                arguments,
                request_id=request_id,
                approved=self.approved,
                timeout_s=self.timeout_s,
                caused_by=request_id,
            )
        ).as_dict()

    def run(
        self,
        arguments: Mapping[str, Any],
        *,
        request_id: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        return self.gateway.call_sync(
            self.tool_name,
            arguments,
            request_id=request_id,
            approved=self.approved,
            timeout_s=self.timeout_s,
            caused_by=request_id,
        ).as_dict()

    def __call__(self, **kwargs: Any) -> dict[str, Any]:
        request_id = kwargs.pop("_lipas_request_id", None)
        if not isinstance(request_id, str) or not request_id.strip():
            raise ValueError(
                "AutoGen tool calls require _lipas_request_id for safe replay",
            )
        return self.run(kwargs, request_id=request_id)
