"""Stable, provider-neutral events emitted by Agent runs."""
from __future__ import annotations

import inspect
import json
import math
import time
import uuid
from copy import deepcopy
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
    EFFECT_OBSERVED = "effect_observed"
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
        if any(char in self.type for char in "\r\n"):
            raise ValueError("AgentEvent.type must not contain CR/LF")
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
        # Events cross process/transport boundaries and are persisted by the
        # ExecutionStore.  Keep them detached from caller-owned mappings and
        # reject values that cannot be represented as strict JSON (notably
        # NaN/Infinity, which Python's default encoder otherwise accepts).
        data = deepcopy(dict(self.data))
        _validate_json_shape(data, "AgentEvent.data")
        try:
            json.dumps(
                data,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
        except (TypeError, ValueError, RecursionError) as exc:
            raise ValueError("AgentEvent.data must be strict JSON") from exc
        object.__setattr__(self, "data", data)
        if not isinstance(self.event_id, str) or not self.event_id.strip():
            raise ValueError("AgentEvent.event_id must be a non-empty string")
        try:
            finite_created_at = (
                not isinstance(self.created_at, bool)
                and isinstance(self.created_at, (int, float))
                and math.isfinite(float(self.created_at))
            )
        except (OverflowError, TypeError, ValueError):
            finite_created_at = False
        if not finite_created_at:
            raise ValueError("AgentEvent.created_at must be finite")
        object.__setattr__(self, "type", self.type.strip())
        object.__setattr__(self, "run_id", self.run_id.strip())
        object.__setattr__(self, "event_id", self.event_id.strip())
        object.__setattr__(self, "created_at", float(self.created_at))

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


def _validate_json_shape(
    value: Any,
    path: str,
    *,
    active: set[int] | None = None,
) -> None:
    """Reject Python values that JSON would otherwise coerce silently.

    ``json.dumps`` accepts integer mapping keys by converting them to text;
    that is unsafe for durable event identities because two distinct Python
    mappings can then produce the same wire representation.  Validate the
    shape explicitly and track only the active recursion path so shared
    (non-cyclic) values remain valid.
    """
    if value is None or isinstance(value, (bool, int, str)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{path} must contain finite numbers")
        return
    if not isinstance(value, (list, tuple, Mapping)):
        raise TypeError(f"{path} contains unsupported {type(value).__name__}")
    active = set() if active is None else active
    marker = id(value)
    if marker in active:
        raise ValueError(f"{path} must not contain reference cycles")
    active.add(marker)
    try:
        if isinstance(value, Mapping):
            for key, child in value.items():
                if not isinstance(key, str):
                    raise ValueError(f"{path} must use string object keys")
                _validate_json_shape(child, f"{path}.{key}", active=active)
        else:
            for index, child in enumerate(value):
                _validate_json_shape(child, f"{path}[{index}]", active=active)
    finally:
        active.remove(marker)


class EventEmitter:
    """Assign per-run sequence numbers and deliver events to a sink."""

    def __init__(self, run_id: str, sink: EventSink, *, sequence: int = 0) -> None:
        if not isinstance(run_id, str) or not run_id.strip():
            raise ValueError("run_id must be a non-empty string")
        if not callable(sink):
            raise TypeError("event sink must be callable")
        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 0:
            raise ValueError("sequence must be a non-negative int")
        self.run_id = run_id.strip()
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
        if data is not None and not isinstance(data, Mapping):
            raise TypeError("AgentEvent.data must be a mapping or None")
        next_sequence = self._sequence + 1
        event = AgentEvent(
            type=event_type,
            run_id=self.run_id,
            sequence=next_sequence,
            iteration=iteration,
            data=dict(data or {}),
        )
        # Advance only after validation succeeds.  A malformed event must not
        # consume a cursor value and create an artificial gap in the durable
        # stream on the caller's next retry.
        self._sequence = next_sequence
        delivered = self.sink(event)
        if inspect.isawaitable(delivered):
            await delivered
        return event
