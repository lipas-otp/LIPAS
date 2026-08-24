"""Explicit policy contracts shared by coordination branches.

These values are host configuration, not another execution state machine. The
coordinator persists budget reservations in ``ExecutionStore`` so competing
workers cannot both pass the same pre-flight check.
"""
from __future__ import annotations

import math
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any
from types import MappingProxyType

__all__ = ["CapabilityPolicy", "SharedBudgetPolicy"]


BudgetEstimator = Callable[[Any, Any], Mapping[str, float]]


@dataclass(frozen=True, slots=True)
class SharedBudgetPolicy:
    """A durable reservation policy shared by coordination handoffs.

    ``limits`` are hard upper bounds. By default one ``handoffs`` unit is
    reserved per new envelope when that bucket is configured. Applications can
    supply ``estimator`` to reserve tokens, cost, or tool-specific resources
    from the immutable envelope and member contract.
    """

    limits: Mapping[str, float]
    scope: str = "default"
    estimator: BudgetEstimator | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.limits, Mapping) or not self.limits:
            raise ValueError("SharedBudgetPolicy.limits must be non-empty")
        normalized: dict[str, float] = {}
        for bucket, limit in self.limits.items():
            if (
                not isinstance(bucket, str)
                or not bucket.strip()
                or bucket != bucket.strip()
            ):
                raise ValueError("budget bucket names must be trimmed strings")
            if (
                isinstance(limit, bool)
                or not isinstance(limit, (int, float))
                or not math.isfinite(float(limit))
                or limit < 0
            ):
                raise ValueError(
                    f"budget {bucket!r} must be finite and non-negative",
                )
            normalized[bucket] = float(limit)
        if not isinstance(self.scope, str) or not self.scope.strip():
            raise ValueError("SharedBudgetPolicy.scope must be non-empty")
        if self.estimator is not None and not callable(self.estimator):
            raise TypeError("SharedBudgetPolicy.estimator must be callable or None")
        object.__setattr__(self, "limits", MappingProxyType(normalized))

    def estimate(self, envelope: Any, member: Any) -> dict[str, float]:
        """Return a validated reservation for one not-yet-admitted envelope."""
        raw = (
            {"handoffs": 1.0}
            if self.estimator is None and "handoffs" in self.limits
            else ({} if self.estimator is None else self.estimator(envelope, member))
        )
        if not isinstance(raw, Mapping):
            raise TypeError("budget estimator must return a mapping")
        estimate: dict[str, float] = {}
        for bucket, amount in raw.items():
            if bucket not in self.limits:
                raise ValueError(
                    f"budget estimator returned undeclared bucket {bucket!r}",
                )
            if (
                isinstance(amount, bool)
                or not isinstance(amount, (int, float))
                or not math.isfinite(float(amount))
                or amount < 0
            ):
                raise ValueError(
                    f"budget estimate for {bucket!r} must be finite and non-negative",
                )
            if amount:
                estimate[bucket] = float(amount)
        return estimate


@dataclass(frozen=True, slots=True)
class CapabilityPolicy:
    """Allowlist declared member capabilities before a handoff is claimed."""

    grants: Mapping[str, Iterable[str]]
    default: Iterable[str] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.grants, Mapping):
            raise TypeError("CapabilityPolicy.grants must be a mapping")
        normalized: dict[str, frozenset[str]] = {}
        for member, capabilities in self.grants.items():
            if (
                not isinstance(member, str)
                or not member.strip()
                or member != member.strip()
            ):
                raise ValueError(
                    "capability grant member names must be trimmed non-empty strings",
                )
            normalized[member] = _capability_set(capabilities)
        object.__setattr__(self, "grants", MappingProxyType(normalized))
        object.__setattr__(self, "default", _capability_set(self.default))

    def allowed_for(self, member: str) -> frozenset[str]:
        return frozenset(self.grants.get(member, self.grants.get("*", self.default)))

    def missing(self, member: str, required: Iterable[str]) -> frozenset[str]:
        return frozenset(required) - self.allowed_for(member)


def _capability_set(values: Iterable[str]) -> frozenset[str]:
    if isinstance(values, (str, bytes)):
        raise TypeError("capabilities must be an iterable of strings, not a string")
    try:
        raw = tuple(values)
    except TypeError as exc:
        raise TypeError("capabilities must be an iterable of strings") from exc
    if any(
        not isinstance(value, str)
        or not value.strip()
        or value != value.strip()
        for value in raw
    ):
        raise ValueError(
            "capabilities must contain trimmed non-empty strings",
        )
    return frozenset(raw)
