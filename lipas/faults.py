"""Deterministic fault-injection helpers for recovery campaigns.

Fault campaigns are test/operator tooling, not an execution authority.  A
campaign asks application code to call ``injector.hit("point")`` at explicit
boundaries (after a durable commit, before a handoff, and so on).  The helper
then raises once at the configured occurrence and records a bounded outcome.
No retry, queue, or hidden state transition is performed here.
"""
from __future__ import annotations

import inspect
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Generic, TypeVar
from types import MappingProxyType

__all__ = [
    "FaultCampaign",
    "FaultCampaignResult",
    "FaultMatrixResult",
    "FaultInjected",
    "FaultInjector",
    "FaultPlan",
    "run_fault_matrix",
]


@dataclass(frozen=True, slots=True)
class FaultPlan:
    """Map a named boundary to the one-based hit count that should fail."""

    points: Mapping[str, int]

    def __post_init__(self) -> None:
        if not isinstance(self.points, Mapping) or not self.points:
            raise ValueError("FaultPlan.points must be a non-empty mapping")
        normalized: dict[str, int] = {}
        for point, occurrence in self.points.items():
            if (
                not isinstance(point, str)
                or not point.strip()
                or point != point.strip()
            ):
                raise ValueError("fault point names must be trimmed strings")
            if (
                isinstance(occurrence, bool)
                or not isinstance(occurrence, int)
                or occurrence < 1
            ):
                raise ValueError("fault occurrences must be positive integers")
            normalized[point] = occurrence
        # A campaign plan is part of the reproducibility contract.  Do not
        # let a caller mutate the mapping after validation and silently run a
        # different fault matrix.
        object.__setattr__(self, "points", MappingProxyType(normalized))


class FaultInjected(RuntimeError):
    """Raised exactly at the configured fault boundary."""

    def __init__(self, point: str, occurrence: int) -> None:
        self.point = point
        self.occurrence = occurrence
        super().__init__(f"fault injected at {point!r} occurrence {occurrence}")


class FaultInjector:
    """Small deterministic counter shared by one recovery campaign."""

    def __init__(self, plan: FaultPlan | Mapping[str, int]) -> None:
        self.plan = plan if isinstance(plan, FaultPlan) else FaultPlan(plan)
        self._counts: dict[str, int] = {}

    def hit(self, point: str) -> None:
        """Record a boundary hit and raise when the plan selects it."""
        if not isinstance(point, str) or not point.strip() or point != point.strip():
            raise ValueError("fault point must be a trimmed non-empty string")
        count = self._counts.get(point, 0) + 1
        self._counts[point] = count
        trigger = self.plan.points.get(point)
        if trigger == count:
            raise FaultInjected(point, count)

    def counts(self) -> Mapping[str, int]:
        return dict(self._counts)


T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class FaultCampaignResult(Generic[T]):
    """Recorded result of one bounded operation under fault injection."""

    value: T | None
    error: BaseException | None
    elapsed_s: float
    counts: Mapping[str, int]

    @property
    def completed(self) -> bool:
        return self.error is None

    @property
    def injected(self) -> bool:
        return isinstance(self.error, FaultInjected)


@dataclass(frozen=True, slots=True)
class FaultMatrixResult(Generic[T]):
    """Results for one bounded run at each named recovery boundary."""

    results: Mapping[str, FaultCampaignResult[T]]

    def __post_init__(self) -> None:
        object.__setattr__(self, "results", MappingProxyType(dict(self.results)))

    @property
    def points(self) -> tuple[str, ...]:
        return tuple(self.results)

    @property
    def all_injected(self) -> bool:
        return bool(self.results) and all(
            result.injected for result in self.results.values()
        )

    @property
    def completed_points(self) -> tuple[str, ...]:
        return tuple(
            point for point, result in self.results.items() if result.completed
        )


Operation = Callable[[FaultInjector], T | Awaitable[T]]


@dataclass(slots=True)
class FaultCampaign(Generic[T]):
    """Run one operation with a deterministic :class:`FaultInjector`."""

    plan: FaultPlan | Mapping[str, int]
    injector: FaultInjector = field(init=False)

    def __post_init__(self) -> None:
        self.injector = FaultInjector(self.plan)

    async def run(self, operation: Operation[T]) -> FaultCampaignResult[T]:
        if not callable(operation):
            raise TypeError("operation must be callable")
        # One campaign object may be reused for a matrix of clean runs.  Each
        # invocation gets a fresh counter so occurrence N means N for this
        # operation, never N plus hits from a previous invocation.
        self.injector = FaultInjector(self.plan)
        started = time.perf_counter()
        try:
            value = operation(self.injector)
            if inspect.isawaitable(value):
                value = await value
            return FaultCampaignResult(
                value,
                None,
                time.perf_counter() - started,
                self.injector.counts(),
            )
        except Exception as exc:
            return FaultCampaignResult(
                None,
                exc,
                time.perf_counter() - started,
                self.injector.counts(),
            )


async def run_fault_matrix(
    operation: Operation[T],
    plan: FaultPlan | Mapping[str, int],
) -> FaultMatrixResult[T]:
    """Run a deterministic operation once for every configured fault point.

    Each point gets an isolated :class:`FaultCampaign`, so a process-kill,
    SQLite-busy, cancellation-race, redelivery, or uncertain-member fixture
    cannot consume the occurrence counter of the next fixture.  The helper
    records outcomes; it does not retry or interpret recovery for the caller.
    """
    if not callable(operation):
        raise TypeError("operation must be callable")
    normalized = plan if isinstance(plan, FaultPlan) else FaultPlan(plan)
    results: dict[str, FaultCampaignResult[T]] = {}
    for point, occurrence in normalized.points.items():
        campaign: FaultCampaign[T] = FaultCampaign(
            FaultPlan({point: occurrence}),
        )
        results[point] = await campaign.run(operation)
    return FaultMatrixResult(results)
