"""Run-scoped identity, cooperative cancellation, and absolute deadlines."""
from __future__ import annotations

import asyncio
import contextvars
import math
import threading
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Iterator, TypeVar

__all__ = [
    "CancellationToken",
    "RunCancelled",
    "RunContext",
    "RunDeadlineExceeded",
    "current_run_context",
]

T = TypeVar("T")


def _finite_number(value: Any, name: str, *, positive: bool = False) -> float:
    """Validate deadline values without leaking ``float`` overflow errors."""
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


class RunCancelled(asyncio.CancelledError):
    """Cooperative cancellation was requested for a logical run."""


class RunDeadlineExceeded(TimeoutError):
    """The run's absolute monotonic deadline expired."""


class CancellationToken:
    """Thread-safe cooperative token shared by a run and its handle."""

    def __init__(self) -> None:
        self._event = threading.Event()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def cancel(self) -> bool:
        if self._event.is_set():
            return False
        self._event.set()
        return True


@dataclass(slots=True)
class RunContext:
    """Provider-neutral control context for one invocation.

    ``deadline`` is an absolute value from ``time.monotonic()``. It therefore
    spans every ReAct phase instead of restarting at each model or tool call.
    """

    run_id: str = field(default_factory=lambda: f"run_{uuid.uuid4().hex}")
    deadline: float | None = None
    cancellation: CancellationToken = field(default_factory=CancellationToken)
    metadata: Mapping[str, Any] = field(default_factory=dict)
    cancel_check: Callable[[], bool] | None = field(
        default=None, repr=False, compare=False,
    )

    def __post_init__(self) -> None:
        if not isinstance(self.run_id, str) or not self.run_id.strip():
            raise ValueError("RunContext.run_id must be a non-empty string")
        if self.deadline is not None:
            self.deadline = _finite_number(self.deadline, "RunContext.deadline")
        if not isinstance(self.cancellation, CancellationToken):
            raise TypeError("RunContext.cancellation must be a CancellationToken")
        if not isinstance(self.metadata, Mapping):
            raise TypeError("RunContext.metadata must be a mapping")
        if self.cancel_check is not None and not callable(self.cancel_check):
            raise TypeError("RunContext.cancel_check must be callable or None")

    @classmethod
    def create(
        cls,
        *,
        run_id: str | None = None,
        timeout_s: float | None = None,
        deadline: float | None = None,
        cancellation: CancellationToken | None = None,
        metadata: Mapping[str, Any] | None = None,
        cancel_check: Callable[[], bool] | None = None,
    ) -> "RunContext":
        if timeout_s is not None and deadline is not None:
            raise ValueError("pass either timeout_s or deadline, not both")
        if timeout_s is not None:
            timeout_value = _finite_number(timeout_s, "timeout_s", positive=True)
            deadline = time.monotonic() + timeout_value
        return cls(
            run_id=run_id or f"run_{uuid.uuid4().hex}",
            deadline=deadline,
            cancellation=cancellation or CancellationToken(),
            metadata=dict(metadata or {}),
            cancel_check=cancel_check,
        )

    @property
    def cancelled(self) -> bool:
        return self.cancellation.cancelled or bool(
            self.cancel_check is not None and self.cancel_check()
        )

    @property
    def expired(self) -> bool:
        return self.deadline is not None and time.monotonic() >= self.deadline

    def remaining(self) -> float | None:
        if self.deadline is None:
            return None
        return max(0.0, self.deadline - time.monotonic())

    def cancel(self) -> bool:
        return self.cancellation.cancel()

    def check(self) -> None:
        if self.cancelled:
            raise RunCancelled(f"run {self.run_id!r} was cancelled")
        if self.expired:
            raise RunDeadlineExceeded(
                f"run {self.run_id!r} exceeded its absolute deadline",
            )

    async def wait(self, awaitable: Awaitable[T]) -> T:
        """Await one boundary while observing the same run-wide controls."""
        task = asyncio.ensure_future(awaitable)
        try:
            self.check()
            while True:
                remaining = self.remaining()
                interval = 0.05 if remaining is None else min(0.05, remaining)
                if interval <= 0:
                    raise RunDeadlineExceeded(
                        f"run {self.run_id!r} exceeded its absolute deadline",
                    )
                done, _ = await asyncio.wait({task}, timeout=interval)
                if task in done:
                    return await task
                self.check()
        except (RunCancelled, RunDeadlineExceeded, asyncio.CancelledError):
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            raise


_CURRENT: contextvars.ContextVar[RunContext | None] = contextvars.ContextVar(
    "lipas_run_context", default=None,
)


def current_run_context(*, required: bool = False) -> RunContext | None:
    """Return the scoped context visible inside async and sync tool code."""
    value = _CURRENT.get()
    if required and value is None:
        raise RuntimeError("no RunContext is active")
    return value


@contextmanager
def bind_run_context(context: RunContext) -> Iterator[None]:
    token = _CURRENT.set(context)
    try:
        yield
    finally:
        _CURRENT.reset(token)
