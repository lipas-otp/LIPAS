"""0.47 observability, cost, SLO, and evaluation contracts."""
from __future__ import annotations

import math
from pathlib import Path

import pytest

from lipas import ExecutionStore
from lipas.performance import (
    CostEntry,
    ExecutionBenchmark,
    measure_execution,
    project_cost_ledger,
)


def test_observability_rejects_non_finite_cost_and_slo_values(tmp_path: Path):
    with pytest.raises(ValueError, match="CostEntry.usage"):
        CostEntry("run", {}, {"tokens": math.nan})
    with pytest.raises(ValueError, match="CostEntry.amount"):
        CostEntry("run", {}, {}, amount=math.inf)
    with pytest.raises(ValueError, match="target_p95"):
        with ExecutionStore(tmp_path / "metrics.db") as execution:
            measure_execution(execution, target_p95_duration_s=math.nan)


def test_cost_ledger_rejects_non_finite_run_usage_and_prices(tmp_path: Path):
    with ExecutionStore(tmp_path / "metrics.db") as execution:
        task = execution.create_task("cost", tmp_path)
        run = execution.create_run(task.id)
        lease = execution.claim_run(run.id)
        execution.complete_run(
            run.id,
            lease.lease_token or "",
            result={"usage": {"tokens": math.inf}},
        )
        with pytest.raises(ValueError, match="usage values"):
            project_cost_ledger(execution)
    # The price validation is exercised against a healthy store to avoid
    # conflating malformed provider usage with malformed configuration.
    with ExecutionStore(tmp_path / "healthy.db") as execution:
        with pytest.raises(ValueError, match="price_per_unit"):
            project_cost_ledger(execution, price_per_unit={"tokens": math.nan})


def test_benchmark_value_object_rejects_inconsistent_worker_counts():
    with pytest.raises(ValueError, match="workers"):
        ExecutionBenchmark(1, 0.1, (1.0,), workers=2)
