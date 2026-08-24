"""Small SQLite performance probes for the 0.40 local-runtime beta.

The benchmark measures only local durable Task/Run transitions.  It is not a
claim about distributed throughput, model latency, or a production SLA.  The
result is deliberately a value object so CI and operators can compare runs
without adding a metrics database.
"""
from __future__ import annotations

import tempfile
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from statistics import mean
from typing import Any

from .execution import ExecutionStore

__all__ = ["ExecutionBenchmark", "benchmark_execution_store"]


@dataclass(frozen=True, slots=True)
class ExecutionBenchmark:
    """Summary of one bounded local ExecutionStore benchmark."""

    operations: int
    elapsed_s: float
    samples_ms: tuple[float, ...]
    workers: int = 1

    @property
    def throughput_per_s(self) -> float:
        return self.operations / self.elapsed_s if self.elapsed_s else 0.0

    @property
    def mean_ms(self) -> float:
        return mean(self.samples_ms) if self.samples_ms else 0.0

    @property
    def p50_ms(self) -> float:
        return _percentile(self.samples_ms, 0.50)

    @property
    def p95_ms(self) -> float:
        return _percentile(self.samples_ms, 0.95)

    def as_dict(self) -> dict[str, Any]:
        return {
            "operations": self.operations,
            "workers": self.workers,
            "elapsed_s": self.elapsed_s,
            "throughput_per_s": self.throughput_per_s,
            "mean_ms": self.mean_ms,
            "p50_ms": self.p50_ms,
            "p95_ms": self.p95_ms,
        }


def benchmark_execution_store(
    path: str | Path = ":memory:",
    *,
    operations: int = 100,
    workspace: str | Path | None = None,
    workers: int = 1,
) -> ExecutionBenchmark:
    """Measure ``operations`` create/claim/complete transitions.

    A temporary workspace is used when none is supplied.  A file path is left
    in place for inspection; ``:memory:`` remains the convenient CI default
    for one worker.  With multiple workers, use a file path (or let this
    helper create a temporary one) so every connection observes one authority.
    Each worker owns its SQLite connection; this intentionally exercises the
    same bounded writer contention as independent local processes.
    """
    if isinstance(operations, bool) or not isinstance(operations, int) or operations < 1:
        raise ValueError("operations must be a positive int")
    if isinstance(workers, bool) or not isinstance(workers, int) or workers < 1:
        raise ValueError("workers must be a positive int")
    workers = min(workers, operations)
    temporary: tempfile.TemporaryDirectory[str] | None = None
    if workspace is None:
        temporary = tempfile.TemporaryDirectory(prefix="lipas-benchmark-")
        root = Path(temporary.name)
    else:
        root = Path(workspace).expanduser().resolve()
        root.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    run_tag = uuid.uuid4().hex
    database_path: str | Path = path
    database_temporary: tempfile.TemporaryDirectory[str] | None = None
    if workers > 1 and str(path) == ":memory:":
        database_temporary = tempfile.TemporaryDirectory(prefix="lipas-benchmark-db-")
        database_path = Path(database_temporary.name) / "execution.db"

    def worker(indices: tuple[int, ...]) -> list[float]:
        local_samples: list[float] = []
        with ExecutionStore(database_path) as execution:
            for index in indices:
                operation_started = time.perf_counter()
                task = execution.create_task(
                    f"benchmark-{index}",
                    root,
                    task_id=f"benchmark_{run_tag}_{index}",
                )
                run = execution.create_run(task.id)
                claimed = execution.claim_run(run.id)
                execution.complete_run(
                    run.id,
                    claimed.lease_token or "",
                    result={"index": index},
                )
                local_samples.append((time.perf_counter() - operation_started) * 1_000)
        return local_samples

    indices = tuple(tuple(range(worker_index, operations, workers)) for worker_index in range(workers))
    try:
        if workers == 1:
            samples = worker(indices[0])
        else:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                batches = pool.map(worker, indices)
                samples = [sample for batch in batches for sample in batch]
    finally:
        if temporary is not None:
            temporary.cleanup()
        if database_temporary is not None:
            database_temporary.cleanup()
    return ExecutionBenchmark(
        operations,
        time.perf_counter() - started,
        tuple(samples),
        workers,
    )


def _percentile(values: tuple[float, ...], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * fraction))))
    return ordered[index]
