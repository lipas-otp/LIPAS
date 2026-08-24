"""Stable, provider-neutral events emitted by Agent runs."""
from __future__ import annotations

import inspect
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

__all__ = ["AgentEvent", "AgentEventType", "EventEmitter", "EventSink"]


class AgentEventType:
    RUN_STARTED = "run_started"
    MODEL_STARTED = "model_started"
    MODEL_DELTA = "model_delta"
    MODEL_THINKING = "model_thinking"
    MODEL_TOOL_DELTA = "model_tool_delta"
    MODEL_COMPLETED = "model_completed"
    TOOL_REQUESTED = "tool_requested"
    TOOL_STARTED = "tool_started"
    TOOL_COMPLETED = "tool_completed"
    OBSERVER_RECOMMENDATION = "observer_recommendation"
    HANDOFF_STARTED = "handoff_started"
    HANDOFF_COMPLETED = "handoff_completed"
    HANDOFF_FAILED = "handoff_failed"
    HANDOFF_CANCELLED = "handoff_cancelled"
    RUN_COMPLETED = "run_completed"
    RUN_FAILED = "run_failed"
    RUN_CANCELLED = "run_cancelled"


@dataclass(frozen=True, slots=True)
class AgentEvent:
    """One ordered application-facing event from an Agent run."""

    type: str
    run_id: str
    sequence: int
    iteration: int = 0
    data: Mapping[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    event_id: str = field(default_factory=lambda: f"event_{uuid.uuid4().hex}")

    def __post_init__(self) -> None:
        if not isinstance(self.type, str) or not self.type.strip():
            raise ValueError("AgentEvent.type must be a non-empty string")
        if not isinstance(self.run_id, str) or not self.run_id.strip():
            raise ValueError("AgentEvent.run_id must be a non-empty string")
        if isinstance(self.sequence, bool) or not isinstance(self.sequence, int):
            raise TypeError("AgentEvent.sequence must be an int")
        if self.sequence < 1:
            raise ValueError("AgentEvent.sequence must be positive")
        if isinstance(self.iteration, bool) or not isinstance(self.iteration, int):
            raise TypeError("AgentEvent.iteration must be an int")
        if self.iteration < 0:
            raise ValueError("AgentEvent.iteration must be non-negative")
        if not isinstance(self.data, Mapping):
            raise TypeError("AgentEvent.data must be a mapping")
        if not isinstance(self.event_id, str) or not self.event_id.strip():
            raise ValueError("AgentEvent.event_id must be a non-empty string")

    @property
    def kind(self) -> str:
        return self.type

    @property
    def payload(self) -> Mapping[str, Any]:
        return self.data

    @property
    def cursor(self) -> int:
        return self.sequence

    def as_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "type": self.type,
            "run_id": self.run_id,
            "sequence": self.sequence,
            "iteration": self.iteration,
            "data": dict(self.data),
            "created_at": self.created_at,
        }


EventSink = Callable[[AgentEvent], Awaitable[None] | None]


class EventEmitter:
    """Assign per-run sequence numbers and deliver events to a sink."""

    def __init__(self, run_id: str, sink: EventSink, *, sequence: int = 0) -> None:
        if not isinstance(run_id, str) or not run_id.strip():
            raise ValueError("run_id must be a non-empty string")
        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 0:
            raise ValueError("sequence must be a non-negative int")
        self.run_id = run_id
        self.sink = sink
        self._sequence = sequence

    @property
    def sequence(self) -> int:
        return self._sequence

    async def emit(
        self,
        event_type: str,
        *,
        iteration: int = 0,
        data: Mapping[str, Any] | None = None,
    ) -> AgentEvent:
        self._sequence += 1
        event = AgentEvent(
            type=event_type,
            run_id=self.run_id,
            sequence=self._sequence,
            iteration=iteration,
            data=dict(data or {}),
        )
        delivered = self.sink(event)
        if inspect.isawaitable(delivered):
            await delivered
        return event
