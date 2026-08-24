"""Schema-v2 composition, migration, and operational diagnostics."""
from __future__ import annotations

import os
import sqlite3
from types import SimpleNamespace

import pytest

from lipas import (
    LIPASRuntime,
    OperationJournal,
    SQLiteSessionStore,
    WorkspaceMigrationRequired,
    WorkspaceStorage,
    open_session,
)
from lipas.behaviour import AgentState
from lipas.cli import main
from lipas.orchestration import Mailbox
from lipas.workbench import Workbench


def _close_rowset(rowset) -> None:
    close = getattr(rowset.store, "close", None)
    if callable(close):
        close()


def test_runtime_schema_v2_consolidates_global_state_but_not_run_evidence(tmp_path):
    home = tmp_path / "state"
    workspace = tmp_path / "project"
    workspace.mkdir()
    with LIPASRuntime.open(home, sandbox="local") as runtime:
        task, run = runtime.workbench.create_task("inspect", workspace)
        runtime.operations.prepare(
            key="operation-1", kind="publish", request={"value": 1},
        )
        runtime.handoffs.send(
            sender="user", recipient="reviewer", payload={"task": task.id},
            message_id="handoff-1",
        )
        runtime.sessions.save(
            "conversation-1", AgentState(messages=("hello",)),
            expected_version=0,
        )

        assert runtime.claims_path == home / "workspace.db"
        assert runtime.operations_path == runtime.claims_path
        assert runtime.workbench.execution_path == runtime.claims_path
        assert runtime.workbench.product_path == runtime.claims_path
        assert runtime.workbench.claims_path_for_run(run.id) == (
            home / "runs" / run.id / "claims.db"
        )
        assert runtime.audit().healthy

    assert (home / "workspace.db").is_file()
    assert not (home / "execution.db").exists()
    assert not (home / "workbench.db").exists()
    assert not (home / "operations.db").exists()
    with sqlite3.connect(home / "workspace.db") as connection:
        tables = {
            row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'",
            )
        }
    assert {
        "runtime_meta", "execution_runs", "workbench_events", "operations",
        "mailbox", "claims", "lipas_conversations",
    } <= tables


def test_run_evidence_mirror_excludes_other_runs_control_events(tmp_path):
    home = tmp_path / "state"
    project = tmp_path / "project"
    project.mkdir()
    run_claims = None
    with LIPASRuntime.open(home, sandbox="local") as runtime:
        _, selected = runtime.workbench.create_task("selected", project)
        _, other = runtime.workbench.create_task("other", project)
        run_claims = open_session(
            runtime.workbench.claims_path_for_run(selected.id),
        )
        with runtime.workbench.execution_scope(
            run_claims, run_id=selected.id,
        ):
            mirrored = [
                claim for claim in run_claims.store
                if claim.source == "execution.store"
            ]
        assert mirrored
        assert all(claim.fields.get("run_id") == selected.id for claim in mirrored)
        assert not any(claim.fields.get("run_id") == other.id for claim in mirrored)
    assert run_claims is not None
    _close_rowset(run_claims)


def test_legacy_workspace_migration_is_explicit_backed_up_and_verified(
    tmp_path, monkeypatch,
):
    home = tmp_path / "state"
    project = tmp_path / "project"
    project.mkdir()
    claims = open_session(home / "claims.db")
    try:
        with Workbench(home, rowset=claims, sandbox="local") as workbench:
            task, run = workbench.create_task("legacy task", project)
            workbench.add_artifact(
                task_id=task.id, run_id=run.id, kind="note",
                metadata={"source": "v1"},
            )
        with OperationJournal(home / "operations.db", rowset=claims) as journal:
            journal.prepare(key="legacy-op", kind="publish", request={"x": 1})
        with Mailbox(home / "mailbox.db", rowset=claims) as mailbox:
            mailbox.send(
                sender="legacy", recipient="reviewer", payload={"x": 1},
                message_id="legacy-handoff",
            )
        with SQLiteSessionStore(home / "conversation.db") as sessions:
            sessions.save(
                "legacy-session", AgentState(messages=("old",)),
                expected_version=0,
            )
    finally:
        _close_rowset(claims)

    storage = WorkspaceStorage(home)
    plan = storage.plan_migration()
    assert plan.required
    assert plan.can_apply
    assert plan.table_rows["execution.db:execution_tasks"] == 1
    with pytest.raises(WorkspaceMigrationRequired):
        LIPASRuntime.open(home, sandbox="local")

    original_plan = storage.plan_migration
    plan_calls = 0

    def tracked_plan():
        nonlocal plan_calls
        plan_calls += 1
        return original_plan()

    monkeypatch.setattr(storage, "plan_migration", tracked_plan)
    result = storage.migrate()
    assert plan_calls == 2  # preflight plus revalidation under the lock
    assert result.backup_path is not None
    assert (result.backup_path / "manifest.json").is_file()
    assert (result.backup_path / "execution.db").is_file()
    assert (home / "execution.db").is_file()

    with LIPASRuntime.open(home, sandbox="local") as runtime:
        assert runtime.execution.get_task(task.id).goal == "legacy task"
        assert runtime.operations.get("legacy-op").kind == "publish"
        assert runtime.handoffs.get("legacy-handoff").recipient == "reviewer"
        assert runtime.sessions.load("legacy-session").state.messages == ("old",)
        assert runtime.workbench.artifacts(task.id)[0].metadata == {"source": "v1"}
        assert runtime.audit(repair=True).healthy
    assert storage.inspect().current
    assert not storage.audit()


def test_rollback_preserves_v2_database_and_reactivates_legacy_layout(tmp_path):
    home = tmp_path / "state"
    with Workbench(home, sandbox="local") as workbench:
        project = tmp_path / "project"
        project.mkdir()
        workbench.create_task("legacy", project)
    storage = WorkspaceStorage(home)
    storage.migrate()
    with LIPASRuntime.open(home, sandbox="local") as runtime:
        runtime.workbench.create_task("v2-only", project)

    preserved = storage.rollback()
    assert preserved.is_file()
    assert not (home / "workspace.db").exists()
    assert (home / "execution.db").is_file()
    with pytest.raises(WorkspaceMigrationRequired):
        LIPASRuntime.open(home, sandbox="local")


def test_migration_recovers_dead_pid_lock_but_refuses_active_lock(tmp_path):
    home = tmp_path / "state"
    project = tmp_path / "project"
    project.mkdir()
    with Workbench(home, sandbox="local") as workbench:
        workbench.create_task("legacy", project)
    lock_path = home / ".migration.lock"
    lock_path.write_text("pid=99999999\n", encoding="ascii")

    storage = WorkspaceStorage(home)
    assert storage.inspect().issues[0].code == "stale_migration_lock"
    assert storage.migrate().database_path.is_file()
    assert not lock_path.exists()

    preserved = storage.rollback()
    assert preserved.is_file()
    lock_path.write_text(f"pid={os.getpid()}\n", encoding="ascii")
    assert storage.inspect().issues[0].code == "active_migration_lock"
    with pytest.raises(WorkspaceMigrationRequired, match="active pid"):
        storage.migrate()
    assert lock_path.is_file()


def test_rollback_refuses_active_runtime_and_sqlite_writer(tmp_path):
    home = tmp_path / "state"
    project = tmp_path / "project"
    project.mkdir()
    with Workbench(home, sandbox="local") as workbench:
        workbench.create_task("legacy", project)
    storage = WorkspaceStorage(home)
    storage.migrate()

    runtime = LIPASRuntime.open(home, sandbox="local")
    try:
        with pytest.raises(WorkspaceMigrationRequired, match="workspace is busy"):
            storage.rollback()
        assert storage.database_path.is_file()
    finally:
        runtime.close()

    writer = sqlite3.connect(storage.database_path)
    try:
        writer.execute("BEGIN IMMEDIATE")
        writer.execute(
            "INSERT INTO runtime_meta(key,value) VALUES('writer_probe','active')",
        )
        with pytest.raises(
            WorkspaceMigrationRequired, match="(?:WAL|database) is busy",
        ):
            storage.rollback()
        assert storage.database_path.is_file()
        writer.rollback()
    finally:
        writer.close()

    preserved = storage.rollback()
    with sqlite3.connect(preserved) as connection:
        assert connection.execute("PRAGMA quick_check").fetchone() == ("ok",)


def test_schema_bootstrap_preserves_constructor_error_and_closes_prior_stores(
    tmp_path, monkeypatch,
):
    calls = []

    class Resource:
        def __init__(self, name, *, fail_close=False):
            self.name = name
            self.fail_close = fail_close

        def close(self):
            calls.append(self.name)
            if self.fail_close:
                raise RuntimeError(f"{self.name} close failed")

    claims = SimpleNamespace(store=Resource("claims"))
    monkeypatch.setattr("lipas.session.open_session", lambda _path: claims)
    monkeypatch.setattr(
        "lipas.workbench.Workbench",
        lambda *_args, **_kwargs: Resource("workbench", fail_close=True),
    )

    class ConstructorFailure(RuntimeError):
        pass

    def fail_operations(*_args, **_kwargs):
        raise ConstructorFailure("operation schema failed")

    monkeypatch.setattr("lipas.operations.OperationJournal", fail_operations)
    storage = WorkspaceStorage(tmp_path / "state")
    with pytest.raises(ConstructorFailure, match="operation schema failed"):
        storage._bootstrap_component_schemas(tmp_path / "workspace.db")
    assert calls == ["workbench", "claims"]


def test_doctor_is_read_only_and_task_cli_uses_workspace_database(
    tmp_path, capsys, monkeypatch,
):
    monkeypatch.setattr("lipas.cli._sandbox_diagnostics", lambda: {
        "name": "bubblewrap",
        "discovered": True,
        "operational": True,
        "isolated": True,
        "network_isolated": True,
        "error": None,
    })
    empty = tmp_path / "empty"
    assert main(["doctor", "--home", str(empty), "--json"]) == 0
    assert not empty.exists()
    capsys.readouterr()

    home = tmp_path / "state"
    project = tmp_path / "project"
    project.mkdir()
    assert main([
        "task", "submit", str(project), "inspect", "--home", str(home),
    ]) == 0
    assert (home / "workspace.db").is_file()
    assert not (home / "execution.db").exists()
    capsys.readouterr()
    assert main(["doctor", "--home", str(home), "--json"]) == 0
    assert '"healthy": true' in capsys.readouterr().out
    assert main(["audit", "--home", str(home)]) == 0
    audit = capsys.readouterr().out
    assert '"claim_audit": "not_run"' in audit
    assert '"claim_issues": null' in audit


def test_doctor_reports_discovered_but_broken_sandbox_as_not_ready(
    tmp_path, capsys, monkeypatch,
):
    monkeypatch.setattr("lipas.cli._sandbox_diagnostics", lambda: {
        "name": "bubblewrap",
        "discovered": True,
        "operational": False,
        "isolated": True,
        "network_isolated": True,
        "error": "probe failed",
    })
    assert main(["doctor", "--home", str(tmp_path / "empty"), "--json"]) == 1
    output = capsys.readouterr().out
    assert '"healthy": false' in output
    assert '"storage_healthy": true' in output
    assert '"ready": false' in output
    assert '"operational": false' in output


def test_migration_cli_requires_confirmation_then_verifies(tmp_path, capsys):
    home = tmp_path / "state"
    project = tmp_path / "project"
    project.mkdir()
    with Workbench(home, sandbox="local") as workbench:
        workbench.create_task("legacy", project)

    assert main([
        "migrate", "plan", "--home", str(home), "--json",
    ]) == 0
    assert '"required": true' in capsys.readouterr().out
    with pytest.raises(SystemExit) as rejected:
        main(["migrate", "apply", "--home", str(home)])
    assert rejected.value.code == 2
    capsys.readouterr()

    assert main([
        "migrate", "apply", "--home", str(home), "--yes",
    ]) == 0
    capsys.readouterr()
    assert main([
        "migrate", "verify", "--home", str(home),
    ]) == 0
    output = capsys.readouterr().out
    assert '"state": "current"' in output
    assert '"sandbox"' not in output


def test_offline_tour_exercises_input_approval_events_and_audit(capsys):
    assert main(["tour", "--offline", "--json"]) == 0
    output = capsys.readouterr().out
    assert '"stage": "input_requested"' in output
    assert '"stage": "approval_requested"' in output
    assert '"stage": "run_completed"' in output
    assert '"input_tool_body_executed": false' in output
    assert '"published": [' in output
    assert '"audit_healthy": true' in output
