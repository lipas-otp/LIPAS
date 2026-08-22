"""Behaviour-neutral, read-only observation contracts."""
from __future__ import annotations

import inspect
from collections.abc import Awaitable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from .behaviour import AgentState
from .context import RunContext

__all__ = ["Recommendation", "RunObserver", "RunSnapshot", "observe_run"]


@dataclass(frozen=True, slots=True)
class RunSnapshot:
    """Immutable public snapshot that does not grant mutation authority."""

    state: AgentState
    phase: str
    reply: Mapping[str, Any] | None = None
    tool_calls: tuple[Mapping[str, Any], ...] = ()
    tool_results: tuple[Mapping[str, Any], ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.state, AgentState):
            raise TypeError("RunSnapshot.state must be AgentState")
        if not isinstance(self.phase, str) or not self.phase.strip():
            raise ValueError("RunSnapshot.phase must be a non-empty string")
        if self.reply is not None and not isinstance(self.reply, Mapping):
            raise TypeError("RunSnapshot.reply must be a mapping or None")
        if not all(isinstance(item, Mapping) for item in self.tool_calls):
            raise TypeError("RunSnapshot.tool_calls must contain mappings")
        if not all(isinstance(item, Mapping) for item in self.tool_results):
            raise TypeError("RunSnapshot.tool_results must contain mappings")
        if not isinstance(self.metadata, Mapping):
            raise TypeError("RunSnapshot.metadata must be a mapping")


@dataclass(frozen=True, slots=True)
class Recommendation:
    """Advisory observer output; it carries no execution permission."""

    kind: str = "advisory"
    reason: str = ""
    payload: Mapping[str, Any] = field(default_factory=dict)
    identity: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, str) or not self.kind.strip():
            raise ValueError("Recommendation.kind must be a non-empty string")
        if not isinstance(self.reason, str):
            raise TypeError("Recommendation.reason must be str")
        if not isinstance(self.payload, Mapping):
            raise TypeError("Recommendation.payload must be a mapping")
        if self.identity is not None and (
            not isinstance(self.identity, str) or not self.identity.strip()
        ):
            raise ValueError("Recommendation.identity must be non-empty or None")

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "reason": self.reason,
            "payload": dict(self.payload),
            "identity": self.identity,
        }


@runtime_checkable
class RunObserver(Protocol):
    async def observe(
        self,
        snapshot: RunSnapshot,
        context: RunContext,
    ) -> Recommendation | None: ...


async def observe_run(
    observer: RunObserver,
    snapshot: RunSnapshot,
    context: RunContext,
) -> Recommendation | None:
    """Call an observer while tolerating a synchronous implementation."""
    if not hasattr(observer, "observe"):
        raise TypeError("observer must define observe(snapshot, context)")
    value: Recommendation | None | Awaitable[Recommendation | None] = (
        observer.observe(snapshot, context)
    )
    if inspect.isawaitable(value):
        value = await value
    if value is not None and not isinstance(value, Recommendation):
        raise TypeError("RunObserver.observe must return Recommendation or None")
    return value
