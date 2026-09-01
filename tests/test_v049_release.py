"""0.49 release-candidate backup, restore, and migration gates."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

from lipas import LIPASRuntime, WorkspaceStorage


def test_evidence_bundle_round_trip_captures_run_claims_and_hashes(tmp_path: Path):
    home = tmp_path / "state"
    project = tmp_path / "project"
    project.mkdir()
    with LIPASRuntime.open(home, sandbox="local") as runtime:
        _, run = runtime.workbench.create_task("bundle", project)
        claims = runtime.workbench.claims_path_for_run(run.id)
        claims.parent.mkdir(parents=True, exist_ok=True)
        import sqlite3

        with sqlite3.connect(claims) as connection:
            connection.execute("CREATE TABLE evidence(value TEXT)")
            connection.execute("INSERT INTO evidence VALUES ('kept')")
    storage = WorkspaceStorage(home)
    bundle = storage.backup_bundle(tmp_path / "evidence-bundle")
    assert "runs/" + run.id + "/claims.db" in bundle.files
    manifest = bundle.bundle_path / "manifest.json"
    assert manifest.is_file()
    restored_home = tmp_path / "restored"
    restored = WorkspaceStorage(restored_home).restore_bundle(bundle.bundle_path)
    assert restored.restored
    with sqlite3.connect(restored_home / "runs" / run.id / "claims.db") as connection:
        assert connection.execute("SELECT value FROM evidence").fetchone() == ("kept",)


def test_evidence_bundle_rejects_tampering_and_symlinks(tmp_path: Path):
    home = tmp_path / "state"
    WorkspaceStorage(home).require_current(create=True)
    bundle = WorkspaceStorage(home).backup_bundle(tmp_path / "bundle")
    database = bundle.bundle_path / "workspace.db"
    database.write_bytes(database.read_bytes() + b"tampered")
    import pytest
    from lipas.workspace_storage import WorkspaceSchemaMismatch

    with pytest.raises(WorkspaceSchemaMismatch, match="(size|hash) mismatch"):
        WorkspaceStorage(tmp_path / "target").restore_bundle(bundle.bundle_path)


def test_evidence_bundle_can_be_verified_without_opening_target(tmp_path: Path):
    home = tmp_path / "state"
    WorkspaceStorage(home).require_current(create=True)
    bundle = WorkspaceStorage(home).backup_bundle(tmp_path / "bundle")
    verified = WorkspaceStorage(tmp_path / "does-not-exist").verify_bundle(bundle.bundle_path)
    assert verified.bundle_path == bundle.bundle_path
    assert verified.restored is False
    assert "workspace.db" in verified.files


def test_backup_restore_keeps_real_pre_restore_backup_identity(tmp_path: Path):
    home = tmp_path / "state"
    storage = WorkspaceStorage(home)
    with LIPASRuntime.open(home, sandbox="local"):
        pass
    backup = storage.backup(tmp_path / "snapshot.db")
    assert backup.backup_path is not None
    restored = storage.restore(backup.backup_path)
    assert restored.backup_path is not None
    assert restored.backup_path.is_file()
    assert restored.backup_path != backup.database_path


def test_workspace_inspect_fails_closed_on_symlinked_layout(tmp_path: Path):
    home = tmp_path / "state"
    WorkspaceStorage(home).require_current(create=True)
    runs = home / "runs"
    runs.mkdir()
    moved = tmp_path / "runs-target"
    runs.rename(moved)
    try:
        runs.symlink_to(moved, target_is_directory=True)
    except (OSError, NotImplementedError):
        moved.rename(runs)
        return
    try:
        status = WorkspaceStorage(home).inspect()
        assert status.state == "invalid"
        assert any(issue.code == "runs_symlink" for issue in status.issues)
    finally:
        runs.unlink(missing_ok=True)
        moved.rename(runs)


def test_runtime_recovers_a_partial_bundle_publish_to_old_tree(tmp_path: Path):
    source = tmp_path / "source"
    target = tmp_path / "target"
    from lipas import install_workspace

    install_workspace(source, sandbox="local")
    install_workspace(target, sandbox="local")
    old_database = target / ".workspace.db.pre-bundle-test"
    old_runs = target / ".runs.pre-bundle-test"
    old_manifest = target / ".installation.json.pre-bundle-test"
    (target / "workspace.db").replace(old_database)
    (target / "runs").replace(old_runs)
    (target / ".installation.json").replace(old_manifest)
    # Simulate a process dying after publishing only workspace.db.
    shutil.copy2(source / "workspace.db", target / "workspace.db")
    marker = {
        "version": 1,
        "phase": "database_published",
        "temporary": str(target / ".workspace-bundle.restore-test"),
        "database": str(target / "workspace.db"),
        "runs": str(target / "runs"),
        "manifest": str(target / ".installation.json"),
        "old_database": str(old_database),
        "old_runs": str(old_runs),
        "old_manifest": str(old_manifest),
        "had_database": True,
        "had_runs": True,
        "had_manifest": True,
    }
    (target / ".restore.pending.json").write_text(
        json.dumps(marker), encoding="utf-8",
    )
    with LIPASRuntime.open(target, sandbox="local"):
        pass
    assert not (target / ".restore.pending.json").exists()
    assert (target / "workspace.db").is_file()
    assert (target / "runs").is_dir()
    assert (target / ".installation.json").is_file()
