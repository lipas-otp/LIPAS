"""Experimental dependency-light LangGraph node and tool adapters."""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Mapping, MutableMapping

from ..coordination import AgentCoordinator, HandoffEnvelope
from ..gateway import ActionGateway

__all__ = [
    "LangGraphActionNode",
    "LangGraphHandoffNode",
    "LangGraphToolAdapter",
]


@dataclass
class LangGraphActionNode:
    """Async LangGraph node that executes one state-carried LIPAS action.

    Expected input under ``input_key``::

        {"tool_name": "save", "arguments": {...}, "request_id": "stable-id"}

    Put an ordinary LangGraph ``interrupt()`` node before this node for human
    approval, then construct this adapter with ``approved=True``. Requiring a
    stable request id makes graph/node replay return the recorded Effect rather
    than submit the action again.
    """

    gateway: ActionGateway
    input_key: str = "action"
    output_key: str = "action_result"
    approved: bool = False
    timeout_s: float | None = None

    async def __call__(
        self,
        state: Mapping[str, Any],
        config: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        action = state.get(self.input_key)
        if not isinstance(action, Mapping):
            raise TypeError(f"state[{self.input_key!r}] must be a mapping")
        tool_name = action.get("tool_name")
        arguments = action.get("arguments", {})
        request_id = action.get("request_id")
        if not isinstance(tool_name, str) or not tool_name:
            raise ValueError("LangGraph action requires tool_name")
        if not isinstance(arguments, Mapping):
            raise TypeError("LangGraph action arguments must be a mapping")
        if not isinstance(request_id, str) or not request_id:
            raise ValueError(
                "LangGraph action requires a stable request_id for safe replay",
            )
        result = await self.gateway.call(
            tool_name,
            arguments,
            request_id=request_id,
            approved=self.approved,
            timeout_s=self.timeout_s,
            caused_by=_thread_id(config),
        )
        return {self.output_key: result.as_dict()}


class LangGraphToolAdapter:
    """Tool-like wrapper with ``invoke``/``ainvoke`` and optional LangChain export."""

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

    async def ainvoke(
        self,
        input: Mapping[str, Any],
        config: Mapping[str, Any] | None = None,
        **_: Any,
    ) -> dict[str, Any]:
        arguments: MutableMapping[str, Any] = dict(input)
        request_id = arguments.pop("_lipas_request_id", None)
        if request_id is None and config is not None:
            request_id = config.get("run_id")
        if request_id is None:
            request_id = f"langgraph_{uuid.uuid4().hex}"
        result = await self.gateway.call(
            self.tool_name,
            arguments,
            request_id=str(request_id),
            approved=self.approved,
            timeout_s=self.timeout_s,
            caused_by=_thread_id(config),
        )
        return result.as_dict()

    def invoke(
        self,
        input: Mapping[str, Any],
        config: Mapping[str, Any] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        return self.gateway.call_sync(
            self.tool_name,
            {
                key: value for key, value in input.items()
                if key != "_lipas_request_id"
            },
            request_id=str(
                input.get("_lipas_request_id")
                or (config or {}).get("run_id")
                or f"langgraph_{uuid.uuid4().hex}"
            ),
            approved=self.approved,
            timeout_s=self.timeout_s,
            caused_by=_thread_id(config),
        ).as_dict()

    def as_langchain_tool(self) -> Any:
        """Return a real ``StructuredTool`` when langchain-core is installed."""
        try:
            from langchain_core.tools import StructuredTool  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise ImportError(
                "install langchain-core to export a StructuredTool",
            ) from exc

        async def call(**kwargs: Any) -> dict[str, Any]:
            return await self.ainvoke(kwargs)

        return StructuredTool.from_function(
            coroutine=call,
            name=self.name,
            description=self.description,
            args_schema=self.input_schema,
        )


@dataclass
class LangGraphHandoffNode:
    """Bridge a LangGraph state node to one durable LIPAS handoff.

    LangGraph retries must provide a stable ``handoff_id`` (normally a
    checkpoint id) through state or ``configurable``. The adapter never creates
    a random id because doing so would turn graph replay into a new side effect.
    """

    coordinator: AgentCoordinator
    recipient: str
    input_key: str = "input"
    output_key: str = "output"
    sender: str = "langgraph"
    coordination_id_key: str = "coordination_id"

    async def __call__(
        self,
        state: Mapping[str, Any],
        config: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not isinstance(state, Mapping):
            raise TypeError("LangGraph state must be a mapping")
        config = config or {}
        configurable = config.get("configurable")
        if not isinstance(configurable, Mapping):
            configurable = {}
        coordination_id = (
            state.get(self.coordination_id_key)
            or configurable.get("thread_id")
            or configurable.get("run_id")
        )
        handoff_id = (
            state.get("_lipas_handoff_id")
            or configurable.get("checkpoint_id")
            or configurable.get("run_id")
        )
        if not isinstance(coordination_id, str) or not coordination_id.strip():
            raise ValueError(
                "LangGraph handoff requires a stable coordination_id/thread_id",
            )
        if not isinstance(handoff_id, str) or not handoff_id.strip():
            raise ValueError(
                "LangGraph handoff requires a stable checkpoint_id or handoff id",
            )
        envelope = HandoffEnvelope.create(
            coordination_id=coordination_id,
            sender=self.sender,
            recipient=self.recipient,
            payload=state.get(self.input_key),
            handoff_id=handoff_id,
            metadata={
                "framework": "langgraph",
                "thread_id": str(configurable.get("thread_id", coordination_id)),
            },
        )
        outcome = await self.coordinator.dispatch(envelope)
        return {
            self.output_key: outcome.value,
            "_lipas_run_id": outcome.run_id,
            "_lipas_handoff_id": envelope.id,
            "_lipas_replayed": outcome.replayed,
        }


def _thread_id(config: Mapping[str, Any] | None) -> str | None:
    if config is None:
        return None
    configurable = config.get("configurable")
    if isinstance(configurable, Mapping):
        value = configurable.get("thread_id")
        if value is not None:
            return str(value)
    value = config.get("run_id")
    return str(value) if value is not None else None
