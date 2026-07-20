"""Persistent local Task dispatcher for the first-party workbench product."""
from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .execution import (
    ExecutionLeaseError,
    ExecutionStore,
    Run,
    Task,
)

__all__ = ["DispatchOutcome", "TaskDispatcher", "TaskExecutor"]


TaskExecutor = Callable[[Task, Run], Awaitable[None]]
OutcomeSink = Callable[["DispatchOutcome"], None]


@dataclass(frozen=True, slots=True)
class DispatchOutcome:
    task_id: str
    run_id: str
    status: str
    attempt: int
    error_type: str | None = None


@dataclass
class TaskDispatcher:
    """Discover claimable Runs and execute them with bounded concurrency.

    The dispatcher owns no second queue. ``ExecutionStore`` remains the source
    of truth: pending Runs and expired running leases are discoverable, while
    ``claim_run`` inside the executor is the atomic ownership boundary.
    Waiting approvals consume no dispatcher slot after the executor returns.
    """

    execution_path: str | Path
    executor: TaskExecutor
    max_concurrency: int = 2
    lease_seconds: float = 60.0
    poll_interval_s: float = 1.0
    retry_delay_s: float = 5.0
    outcome_sink: OutcomeSink | None = None
    _active: dict[str, asyncio.Task[DispatchOutcome]] = field(
        default_factory=dict, init=False, repr=False,
    )
    _retry_after: dict[str, float] = field(
        default_factory=dict, init=False, repr=False,
    )

    def __post_init__(self) -> None:
        if (
            isinstance(self.max_concurrency, bool)
            or not isinstance(self.max_concurrency, int)
            or self.max_concurrency < 1
        ):
            raise ValueError("max_concurrency must be a positive integer")
        self.poll_interval_s = self._positive_seconds(
            self.poll_interval_s, "poll_interval_s",
        )
        self.lease_seconds = self._positive_seconds(
            self.lease_seconds, "lease_seconds",
        )
        self.retry_delay_s = self._positive_seconds(
            self.retry_delay_s, "retry_delay_s",
        )
        self.execution_path = Path(self.execution_path).expanduser().resolve()

    async def run_until_idle(self) -> tuple[DispatchOutcome, ...]:
        """Run every candidate visible in this invocation, then return.

        A worker/setup error is reported once rather than creating a tight
        retry loop. A later invocation can retry that still-pending Run.
        """
        outcomes: list[DispatchOutcome] = []
        attempted: set[str] = set()
        while True:
            candidates = [
                run for run in self._claimable_runs()
                if run.id not in attempted
            ]
            if not candidates:
                return tuple(outcomes)
            batch = candidates[:self.max_concurrency]
            attempted.update(run.id for run in batch)
            completed = await asyncio.gather(*(
                self._execute(run) for run in batch
            ))
            outcomes.extend(completed)
            for outcome in completed:
                self._emit(outcome)

    async def serve(self, stop: asyncio.Event | None = None) -> None:
        """Continuously dispatch work until cancelled or ``stop`` is set."""
        stop = stop or asyncio.Event()
        try:
            while not stop.is_set():
                self._reap_finished()
                self._fill_slots()
                await self._wait_for_progress(stop)
        finally:
            for task in self._active.values():
                task.cancel()
            if self._active:
                await asyncio.gather(
                    *self._active.values(), return_exceptions=True,
                )
            self._active.clear()

    def _claimable_runs(self) -> tuple[Run, ...]:
        with ExecutionStore(self.execution_path) as store:
            return store.list_claimable_runs()

    def _fill_slots(self) -> None:
        slots = self.max_concurrency - len(self._active)
        if slots <= 0:
            return
        now = time.monotonic()
        candidates = (
            run for run in self._claimable_runs()
            if run.id not in self._active
            and self._retry_after.get(run.id, 0.0) <= now
        )
        for run in candidates:
            self._active[run.id] = asyncio.create_task(self._execute(run))
            slots -= 1
            if slots == 0:
                break

    def _reap_finished(self) -> None:
        for run_id, task in tuple(self._active.items()):
            if not task.done():
                continue
            del self._active[run_id]
            outcome = task.result()
            if outcome.status == "worker_error":
                self._retry_after[run_id] = (
                    time.monotonic() + self.retry_delay_s
                )
            else:
                self._retry_after.pop(run_id, None)
            self._emit(outcome)

    async def _wait_for_progress(self, stop: asyncio.Event) -> None:
        stopper = asyncio.create_task(stop.wait())
        waiters: set[asyncio.Task[Any]] = {stopper}
        waiters.update(self._active.values())
        try:
            await asyncio.wait(
                waiters,
                timeout=self.poll_interval_s,
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            if not stopper.done():
                stopper.cancel()
            await asyncio.gather(stopper, return_exceptions=True)

    async def _execute(self, discovered: Run) -> DispatchOutcome:
        try:
            with ExecutionStore(self.execution_path) as store:
                task = store.get_task(discovered.task_id)
                claimed = store.claim_run(
                    discovered.id, lease_seconds=self.lease_seconds,
                )
        except ExecutionLeaseError:
            # Another dispatcher won the conditional claim. This is expected
            # under multi-worker discovery and is not a task failure.
            return DispatchOutcome(
                discovered.task_id, discovered.id, "claimed_elsewhere",
                discovered.attempt,
            )
        if task is None:
            return DispatchOutcome(
                discovered.task_id, discovered.id, "worker_error",
                discovered.attempt, "MissingTask",
            )
        try:
            await self.executor(task, claimed)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return DispatchOutcome(
                task.id, discovered.id, "worker_error", claimed.attempt,
                type(exc).__name__,
            )

        with ExecutionStore(self.execution_path) as store:
            current = store.get_run(discovered.id)
        if current is None:
            return DispatchOutcome(
                task.id, discovered.id, "worker_error", discovered.attempt,
                "MissingRun",
            )
        return DispatchOutcome(
            task.id, current.id, current.state.value, current.attempt,
            (
                str(current.error.get("type"))
                if current.error is not None and current.error.get("type")
                else None
            ),
        )

    def _emit(self, outcome: DispatchOutcome) -> None:
        if self.outcome_sink is not None:
            self.outcome_sink(outcome)

    @staticmethod
    def _positive_seconds(value: float, name: str) -> float:
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or value <= 0
        ):
            raise ValueError(f"{name} must be a positive finite number")
        return float(value)
