"""Offline 0.40 beta tour: operator projection, fault drill, and benchmark.

This example does not bind a socket.  It shows the safe projection API first;
an application can call ``operator.serve_forever(host='127.0.0.1', port=8787)``
after choosing its own local bearer token.
"""
from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

from lipas import (
    AgentCoordinator,
    FaultCampaign,
    FaultPlan,
    LocalWebOperator,
    benchmark_execution_store,
    run_fault_matrix,
)


async def main() -> None:
    with tempfile.TemporaryDirectory(prefix="lipas-operator-example-") as raw:
        root = Path(raw)
        with AgentCoordinator.open(root / "coordination.db", workspace=root) as coordinator:
            async def worker(payload: str) -> dict[str, str]:
                return {"echo": payload}

            coordinator.add("worker", worker)
            await coordinator.handoff(
                "worker",
                "operator preview",
                coordination_id="operator-example",
                handoff_id="operator-example-1",
            )
            operator = LocalWebOperator(
                coordinator.execution,
                coordinator=coordinator,
                operator_token="local-demo-token",
            )
            print("snapshot:", operator.snapshot())

            async def guarded_operation(injector):
                injector.hit("after-commit")
                return "safe to continue"

            drill = await FaultCampaign(
                FaultPlan({"never-hit": 1}),
            ).run(guarded_operation)
            print("fault drill completed:", drill.completed)

            async def matrix_operation(injector):
                injector.hit("process-kill")
                injector.hit("cancellation-race")
                return "safe to recover"

            matrix = await run_fault_matrix(
                matrix_operation,
                FaultPlan({"process-kill": 1, "cancellation-race": 1}),
            )
            print("fault matrix:", matrix.points, matrix.all_injected)
            print("browser page bytes:", len(operator.render_ui()))

        benchmark = benchmark_execution_store(
            operations=8,
            workers=2,
            workspace=root,
        )
        print("benchmark:", benchmark.as_dict())


if __name__ == "__main__":
    asyncio.run(main())
