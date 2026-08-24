"""Durable task/run/checkpoint semantics for the local execution system."""
from __future__ import annotations

import sqlite3

import pytest

import lipas
from lipas.execution import (
    CheckpointConflict,
    ExecutionLeaseError,
    ExecutionSchemaVersionMismatch,
    ExecutionStateError,
    ExecutionStore,
    InterruptState,
    RunState,
    TAG_EXECUTION_CHECKPOINT_SAVED,
    TAG_EXECUTION_CANCEL_REQUESTED,
    TAG_EXECUTION_INTERRUPT_REQUESTED,
    TAG_EXECUTION_INTERRUPT_RESOLVED,
    TAG_EXECUTION_LEASE_RENEWED,
    TAG_EXECUTION_RUN_CANCELLED,
    TAG_EXECUTION_RUN_CLAIMED,
    TAG_EXECUTION_RUN_COMPLETED,
    TAG_EXECUTION_RUN_CREATED,
    TAG_EXECUTION_RUN_FAILED,
    TAG_EXECUTION_TASK_CANCELLED,
    TAG_EXECUTION_TASK_COMPLETED,
    TAG_EXECUTION_TASK_CREATED,
    TaskState,
)
from lipas.rows import RowSet
from lipas.rows.history import HistoryRow
from lipas.serialization.codec import UnserializableClaim
from lipas.store import ClaimStore


def _active_run(store: ExecutionStore, workspace, *, suffix: str = "1"):
    task = store.create_task(
        f"update workspace {suffix}", workspace, task_id=f"task-{suffix}",
    )
    run = store.create_run(task.id, run_id=f"run-{suffix}")
    return task, store.claim_run(run.id, lease_seconds=60, now=100)


def test_task_run_and_checkpoint_survive_reopen(tmp_path):
    path = tmp_path / "runs" / "execution.db"
    with ExecutionStore(path) as store:
        task, run = _active_run(store, tmp_path)
        checkpoint = store.save_checkpoint(
            run.id,
            run.lease_token or "",
            expected_version=0,
            phase="after_iteration",
            state={"messages": [{"role": "user", "content": "fix it"}], "iteration": 1},
            now=101,
        )
        assert checkpoint.version == 1

    with ExecutionStore(path) as reopened:
        assert reopened.get_task(task.id) == task
        restored_run = reopened.get_run(run.id)
        assert restored_run is not None
        assert restored_run.state is RunState.RUNNING
        assert restored_run.checkpoint_version == 1
        restored = reopened.get_checkpoint(run.id)
        assert restored is not None
        assert restored.phase == "after_iteration"
        assert restored.state["iteration"] == 1


def test_expired_run_is_reclaimed_and_old_worker_is_fenced(tmp_path):
    with ExecutionStore(tmp_path / "execution.db") as store:
        _, first = _active_run(store, tmp_path)
        second = store.claim_run(first.id, lease_seconds=60, now=161)

        assert second.attempt == 2
        assert second.lease_token != first.lease_token
        with pytest.raises(ExecutionLeaseError):
            store.save_checkpoint(
                first.id,
                first.lease_token or "",
                expected_version=0,
                phase="stale",
                state={},
                now=162,
            )
        store.save_checkpoint(
            second.id,
            second.lease_token or "",
            expected_version=0,
            phase="recovered",
            state={"attempt": 2},
            now=162,
        )


def test_expired_run_is_fenced_across_store_connections(tmp_path):
    path = tmp_path / "execution.db"
    first_store = ExecutionStore(path)
    second_store = ExecutionStore(path)
    try:
        _, first = _active_run(first_store, tmp_path)
        with pytest.raises(ExecutionLeaseError):
            second_store.claim_run(first.id, now=120)

        replacement = second_store.claim_run(first.id, now=161)
        with pytest.raises(ExecutionLeaseError):
            first_store.save_checkpoint(
                first.id,
                first.lease_token or "",
                expected_version=0,
                phase="stale",
                state={},
                now=162,
            )
        second_store.save_checkpoint(
            replacement.id,
            replacement.lease_token or "",
            expected_version=0,
            phase="replacement",
            state={},
            now=162,
        )
    finally:
        first_store.close()
        second_store.close()


def test_agent_event_identity_cannot_be_reused_with_different_payload(tmp_path):
    with ExecutionStore(tmp_path / "execution.db") as store:
        task = store.create_task("event identity", tmp_path)
        run = store.create_run(task.id)
        first = store.append_agent_event(
            run.id,
            "tool_started",
            identity="tool:0:started",
            data={"tool": "read"},
        )
        assert store.append_agent_event(
            run.id,
            "tool_started",
            identity="tool:0:started",
            data={"tool": "read"},
        ) == first
        with pytest.raises(ExecutionStateError, match="different event"):
            store.append_agent_event(
                run.id,
                "tool_started",
                identity="tool:0:started",
                data={"tool": "write"},
            )


def test_late_heartbeat_can_renew_until_a_replacement_changes_the_token(tmp_path):
    with ExecutionStore(tmp_path / "execution.db") as store:
        _, first = _active_run(store, tmp_path)
        renewed = store.renew_lease(
            first.id, first.lease_token or "", lease_seconds=10, now=161,
        )
        assert renewed.lease_expires == 171

        replacement = store.claim_run(first.id, lease_seconds=10, now=172)
        assert replacement.lease_token != first.lease_token
        with pytest.raises(ExecutionLeaseError):
            store.renew_lease(
                first.id, first.lease_token or "", lease_seconds=10, now=173,
            )


def test_live_run_cannot_be_claimed_twice(tmp_path):
    with ExecutionStore(tmp_path / "execution.db") as store:
        _, run = _active_run(store, tmp_path)
        with pytest.raises(ExecutionLeaseError):
            store.claim_run(run.id, now=120)


def test_checkpoint_uses_optimistic_version(tmp_path):
    with ExecutionStore(tmp_path / "execution.db") as store:
        _, run = _active_run(store, tmp_path)
        token = run.lease_token or ""
        store.save_checkpoint(
            run.id, token, expected_version=0, phase="first", state={}, now=101,
        )
        with pytest.raises(CheckpointConflict, match="version 1, expected 0"):
            store.save_checkpoint(
                run.id, token, expected_version=0, phase="stale", state={}, now=102,
            )
        assert store.get_run(run.id).checkpoint_version == 1  # type: ignore[union-attr]


def test_suspend_checkpoints_and_releases_lease_atomically(tmp_path):
    path = tmp_path / "execution.db"
    with ExecutionStore(path) as store:
        _, run = _active_run(store, tmp_path)
        interrupt = store.suspend(
            run.id,
            run.lease_token or "",
            expected_version=0,
            phase="before_external_write",
            checkpoint_state={"pending_tool": "send_email"},
            kind="approval",
            request={"summary": "send one email", "risk": "external_write"},
            interrupt_id="approval-1",
            now=101,
        )
        waiting = store.get_run(run.id)
        assert waiting is not None
        assert waiting.state is RunState.WAITING
        assert waiting.lease_token is None
        assert waiting.checkpoint_version == 1
        assert interrupt.state is InterruptState.PENDING
        with pytest.raises(ExecutionLeaseError):
            store.claim_run(run.id, now=200)

    with ExecutionStore(path) as reopened:
        restored = reopened.get_checkpoint(run.id)
        assert restored is not None
        assert restored.state == {"pending_tool": "send_email"}
        allowed = reopened.resolve_interrupt(
            "approval-1", allow=True, response={"approved_by": "user"}, now=300,
        )
        assert allowed.state is InterruptState.ALLOWED
        resumed = reopened.claim_run(run.id, now=301)
        assert resumed.state is RunState.RUNNING
        assert resumed.attempt == 2
        assert resumed.checkpoint_version == 1


def test_interrupt_resolution_is_idempotent_but_cannot_be_reversed(tmp_path):
    with ExecutionStore(tmp_path / "execution.db") as store:
        _, run = _active_run(store, tmp_path)
        store.suspend(
            run.id,
            run.lease_token or "",
            expected_version=0,
            phase="approval",
            checkpoint_state={},
            kind="approval",
            request={"action": "write"},
            interrupt_id="approval-1",
            now=101,
        )
        first = store.resolve_interrupt(
            "approval-1", allow=True, response={"by": "user"}, now=102,
        )
        repeated = store.resolve_interrupt(
            "approval-1", allow=True, response={"by": "user"}, now=103,
        )
        assert repeated == first
        with pytest.raises(ExecutionStateError, match="already allowed"):
            store.resolve_interrupt("approval-1", allow=False, now=104)


def test_failed_suspend_rolls_back_checkpoint_and_run_state(tmp_path):
    with ExecutionStore(tmp_path / "execution.db") as store:
        _, first = _active_run(store, tmp_path, suffix="1")
        store.suspend(
            first.id,
            first.lease_token or "",
            expected_version=0,
            phase="approval",
            checkpoint_state={},
            kind="approval",
            request={"action": "first"},
            interrupt_id="same-id",
            now=101,
        )

        _, second = _active_run(store, tmp_path, suffix="2")
        with pytest.raises(ExecutionStateError, match="interrupt id"):
            store.suspend(
                second.id,
                second.lease_token or "",
                expected_version=0,
                phase="approval",
                checkpoint_state={"must": "roll back"},
                kind="approval",
                request={"action": "second"},
                interrupt_id="same-id",
                now=102,
            )
        unchanged = store.get_run(second.id)
        assert unchanged is not None
        assert unchanged.state is RunState.RUNNING
        assert unchanged.checkpoint_version == 0
        assert store.get_checkpoint(second.id) is None


def test_denied_interrupt_cancels_run(tmp_path):
    with ExecutionStore(tmp_path / "execution.db") as store:
        _, run = _active_run(store, tmp_path)
        store.suspend(
            run.id,
            run.lease_token or "",
            expected_version=0,
            phase="approval",
            checkpoint_state={},
            kind="approval",
            request={"action": "delete"},
            interrupt_id="approval-1",
            now=101,
        )
        denied = store.resolve_interrupt(
            "approval-1", allow=False, response={"reason": "too risky"}, now=102,
        )
        assert denied.state is InterruptState.DENIED
        assert store.get_run(run.id).state is RunState.CANCELLED  # type: ignore[union-attr]


def test_only_one_active_run_per_task(tmp_path):
    with ExecutionStore(tmp_path / "execution.db") as store:
        task = store.create_task("do work", tmp_path)
        store.create_run(task.id)
        with pytest.raises(ExecutionStateError, match="active run"):
            store.create_run(task.id)


def test_completion_is_lease_owned_and_completes_task(tmp_path):
    with ExecutionStore(tmp_path / "execution.db") as store:
        task, run = _active_run(store, tmp_path)
        with pytest.raises(ExecutionLeaseError):
            store.complete_run(run.id, "wrong-token", result={"ok": True}, now=101)
        completed = store.complete_run(
            run.id, run.lease_token or "", result={"ok": True}, now=101,
        )
        assert completed.state is RunState.COMPLETED
        assert completed.result == {"ok": True}
        assert completed.lease_token is None
        assert store.get_task(task.id).state is TaskState.COMPLETED  # type: ignore[union-attr]


def test_running_cancel_is_cooperative_and_prevents_completion(tmp_path):
    with ExecutionStore(tmp_path / "execution.db") as store:
        _, run = _active_run(store, tmp_path)
        requested = store.request_cancel(run.id, now=101)
        assert requested.state is RunState.RUNNING
        assert requested.cancel_requested is True
        with pytest.raises(ExecutionStateError, match="cancellation"):
            store.complete_run(
                run.id, run.lease_token or "", result={"ignored": True}, now=102,
            )
        cancelled = store.finish_cancelled(
            run.id, run.lease_token or "", now=102,
        )
        assert cancelled.state is RunState.CANCELLED


def test_expired_cancel_requested_run_can_be_reclaimed_to_finish_cancel(tmp_path):
    with ExecutionStore(tmp_path / "execution.db") as store:
        _, first = _active_run(store, tmp_path)
        store.request_cancel(first.id, now=101)

        replacement = store.claim_run(first.id, now=161)
        assert replacement.cancel_requested is True
        cancelled = store.finish_cancelled(
            replacement.id,
            replacement.lease_token or "",
            now=162,
        )
        assert cancelled.state is RunState.CANCELLED


def test_cancel_waiting_run_resolves_pending_interrupt(tmp_path):
    with ExecutionStore(tmp_path / "execution.db") as store:
        _, run = _active_run(store, tmp_path)
        store.suspend(
            run.id,
            run.lease_token or "",
            expected_version=0,
            phase="approval",
            checkpoint_state={},
            kind="approval",
            request={"action": "write"},
            interrupt_id="approval-1",
            now=101,
        )
        cancelled = store.request_cancel(run.id, now=102)
        interrupt = store.get_interrupt("approval-1")
        assert cancelled.state is RunState.CANCELLED
        assert interrupt is not None
        assert interrupt.state is InterruptState.DENIED
        assert interrupt.response == {"reason": "run_cancelled"}


def test_non_json_checkpoint_does_not_advance_run(tmp_path):
    with ExecutionStore(tmp_path / "execution.db") as store:
        _, run = _active_run(store, tmp_path)
        with pytest.raises(UnserializableClaim):
            store.save_checkpoint(
                run.id,
                run.lease_token or "",
                expected_version=0,
                phase="invalid",
                state={"not_json": {1, 2}},
                now=101,
            )
        assert store.get_run(run.id).checkpoint_version == 0  # type: ignore[union-attr]
        assert store.get_checkpoint(run.id) is None


@pytest.mark.parametrize(
    "lease_seconds",
    [True, 0, -1, float("nan"), float("inf"), float("-inf")],
)
def test_invalid_lease_duration_cannot_create_an_unrecoverable_run(
    tmp_path, lease_seconds,
):
    with ExecutionStore(tmp_path / "execution.db") as store:
        task = store.create_task("validate lease", tmp_path)
        pending = store.create_run(task.id)

        with pytest.raises(ValueError, match="finite positive"):
            store.claim_run(pending.id, lease_seconds=lease_seconds)

        unchanged = store.get_run(pending.id)
        assert unchanged is not None
        assert unchanged.state is RunState.PENDING
        assert unchanged.attempt == 0
        assert unchanged.lease_expires is None


@pytest.mark.parametrize("now", [True, float("nan"), float("inf")])
def test_invalid_clock_value_cannot_mutate_execution_state(tmp_path, now):
    with ExecutionStore(tmp_path / "execution.db") as store:
        task = store.create_task("validate clock", tmp_path)
        pending = store.create_run(task.id)

        with pytest.raises(ValueError, match="now must be a finite"):
            store.claim_run(pending.id, now=now)

        assert store.get_run(pending.id) == pending


def test_execution_schema_version_mismatch_fails_before_opening_state(tmp_path):
    path = tmp_path / "execution.db"
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            "CREATE TABLE execution_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL)",
        )
        connection.execute(
            "INSERT INTO execution_meta(key,value) VALUES('schema_version','999')",
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(ExecutionSchemaVersionMismatch, match="schema version 999"):
        ExecutionStore(path)

    connection = sqlite3.connect(path)
    try:
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name='execution_tasks'",
        ).fetchone() is None
    finally:
        connection.close()


def test_stable_public_execution_surface_excludes_low_level_phase_runner():
    assert lipas.ExecutionStore is ExecutionStore
    assert lipas.ApprovalPolicy is not None
    assert lipas.ExecutionSchemaVersionMismatch is ExecutionSchemaVersionMismatch
    assert not hasattr(lipas, "DurableReActRunner")


def test_history_row_owns_every_execution_audit_tag():
    expected = {
        TAG_EXECUTION_TASK_CREATED,
        TAG_EXECUTION_TASK_COMPLETED,
        TAG_EXECUTION_TASK_CANCELLED,
        TAG_EXECUTION_RUN_CREATED,
        TAG_EXECUTION_RUN_CLAIMED,
        TAG_EXECUTION_LEASE_RENEWED,
        TAG_EXECUTION_CHECKPOINT_SAVED,
        TAG_EXECUTION_INTERRUPT_REQUESTED,
        TAG_EXECUTION_INTERRUPT_RESOLVED,
        TAG_EXECUTION_CANCEL_REQUESTED,
        TAG_EXECUTION_RUN_COMPLETED,
        TAG_EXECUTION_RUN_FAILED,
        TAG_EXECUTION_RUN_CANCELLED,
    }

    assert expected <= HistoryRow().namespace


def test_execution_transitions_mirror_to_claims_with_stable_ids(tmp_path):
    rowset = RowSet(ClaimStore(), [HistoryRow()])
    with ExecutionStore(tmp_path / "execution.db", rowset=rowset) as store:
        task, run = _active_run(store, tmp_path)
        token = run.lease_token or ""
        store.save_checkpoint(
            run.id,
            token,
            expected_version=0,
            phase="ready",
            state={},
            now=101,
        )
        interrupt = store.suspend(
            run.id,
            token,
            expected_version=1,
            phase="approval",
            checkpoint_state={},
            kind="approval",
            request={"action": "write"},
            now=102,
        )
        store.resolve_interrupt(interrupt.id, allow=True, now=103)
        resumed = store.claim_run(run.id, now=104)
        store.complete_run(
            run.id,
            resumed.lease_token or "",
            result={"ok": True},
            now=105,
        )

        assert store.repair_audit() == 0
        assert store.schema_version == 1

    tags = [claim.tag for claim in rowset.store]
    assert tags == [
        TAG_EXECUTION_TASK_CREATED,
        TAG_EXECUTION_RUN_CREATED,
        TAG_EXECUTION_RUN_CLAIMED,
        TAG_EXECUTION_CHECKPOINT_SAVED,
        TAG_EXECUTION_CHECKPOINT_SAVED,
        TAG_EXECUTION_INTERRUPT_REQUESTED,
        TAG_EXECUTION_INTERRUPT_RESOLVED,
        TAG_EXECUTION_RUN_CLAIMED,
        TAG_EXECUTION_RUN_COMPLETED,
        TAG_EXECUTION_TASK_COMPLETED,
    ]
    assert len({claim.claim_id for claim in rowset.store}) == len(tags)
    assert {claim.source for claim in rowset.store} == {"execution.store"}
    assert task.id in {claim.fields.get("task_id") for claim in rowset.store}
    assert rowset.project("history")["event_count"] == len(tags)


def test_execution_audit_repairs_after_committed_transition_crash(tmp_path):
    class Crash(BaseException):
        pass

    path = tmp_path / "execution.db"
    rowset = RowSet(ClaimStore())
    store = ExecutionStore(path, rowset=rowset)
    original_fold = rowset.fold
    rowset.fold = lambda _claim: (_ for _ in ()).throw(Crash())  # type: ignore[method-assign]
    with pytest.raises(Crash):
        store.create_task("survive audit crash", tmp_path, task_id="task-crash")
    assert store.get_task("task-crash") is not None
    assert not rowset.store.filter(tag=TAG_EXECUTION_TASK_CREATED)
    rowset.fold = original_fold  # type: ignore[method-assign]
    store.close()

    with ExecutionStore(path, rowset=rowset) as reopened:
        claims = rowset.store.filter(tag=TAG_EXECUTION_TASK_CREATED)
        assert len(claims) == 1
        assert claims[0].claim_id.startswith("execution_audit_")
        assert reopened.repair_audit() == 0


def test_execution_open_repairs_only_a_bounded_outbox_batch(tmp_path):
    path = tmp_path / "execution.db"
    with ExecutionStore(path) as store:
        for index in range(300):
            store.create_task(
                f"task {index}", tmp_path, task_id=f"task-{index}",
            )

    rowset = RowSet(ClaimStore())
    with ExecutionStore(path, rowset=rowset) as reopened:
        assert len(rowset.store) == 256
        assert reopened.repair_audit() == 44
        assert len(rowset.store) == 300


def test_cancel_task_reaches_cancelled_state_and_stops_active_run(tmp_path):
    rowset = RowSet(ClaimStore())
    with ExecutionStore(tmp_path / "execution.db", rowset=rowset) as store:
        task = store.create_task("cancel me", tmp_path)
        run = store.create_run(task.id)

        cancelled = store.cancel_task(task.id, now=100)

        assert cancelled.state is TaskState.CANCELLED
        assert store.get_run(run.id).state is RunState.CANCELLED  # type: ignore[union-attr]
        assert store.cancel_task(task.id, now=101) == cancelled
        assert len(rowset.store.filter(tag=TAG_EXECUTION_TASK_CANCELLED)) == 1
        assert len(rowset.store.filter(tag=TAG_EXECUTION_RUN_CANCELLED)) == 1
