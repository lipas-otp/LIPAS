"""0.40 operator, fault-campaign, and local performance beta contracts."""
from __future__ import annotations

import asyncio
import http.client
import json
import queue
import socket
import threading
from pathlib import Path

import pytest

from lipas import (
    AgentCoordinator,
    CoordinationIdentityConflict,
    ExecutionStore,
    FaultCampaign,
    FaultInjected,
    FaultMatrixResult,
    FaultPlan,
    LocalWebOperator,
    OperationJournal,
    SharedBudgetPolicy,
    benchmark_execution_store,
    run_fault_matrix,
)


def test_local_operator_is_a_projection_and_requires_token_for_mutations(
    tmp_path: Path,
):
    with ExecutionStore(tmp_path / "execution.db") as execution:
        task = execution.create_task("operator task", tmp_path)
        run = execution.create_run(task.id)
        operator = LocalWebOperator(execution, operator_token="secret")
        snapshot = operator.snapshot()
        assert snapshot["version"]
        assert snapshot["runs"][0]["id"] == run.id
        assert "lease_token" not in snapshot["runs"][0]
        assert operator._authorized("Bearer secret")
        assert not operator._authorized("Bearer wrong")
        operator._post(
            ("api", "runs", run.id, "cancel"),
            {},
        )
        cancelled = execution.get_run(run.id)
        assert cancelled is not None and cancelled.cancel_requested
        operator.close()


def test_local_operator_requires_reconciliation_before_reopening_uncertain_run(
    tmp_path: Path,
):
    with ExecutionStore(tmp_path / "reopen.db") as execution:
        task = execution.create_task("uncertain", tmp_path)
        run = execution.create_run(task.id)
        claimed = execution.claim_run(run.id)
        assert claimed.lease_token is not None
        execution.fail_run(
            run.id,
            claimed.lease_token,
            error={"type": "DurablePhaseTimeout", "recovery_required": True},
        )
        operator = LocalWebOperator(execution)
        with pytest.raises(ValueError, match="reconciled=true"):
            operator._post(
                ("api", "runs", run.id, "reopen"),
                {"acknowledge_uncertain": True},
            )
        reopened = operator._post(
            ("api", "runs", run.id, "reopen"),
            {
                "acknowledge_uncertain": True,
                "reconciled": True,
                "evidence": {
                    "source": "test",
                    "observation": "provider/effect outcome was checked",
                },
            },
        )
        assert reopened["run"]["state"] == "pending"
        operator.close()


def test_local_operator_reconciliation_requires_and_audits_observation(tmp_path: Path):
    with ExecutionStore(tmp_path / "execution.db") as execution:
        journal = OperationJournal(tmp_path / "operations.db")
        journal.prepare(
            key="op-1",
            kind="http_request",
            request={"provider_request_id": "provider-op-1"},
            provider_request_id="provider-op-1",
        )
        journal.mark_uncertain("op-1", error={"type": "timeout"})
        operator = LocalWebOperator(execution, operations=journal)
        with pytest.raises(ValueError, match="observation"):
            operator._post(
                ("api", "operations", "op-1", "reconcile"),
                {"found": True, "provider_reference": "ref-1"},
            )
        result = operator._post(
            ("api", "operations", "op-1", "reconcile"),
            {
                "found": True,
                "provider_reference": "ref-1",
                "observation": "provider lookup returned ref-1",
                "result": {"accepted": True},
            },
        )
        assert result["operation"]["state"] == "succeeded"
        row = journal._conn.execute(
            "SELECT fields_json FROM operation_audit_events "
            "WHERE tag='operation_succeeded'"
        ).fetchone()
        assert row is not None and "provider lookup returned ref-1" in row[0]
        journal.close()


def test_local_operator_exposes_reconnectable_run_pages_and_evidence(
    tmp_path: Path,
):
    from lipas import AgentEventType

    with ExecutionStore(tmp_path / "events.db") as execution:
        task = execution.create_task("events", tmp_path)
        run = execution.create_run(task.id)
        for index in range(3):
            execution.append_agent_event(
                run.id,
                AgentEventType.TOOL_STARTED,
                identity=f"tool:{index}",
                data={"index": index},
            )
        operator = LocalWebOperator(execution, max_items=10)
        first = operator._get(("api", "runs", run.id, "events"), {
            "limit": ["2"],
        })
        assert len(first["events"]) == 2
        assert first["has_more"] is True
        second = operator._get(("api", "runs", run.id, "events"), {
            "after": [str(first["next_cursor"])],
            "limit": ["2"],
        })
        assert [event["sequence"] for event in second["events"]] == [3]
        evidence = operator.snapshot()["evidence"]
        assert evidence["event_count"] == 3
        assert evidence["event_types"][AgentEventType.TOOL_STARTED] == 3
        assert "<title>LIPAS Local Operator</title>" in operator.render_ui()


def test_local_operator_projects_task_product_evidence_and_task_cancel(
    tmp_path: Path,
):
    from lipas import Workbench

    with Workbench(tmp_path / "home", sandbox="local") as workbench:
        task, run = workbench.create_task("inspect evidence", tmp_path)
        workbench.add_artifact(
            task_id=task.id,
            run_id=run.id,
            kind="note",
            path="notes.txt",
            metadata={"purpose": "operator"},
        )
        operator = LocalWebOperator(
            workbench.execution,
            workbench=workbench,
            operator_token="secret",
        )
        detail = operator._get(("api", "tasks", task.id), {})
        assert detail["task"]["id"] == task.id
        assert detail["workbench"]["artifacts"][0]["kind"] == "note"
        cancelled = operator._post(("api", "tasks", task.id, "cancel"), {})
        assert cancelled["task"]["state"] == "cancelled"
        operator.close()


def test_local_operator_http_boundary_redacts_and_authorizes_mutations(
    tmp_path: Path,
):
    ready: queue.Queue[tuple[int, str] | tuple[str, str]] = queue.Queue()
    stop = threading.Event()

    def serve() -> None:
        try:
            from lipas import OperatorServer

            with ExecutionStore(tmp_path / "http.db") as execution:
                task = execution.create_task("http task", tmp_path)
                operator = LocalWebOperator(execution, operator_token="secret")
                server = operator.make_server(port=0)
                assert isinstance(server, OperatorServer)
                ready.put((str(server.server_address[1]), task.id))
                while not stop.is_set():
                    server.handle_request()
                operator.close()
        except BaseException as exc:
            ready.put(("error", repr(exc)))

    thread = threading.Thread(target=serve)
    thread.start()
    raw_port, task_id = ready.get(timeout=5)
    if raw_port == "error":
        if "PermissionError" in task_id:
            pytest.skip("sandbox disallows loopback socket binding")
        raise AssertionError(task_id)
    port = int(raw_port)

    def request(method: str, path: str, *, auth: str | None = None) -> tuple[int, dict]:
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        headers = {"Content-Type": "application/json"}
        if auth is not None:
            headers["Authorization"] = auth
        body = b"{}" if method == "POST" else None
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        payload = json.loads(response.read().decode("utf-8"))
        connection.close()
        return response.status, payload

    assert request("GET", "/api/tasks")[0] == 200
    assert request("POST", f"/api/tasks/{task_id}/cancel")[0] == 401
    status, payload = request(
        "POST", f"/api/tasks/{task_id}/cancel", auth="Bearer secret",
    )
    assert status == 200
    assert payload["task"]["state"] == "cancelled"
    stop.set()
    # Wake the blocking handle_request without racing its final response: the
    # serving loop observes ``stop`` after this request and closes cleanly.
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=2) as wake:
            wake.sendall(b"GET /health HTTP/1.0\r\nHost: localhost\r\n\r\n")
    except ConnectionRefusedError:
        # The authenticated request may have been the final handle_request
        # iteration already; shutdown is then complete before the wake-up
        # connection is attempted.
        pass
    thread.join(timeout=2)
    assert not thread.is_alive()


def test_fault_campaign_is_deterministic_and_does_not_swallow_cancellation():
    async def run() -> None:
        campaign = FaultCampaign(FaultPlan({"after-commit": 2}))

        async def operation(injector):
            injector.hit("after-commit")
            injector.hit("after-commit")
            return "unreachable"

        result = await campaign.run(operation)
        assert isinstance(result.error, FaultInjected)
        assert result.injected
        assert result.counts == {"after-commit": 2}

    asyncio.run(run())


def test_fault_campaign_restarts_occurrence_counters_and_freezes_plan():
    async def run() -> None:
        campaign = FaultCampaign(FaultPlan({"commit": 1}))
        first = await campaign.run(lambda injector: injector.hit("commit"))
        second = await campaign.run(lambda injector: injector.hit("commit"))
        assert first.injected and second.injected
        with pytest.raises(TypeError):
            campaign.plan.points["other"] = 1  # type: ignore[index]

    asyncio.run(run())


def test_fault_matrix_isolated_and_recovery_points_are_observable():
    async def run() -> None:
        async def operation(injector):
            injector.hit("process-kill")
            injector.hit("sqlite-busy")
            return "ok"

        matrix = await run_fault_matrix(
            operation,
            FaultPlan({"process-kill": 1, "sqlite-busy": 1}),
        )
        assert isinstance(matrix, FaultMatrixResult)
        assert matrix.points == ("process-kill", "sqlite-busy")
        assert matrix.all_injected
        assert matrix.completed_points == ()

    asyncio.run(run())


def test_execution_benchmark_reports_local_transition_percentiles():
    result = benchmark_execution_store(operations=5)
    assert result.operations == 5
    assert len(result.samples_ms) == 5
    assert result.elapsed_s >= 0
    assert result.p95_ms >= result.p50_ms >= 0
    assert result.as_dict()["throughput_per_s"] > 0


def test_execution_benchmark_can_measure_shared_sqlite_contention(tmp_path: Path):
    result = benchmark_execution_store(
        tmp_path / "contention.db",
        operations=8,
        workers=2,
        workspace=tmp_path,
    )
    assert result.workers == 2
    assert result.operations == 8
    assert len(result.samples_ms) == 8
    assert result.as_dict()["workers"] == 2


def test_shared_budget_is_atomic_across_coordinator_connections(tmp_path: Path):
    async def scenario() -> None:
        async def member(payload):
            return payload

        database = tmp_path / "shared-budget.db"
        first = AgentCoordinator.open(
            database,
            budget_policy=SharedBudgetPolicy({"handoffs": 1}, scope="race"),
        )
        second = AgentCoordinator.open(
            database,
            budget_policy=SharedBudgetPolicy({"handoffs": 1}, scope="race"),
        )
        try:
            first.add("worker", member)
            second.add("worker", member)
            results = await asyncio.gather(
                first.handoff("worker", "a", coordination_id="race", handoff_id="a"),
                second.handoff("worker", "b", coordination_id="race", handoff_id="b"),
                return_exceptions=True,
            )
            assert sum(not isinstance(value, Exception) for value in results) == 1
            assert sum(isinstance(value, Exception) for value in results) == 1
            snapshot = first.budget_snapshot()
            assert snapshot is not None
            assert snapshot["spent"] == {"handoffs": 1.0}
        finally:
            first.close()
            second.close()

    asyncio.run(scenario())


def test_identity_conflict_is_rejected_before_consuming_shared_budget(
    tmp_path: Path,
):
    async def scenario() -> None:
        async def member(payload):
            return payload

        with AgentCoordinator.open(
            tmp_path / "identity-budget.db",
            budget_policy=SharedBudgetPolicy({"handoffs": 2}, scope="identity"),
        ) as coordinator:
            coordinator.add("worker", member)
            await coordinator.handoff(
                "worker",
                {"version": 1},
                coordination_id="identity-budget",
                handoff_id="stable",
            )
            with pytest.raises(CoordinationIdentityConflict):
                await coordinator.handoff(
                    "worker",
                    {"version": 2},
                    coordination_id="identity-budget",
                    handoff_id="stable",
                )
            snapshot = coordinator.budget_snapshot()
            assert snapshot is not None
            assert snapshot["spent"] == {"handoffs": 1.0}

    asyncio.run(scenario())
