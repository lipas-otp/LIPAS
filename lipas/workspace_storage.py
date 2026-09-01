"""Versioned storage boundary for one local LIPAS runtime workspace.

Schema v2 is a composition schema: compatible global tables share one SQLite
file while every Run keeps its own Claim/Effect tape.  The latter is an
intentional authority and budget boundary, not an accidental second product
state machine.

Migration is explicit and copy-on-write.  Legacy databases remain untouched,
the target is assembled in a temporary file, checked, and only then moved into
place.  Runtime opening never silently rewrites an existing workspace.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import sqlite3
import stat
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from pathlib import PurePosixPath
from typing import Any, Iterator, Mapping

try:  # POSIX advisory locks protect first-party Runtime lifecycles.
    import fcntl as _fcntl
except ImportError:  # pragma: no cover - exercised on non-POSIX hosts
    _fcntl = None  # type: ignore[assignment]

__all__ = [
    "WORKSPACE_DATABASE_NAME",
    "WORKSPACE_SCHEMA_VERSION",
    "RuntimeStorageIssue",
    "WorkspaceMigrationPlan",
    "WorkspaceMigrationRequired",
    "WorkspaceMigrationResult",
    "WorkspaceSchemaMismatch",
    "WorkspaceStatus",
    "WorkspaceBackup",
    "WorkspaceBundle",
    "WorkspaceBackupBundle",
    "WorkspaceStorage",
]


WORKSPACE_DATABASE_NAME = "workspace.db"
WORKSPACE_SCHEMA_VERSION = 2
_MIGRATION_LOCK_NAME = ".migration.lock"
_RUNTIME_LOCK_NAME = ".runtime.lock"
_RESTORE_MARKER_NAME = ".restore.pending.json"
_MALFORMED_LOCK_GRACE_SECONDS = 30.0


class WorkspaceSchemaMismatch(RuntimeError):
    """A workspace database is not understood by this LIPAS release."""


class WorkspaceMigrationRequired(RuntimeError):
    """Legacy state exists and must be migrated explicitly before opening."""


@dataclass(frozen=True, slots=True)
class RuntimeStorageIssue:
    code: str
    message: str
    severity: str = "error"
    context: Mapping[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "severity": self.severity,
            "context": dict(self.context),
        }


@dataclass(frozen=True, slots=True)
class WorkspaceStatus:
    home: Path
    database_path: Path
    state: str
    schema_version: int | None
    legacy_files: tuple[Path, ...] = ()
    issues: tuple[RuntimeStorageIssue, ...] = ()

    @property
    def current(self) -> bool:
        return (
            self.state == "current"
            and self.schema_version == WORKSPACE_SCHEMA_VERSION
            and not any(issue.severity == "error" for issue in self.issues)
        )

    @property
    def migration_required(self) -> bool:
        return self.state == "migration_required"

    def as_dict(self) -> dict[str, Any]:
        return {
            "home": str(self.home),
            "database_path": str(self.database_path),
            "state": self.state,
            "schema_version": self.schema_version,
            "current_schema_version": WORKSPACE_SCHEMA_VERSION,
            "legacy_files": [str(path) for path in self.legacy_files],
            "issues": [issue.as_dict() for issue in self.issues],
        }


@dataclass(frozen=True, slots=True)
class WorkspaceBackup:
    """Result of a copy-on-write workspace backup/restore operation."""

    database_path: Path
    backup_path: Path | None
    restored: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "database_path": str(self.database_path),
            "backup_path": None if self.backup_path is None else str(self.backup_path),
            "restored": self.restored,
        }


@dataclass(frozen=True, slots=True)
class WorkspaceBundle:
    """A verified workspace snapshot including per-Run evidence tapes.

    ``backup_path`` is populated when restoring over an existing workspace and
    points to a separately retained rollback bundle.  ``bundle_path`` is a
    directory containing ``manifest.json``, ``workspace.db`` and any
    ``runs/<run-id>`` evidence files.
    """

    database_path: Path
    bundle_path: Path
    backup_path: Path | None = None
    restored: bool = False
    files: tuple[str, ...] = ()

    @property
    def path(self) -> Path:
        """Convenient alias for callers that treat a bundle as an artifact."""
        return self.bundle_path

    @property
    def manifest_path(self) -> Path:
        return self.bundle_path / "manifest.json"

    def as_dict(self) -> dict[str, Any]:
        return {
            "database_path": str(self.database_path),
            "bundle_path": str(self.bundle_path),
            "backup_path": None if self.backup_path is None else str(self.backup_path),
            "restored": self.restored,
            "files": list(self.files),
        }


# Descriptive compatibility alias for callers that prefer the operation name.
WorkspaceBackupBundle = WorkspaceBundle


@dataclass(frozen=True, slots=True)
class WorkspaceMigrationPlan:
    home: Path
    database_path: Path
    legacy_files: tuple[Path, ...]
    table_rows: Mapping[str, int]
    required: bool
    issues: tuple[RuntimeStorageIssue, ...] = ()

    @property
    def rows(self) -> int:
        return sum(self.table_rows.values())

    @property
    def can_apply(self) -> bool:
        return self.required and not any(
            issue.severity == "error" for issue in self.issues
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "home": str(self.home),
            "database_path": str(self.database_path),
            "from": "legacy-v1",
            "to_schema_version": WORKSPACE_SCHEMA_VERSION,
            "required": self.required,
            "can_apply": self.can_apply,
            "legacy_files": [str(path) for path in self.legacy_files],
            "table_rows": dict(self.table_rows),
            "rows": self.rows,
            "issues": [issue.as_dict() for issue in self.issues],
        }


@dataclass(frozen=True, slots=True)
class WorkspaceMigrationResult:
    database_path: Path
    backup_path: Path | None
    table_rows: Mapping[str, int]
    already_current: bool = False

    @property
    def rows(self) -> int:
        return sum(self.table_rows.values())

    def as_dict(self) -> dict[str, Any]:
        return {
            "database_path": str(self.database_path),
            "backup_path": None if self.backup_path is None else str(self.backup_path),
            "table_rows": dict(self.table_rows),
            "rows": self.rows,
            "already_current": self.already_current,
        }


_RUNTIME_SCHEMA = """
CREATE TABLE IF NOT EXISTS runtime_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


# The order is significant for execution foreign keys.  Names are fixed here
# rather than accepted from users, so quoting is only defensive.
_LEGACY_DATABASES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("claims.db", ("meta", "claims")),
    (
        "execution.db",
        (
            "execution_meta",
            "execution_tasks",
            "execution_runs",
            "execution_checkpoints",
            "execution_interrupts",
            "execution_audit_events",
            "execution_agent_events",
        ),
    ),
    (
        "workbench.db",
        (
            "workbench_meta",
            "workbench_artifacts",
            "workbench_reports",
            "workbench_run_sessions",
            "workbench_change_sets",
            "workbench_events",
        ),
    ),
    (
        "operations.db",
        ("operation_meta", "operations", "operation_audit_events"),
    ),
    (
        "conversation.db",
        (
            "lipas_conversation_meta",
            "lipas_conversations",
            "lipas_chat_conversations",
            "lipas_chat_messages",
            "lipas_chat_events",
        ),
    ),
    (
        "mailbox.db",
        ("mailbox_meta", "mailbox", "mailbox_audit_events"),
    ),
)


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _read_only_uri(path: Path) -> str:
    return f"{path.resolve().as_uri()}?mode=ro"


def _immutable_read_only_uri(path: Path) -> str:
    """Read an offline snapshot without creating WAL/SHM sidecars."""
    return f"{path.resolve().as_uri()}?mode=ro&immutable=1"


def _sqlite_backup(source: Path, destination: Path) -> None:
    """Take a consistent SQLite snapshot, including any source WAL pages."""
    with sqlite3.connect(source) as source_conn, sqlite3.connect(destination) as target_conn:
        source_conn.backup(target_conn)


def _remove_sqlite_sidecars(path: Path) -> None:
    """Remove WAL/SHM files created beside an offline snapshot."""
    for suffix in ("-wal", "-shm"):
        Path(str(path) + suffix).unlink(missing_ok=True)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_restore_marker(path: Path, payload: Mapping[str, Any]) -> None:
    """Atomically persist a restore-recovery marker and flush its directory."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent), text=True)
    temporary = Path(raw)
    try:
        if os.name == "posix":
            os.fchmod(fd, 0o600)
        encoded = json.dumps(
            dict(payload), ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False,
        ) + "\n"
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
        if os.name == "posix":
            os.chmod(path, 0o600)
        if os.name == "posix":
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)


def _is_sqlite_sidecar(path: Path) -> bool:
    """Identify transient SQLite files that are folded into the .db snapshot."""
    return any(
        path.name.endswith(suffix) and path.name[: -len(suffix)].endswith(".db")
        for suffix in ("-wal", "-shm", "-journal")
    )


def _close_all(resources: tuple[Any, ...]) -> BaseException | None:
    """Close all initialized resources and retain the first cleanup error."""
    first_error: BaseException | None = None
    for resource in resources:
        close = getattr(resource, "close", None)
        if not callable(close):
            continue
        try:
            close()
        except BaseException as exc:
            if first_error is None:
                first_error = exc
    return first_error


def _pid_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class _WorkspaceLease:
    """Lifetime lock shared by Runtime and held exclusively by rollback."""

    def __init__(self, file_descriptor: int) -> None:
        self._file_descriptor = file_descriptor
        self._closed = False

    def close(self) -> None:
        if self._closed:
            return
        try:
            if _fcntl is not None:
                _fcntl.flock(self._file_descriptor, _fcntl.LOCK_UN)
        finally:
            os.close(self._file_descriptor)
            self._closed = True

    def make_shared(self) -> None:
        """Downgrade an initialization lease after all schemas are ready."""
        if self._closed:
            raise RuntimeError("workspace lease is closed")
        if _fcntl is not None:
            _fcntl.flock(self._file_descriptor, _fcntl.LOCK_SH)

    def __enter__(self) -> "_WorkspaceLease":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()


class WorkspaceStorage:
    """Inspect, initialize, migrate, and audit a local runtime workspace."""

    def __init__(self, home: str | Path) -> None:
        self.home = Path(home).expanduser().resolve()
        self.database_path = self.home / WORKSPACE_DATABASE_NAME

    def backup(self, destination: str | Path | None = None) -> WorkspaceBackup:
        """Create a consistent SQLite backup without stopping a shared Runtime."""
        self.recover_pending_restore()
        raw_target = Path(destination).expanduser() if destination is not None else None
        if raw_target is not None and raw_target.is_symlink():
            raise ValueError("backup destination must not be a symlink")
        target = raw_target.resolve() if raw_target is not None else self.home / f"workspace.db.backup-{int(time.time())}"
        lease = self.acquire_runtime_lease(exclusive=False)
        try:
            # Hold the shared lifecycle lease across both the status check and
            # SQLite backup.  Otherwise a concurrent migration/restore could
            # replace workspace.db between ``require_current`` and opening
            # the source connection.
            source = self.require_current(create=False)
            if target == source:
                raise ValueError("backup destination must differ from workspace.db")
            if target.exists() and os.path.samefile(target, source):
                raise ValueError(
                    "backup destination must not reference workspace.db",
                )
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_name(f".{target.name}.tmp-{uuid.uuid4().hex}")
            _sqlite_backup(source, temporary)
            with contextlib.closing(
                sqlite3.connect(_read_only_uri(temporary), uri=True),
            ) as snapshot:
                row = snapshot.execute(
                    "SELECT value FROM runtime_meta WHERE key='schema_version'",
                ).fetchone()
                try:
                    version = int(row[0]) if row is not None else -1
                except (TypeError, ValueError, OverflowError) as exc:
                    raise WorkspaceSchemaMismatch(
                        "backup schema version is malformed",
                    ) from exc
                if version != WORKSPACE_SCHEMA_VERSION:
                    raise WorkspaceSchemaMismatch(
                        "backup schema does not match this release",
                    )
                integrity = snapshot.execute("PRAGMA integrity_check").fetchone()
                if integrity is None or integrity[0] != "ok":
                    raise WorkspaceSchemaMismatch(
                        "backup failed SQLite integrity_check",
                    )
            temporary.replace(target)
            # ``replace`` swaps only the main file.  If the destination was a
            # previously-used WAL database, stale sidecars could be attached
            # to the new snapshot on its next open.  They are never part of a
            # backup artifact and must not survive the atomic swap.
            for suffix in ("-wal", "-shm"):
                Path(str(target) + suffix).unlink(missing_ok=True)
        finally:
            lease.close()
            if 'temporary' in locals() and temporary.exists():
                temporary.unlink()
            if 'temporary' in locals():
                for suffix in ("-wal", "-shm"):
                    Path(str(temporary) + suffix).unlink(missing_ok=True)
        return WorkspaceBackup(source, target, False)

    def backup_bundle(self, destination: str | Path | None = None) -> WorkspaceBundle:
        """Create a verified directory snapshot of the complete workspace.

        The legacy :meth:`backup` method intentionally remains a single-file
        SQLite snapshot.  This explicit bundle API additionally captures every
        regular file below ``runs/`` (including each Run's ``claims.db``),
        records SHA-256/size metadata, and validates the SQLite snapshots.
        """
        self.recover_pending_restore()
        target = (
            Path(destination).expanduser().resolve()
            if destination is not None
            else self._new_backup_directory("workspace-bundle")
        )
        if target == self.home or target.is_relative_to(self.home / "runs"):
            raise ValueError("bundle destination must not be the workspace or runs directory")
        if target.exists():
            if target.is_symlink() or not target.is_dir():
                raise FileExistsError(target)
            if any(target.iterdir()):
                raise FileExistsError(f"bundle destination is not empty: {target}")
        lease = self.acquire_runtime_lease(exclusive=False)
        try:
            source = self.require_current(create=False)
            return self._backup_bundle_locked(source, target)
        finally:
            lease.close()

    def verify_bundle(self, source: str | Path) -> WorkspaceBundle:
        """Verify a complete workspace/evidence bundle without changing state.

        The returned :class:`WorkspaceBundle` is an immutable description of
        the verified artifact.  Unlike ``restore_bundle`` this method never
        acquires a maintenance lease and never copies or rewrites files,
        making it safe for offline preflight and CI checks.
        """
        raw_source = Path(source).expanduser()
        if raw_source.is_symlink():
            raise ValueError("bundle source must not be a symlink")
        bundle_path = raw_source.resolve()
        entries = self._verify_bundle(bundle_path)
        return WorkspaceBundle(
            bundle_path / WORKSPACE_DATABASE_NAME,
            bundle_path,
            None,
            False,
            tuple(entry["path"] for entry in entries),
        )

    def restore_bundle(
        self,
        source: str | Path,
        *,
        keep_backup: bool = True,
    ) -> WorkspaceBundle:
        """Restore a manifest- and hash-verified complete workspace bundle.

        Restore is staged in a temporary directory and published only after
        every file has been checked.  Existing state is retained as a rollback
        bundle by default; no partially copied evidence directory is exposed
        to a Runtime.
        """
        if not isinstance(keep_backup, bool):
            raise TypeError("keep_backup must be bool")
        self.recover_pending_restore()
        raw_bundle_path = Path(source).expanduser()
        if raw_bundle_path.is_symlink():
            raise ValueError("bundle source must not be a symlink")
        bundle_path = raw_bundle_path.resolve()
        entries = self._verify_bundle(bundle_path)
        lease = self.acquire_runtime_lease(exclusive=True)
        temporary: Path | None = None
        old_database: Path | None = None
        old_runs: Path | None = None
        old_manifest: Path | None = None
        published_database = False
        published_runs = False
        published_manifest = False
        rollback_bundle: Path | None = None
        try:
            self.home.mkdir(parents=True, exist_ok=True)
            if self.database_path.exists() and self.database_path.is_symlink():
                raise WorkspaceSchemaMismatch("workspace.db must not be a symlink")
            runs_path = self.home / "runs"
            if runs_path.exists() and runs_path.is_symlink():
                raise WorkspaceSchemaMismatch("runs directory must not be a symlink")
            if (
                not self.database_path.exists()
                and runs_path.is_dir()
                and any(runs_path.iterdir())
            ):
                raise WorkspaceSchemaMismatch(
                    "cannot restore over orphan runs/ without workspace.db",
                )
            manifest_path = self.home / ".installation.json"
            if manifest_path.is_symlink():
                raise WorkspaceSchemaMismatch("installation manifest must not be a symlink")
            if keep_backup and self.database_path.is_file():
                rollback_bundle = self._new_backup_directory("pre-restore-bundle")
                self._backup_bundle_locked(self.database_path, rollback_bundle)

            temporary = self.home / f".workspace-bundle.restore-{uuid.uuid4().hex}"
            temporary.mkdir(parents=True, exist_ok=False, mode=0o700)
            for entry in entries:
                relative = entry["path"]
                src = bundle_path / Path(relative)
                dst = temporary / Path(relative)
                dst.parent.mkdir(parents=True, exist_ok=True)
                if relative == ".installation.json":
                    try:
                        installation = json.loads(src.read_text(encoding="utf-8"))
                    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                        raise WorkspaceSchemaMismatch("installation manifest in bundle is invalid") from exc
                    if not isinstance(installation, Mapping):
                        raise WorkspaceSchemaMismatch("installation manifest in bundle is invalid")
                    # A bundle can be restored to a different home. Rewrite
                    # only location fields; package/schema metadata and
                    # timestamps remain evidence from the source install.
                    installation = dict(installation)
                    installation["home"] = str(self.home)
                    installation["database"] = str(self.database_path)
                    installation["runs"] = str(self.home / "runs")
                    dst.write_text(
                        json.dumps(installation, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
                        encoding="utf-8",
                    )
                elif entry.get("kind") == "sqlite":
                    _sqlite_backup(src, dst)
                    _remove_sqlite_sidecars(dst)
                else:
                    shutil.copy2(src, dst)
                if os.name == "posix":
                    os.chmod(dst, 0o600)
            staged_database = temporary / WORKSPACE_DATABASE_NAME
            staged_runs = temporary / "runs"
            if not staged_database.is_file():  # defensive; manifest verifies this
                raise WorkspaceSchemaMismatch("bundle has no staged workspace.db")
            staged_runs.mkdir(exist_ok=True)
            if os.name == "posix":
                for directory in (temporary, *[p for p in temporary.rglob("*") if p.is_dir()]):
                    os.chmod(directory, 0o700)

            # Write a durable recovery marker before the first destructive
            # rename.  A process crash between publishing workspace.db and
            # runs/ would otherwise strand a half-restored workspace with no
            # way for the next Runtime to distinguish old from new state.
            marker_path = self.home / _RESTORE_MARKER_NAME
            old_database = self.home / f".workspace.db.pre-bundle-{uuid.uuid4().hex}"
            old_runs = self.home / f".runs.pre-bundle-{uuid.uuid4().hex}"
            old_manifest = self.home / f".installation.json.pre-bundle-{uuid.uuid4().hex}"
            marker_payload = {
                "version": 1,
                "phase": "staged",
                "temporary": str(temporary),
                "database": str(self.database_path),
                "runs": str(runs_path),
                "manifest": str(self.home / ".installation.json"),
                "old_database": str(old_database),
                "old_runs": str(old_runs),
                "old_manifest": str(old_manifest),
                "had_database": self.database_path.exists(),
                "had_runs": runs_path.exists(),
                "had_manifest": (self.home / ".installation.json").exists(),
            }
            _write_restore_marker(marker_path, marker_payload)

            # Rename old trees out of the way before publishing both roots. If
            # the second replacement fails, the old paths are restored below.
            if self.database_path.exists():
                os.replace(self.database_path, old_database)
                marker_payload["phase"] = "database_moved"
                _write_restore_marker(marker_path, marker_payload)
            if runs_path.exists():
                os.replace(runs_path, old_runs)
                marker_payload["phase"] = "runs_moved"
                _write_restore_marker(marker_path, marker_payload)
            staged_manifest = temporary / ".installation.json"
            if staged_manifest.exists():
                if manifest_path.exists():
                    os.replace(manifest_path, old_manifest)
            os.replace(staged_database, self.database_path)
            published_database = True
            marker_payload["phase"] = "database_published"
            _write_restore_marker(marker_path, marker_payload)
            os.replace(staged_runs, runs_path)
            published_runs = True
            marker_payload["phase"] = "runs_published"
            _write_restore_marker(marker_path, marker_payload)
            if staged_manifest.exists():
                os.replace(staged_manifest, manifest_path)
                published_manifest = True
                marker_payload["phase"] = "manifest_published"
                _write_restore_marker(marker_path, marker_payload)
            for suffix in ("-wal", "-shm"):
                Path(str(self.database_path) + suffix).unlink(missing_ok=True)
            if os.name == "posix":
                os.chmod(self.home, 0o700)
                os.chmod(self.database_path, 0o600)
                os.chmod(runs_path, 0o700)
            if old_database is not None:
                old_database.unlink(missing_ok=True)
            if old_runs is not None and old_runs.exists():
                shutil.rmtree(old_runs, ignore_errors=False)
            marker_path.unlink(missing_ok=True)
            return WorkspaceBundle(
                self.database_path,
                bundle_path,
                rollback_bundle,
                True,
                tuple(entry["path"] for entry in entries),
            )
        except BaseException:
            # Best-effort rollback keeps a failed restore from stranding a
            # workspace with only one half of the bundle published.
            if published_runs:
                with contextlib.suppress(OSError):
                    shutil.rmtree(self.home / "runs")
            if old_runs is not None and old_runs.exists():
                with contextlib.suppress(OSError):
                    os.replace(old_runs, self.home / "runs")
            if published_database:
                with contextlib.suppress(OSError):
                    self.database_path.unlink()
            if old_database is not None and old_database.exists():
                with contextlib.suppress(OSError):
                    os.replace(old_database, self.database_path)
            if published_manifest:
                with contextlib.suppress(OSError):
                    (self.home / ".installation.json").unlink()
            if old_manifest is not None and old_manifest.exists():
                with contextlib.suppress(OSError):
                    os.replace(old_manifest, self.home / ".installation.json")
            raise
        finally:
            lease.close()
            if temporary is not None and temporary.exists():
                shutil.rmtree(temporary, ignore_errors=True)
            if old_database is not None and old_database.exists():
                old_database.unlink(missing_ok=True)
            if old_runs is not None and old_runs.exists():
                shutil.rmtree(old_runs, ignore_errors=True)
            if old_manifest is not None and old_manifest.exists():
                old_manifest.unlink(missing_ok=True)

    def recover_pending_restore(self) -> bool:
        """Recover a bundle restore interrupted by process or host failure.

        Returns ``True`` when a marker was found and settled.  Recovery is
        deliberately conservative: if neither a complete new tree nor a
        complete old tree can be proven, it raises instead of guessing which
        evidence set is authoritative.
        """
        marker_path = self.home / _RESTORE_MARKER_NAME
        if not marker_path.exists():
            return False
        if marker_path.is_symlink():
            raise WorkspaceSchemaMismatch("pending restore marker must not be a symlink")
        lease = self.acquire_runtime_lease(exclusive=True)
        try:
            # Another process may have settled the marker while we waited.
            if not marker_path.exists():
                return False
            if marker_path.is_symlink():
                raise WorkspaceSchemaMismatch("pending restore marker must not be a symlink")
            try:
                payload = json.loads(marker_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise WorkspaceSchemaMismatch("pending restore marker is invalid") from exc
            if not isinstance(payload, Mapping) or payload.get("version") != 1:
                raise WorkspaceSchemaMismatch("pending restore marker is invalid")

            def _marker_path(key: str, expected_name: str) -> Path:
                value = payload.get(key)
                if not isinstance(value, str):
                    raise WorkspaceSchemaMismatch("pending restore marker is invalid")
                path = Path(value).expanduser().resolve()
                if path.parent != self.home or path.name != expected_name and not path.name.startswith(expected_name):
                    raise WorkspaceSchemaMismatch("pending restore marker path is invalid")
                return path

            database = _marker_path("database", WORKSPACE_DATABASE_NAME)
            runs = _marker_path("runs", "runs")
            manifest = _marker_path("manifest", ".installation.json")
            temporary_value = payload.get("temporary")
            if not isinstance(temporary_value, str):
                raise WorkspaceSchemaMismatch("pending restore marker is invalid")
            temporary = Path(temporary_value).expanduser().resolve()
            if temporary.parent != self.home or not temporary.name.startswith(".workspace-bundle.restore-"):
                raise WorkspaceSchemaMismatch("pending restore temporary path is invalid")
            old_database = _marker_path("old_database", ".workspace.db.pre-bundle-")
            old_runs = _marker_path("old_runs", ".runs.pre-bundle-")
            old_manifest = _marker_path("old_manifest", ".installation.json.pre-bundle-")
            for candidate in (
                database, runs, manifest, temporary,
                old_database, old_runs, old_manifest,
            ):
                if candidate.is_symlink():
                    raise WorkspaceSchemaMismatch(
                        "pending restore contains a symlinked recovery path",
                    )
            had_database = payload.get("had_database") is True
            had_runs = payload.get("had_runs") is True
            had_manifest = payload.get("had_manifest") is True

            new_complete = database.is_file() and runs.is_dir() and (
                not had_manifest or manifest.is_file()
            )
            old_complete = (
                (not had_database or old_database.is_file())
                and (not had_runs or old_runs.is_dir())
                and (not had_manifest or old_manifest.is_file())
            )
            if new_complete:
                old_database.unlink(missing_ok=True)
                if old_runs.exists():
                    shutil.rmtree(old_runs, ignore_errors=False)
                old_manifest.unlink(missing_ok=True)
            elif old_complete:
                if database.exists():
                    database.unlink()
                if runs.exists():
                    shutil.rmtree(runs, ignore_errors=False)
                if manifest.exists():
                    manifest.unlink()
                if had_database:
                    os.replace(old_database, database)
                if had_runs:
                    os.replace(old_runs, runs)
                if had_manifest:
                    os.replace(old_manifest, manifest)
            else:
                raise WorkspaceSchemaMismatch(
                    "pending restore cannot be recovered safely; preserve workspace and rollback artifacts",
                )
            if temporary.exists():
                shutil.rmtree(temporary, ignore_errors=True)
            marker_path.unlink(missing_ok=True)
            return True
        finally:
            lease.close()

    def _backup_bundle_locked(self, source: Path, target: Path) -> WorkspaceBundle:
        """Create a bundle while the caller holds the lifecycle lease."""
        if target == self.home or target.is_relative_to(self.home / "runs"):
            raise ValueError("bundle destination must not be the workspace or runs directory")
        if target.exists():
            if target.is_symlink() or not target.is_dir() or any(target.iterdir()):
                raise FileExistsError(f"bundle destination is not empty: {target}")
            target.rmdir()
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.parent / f".{target.name}.tmp-{uuid.uuid4().hex}"
        temporary.mkdir(parents=True, exist_ok=False, mode=0o700)
        entries: list[dict[str, Any]] = []
        try:
            staged_database = temporary / WORKSPACE_DATABASE_NAME
            _sqlite_backup(source, staged_database)
            _remove_sqlite_sidecars(staged_database)
            if os.name == "posix":
                os.chmod(staged_database, 0o600)
            self._validate_sqlite_snapshot(staged_database, require_workspace=True)
            entries.append(self._bundle_entry(temporary, staged_database, "sqlite"))

            runs = self.home / "runs"
            if runs.exists():
                if runs.is_symlink() or not runs.is_dir():
                    raise WorkspaceSchemaMismatch("runs directory must be a regular directory")
                for path in sorted(runs.rglob("*")):
                    if path.is_symlink():
                        raise WorkspaceSchemaMismatch(
                            f"run evidence contains a symlink: {path.relative_to(self.home)}",
                        )
                    if path.is_dir():
                        continue
                    if _is_sqlite_sidecar(path):
                        continue
                    if not path.is_file():
                        raise WorkspaceSchemaMismatch(
                            f"run evidence contains a special file: {path.relative_to(self.home)}",
                        )
                    relative = path.relative_to(self.home)
                    destination = temporary / relative
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    kind = "sqlite" if path.suffix.lower() == ".db" else "file"
                    if kind == "sqlite":
                        _sqlite_backup(path, destination)
                        _remove_sqlite_sidecars(destination)
                        self._validate_sqlite_snapshot(destination, require_workspace=False)
                    else:
                        shutil.copy2(path, destination)
                    if os.name == "posix":
                        os.chmod(destination, 0o600)
                    entries.append(self._bundle_entry(temporary, destination, kind))

            installation_manifest = self.home / ".installation.json"
            if installation_manifest.is_symlink():
                raise WorkspaceSchemaMismatch("installation manifest must not be a symlink")
            if installation_manifest.exists() and not installation_manifest.is_file():
                raise WorkspaceSchemaMismatch("installation manifest must be a regular file")
            if installation_manifest.is_file():
                try:
                    installation_payload = json.loads(
                        installation_manifest.read_text(encoding="utf-8"),
                    )
                except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise WorkspaceSchemaMismatch("installation manifest is invalid") from exc
                if not isinstance(installation_payload, Mapping):
                    raise WorkspaceSchemaMismatch("installation manifest is invalid")
                destination = temporary / ".installation.json"
                shutil.copy2(installation_manifest, destination)
                if os.name == "posix":
                    os.chmod(destination, 0o600)
                entries.append(self._bundle_entry(temporary, destination, "manifest"))

            if os.name == "posix":
                for directory in (temporary, *[p for p in temporary.rglob("*") if p.is_dir()]):
                    os.chmod(directory, 0o700)

            manifest = {
                "format": "lipas-workspace-bundle",
                "format_version": 1,
                "schema_version": WORKSPACE_SCHEMA_VERSION,
                "created_at": time.time(),
                "source_home": str(self.home),
                "files": entries,
            }
            (temporary / "manifest.json").write_text(
                json.dumps(manifest, indent=2, sort_keys=True, allow_nan=False) + "\n",
                encoding="utf-8",
            )
            if os.name == "posix":
                os.chmod(temporary, 0o700)
                os.chmod(temporary / "manifest.json", 0o600)
            temporary.replace(target)
            return WorkspaceBundle(
                source,
                target,
                None,
                False,
                tuple(entry["path"] for entry in entries),
            )
        finally:
            if temporary.exists():
                shutil.rmtree(temporary, ignore_errors=True)

    @staticmethod
    def _bundle_entry(root: Path, path: Path, kind: str) -> dict[str, Any]:
        size = 0
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                size += len(chunk)
        schema_version: int | None = None
        if kind == "sqlite":
            with contextlib.closing(sqlite3.connect(_immutable_read_only_uri(path), uri=True)) as conn:
                table = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='runtime_meta'",
                ).fetchone()
                if table is not None:
                    row = conn.execute(
                        "SELECT value FROM runtime_meta WHERE key='schema_version'",
                    ).fetchone()
                    if row is not None:
                        with contextlib.suppress(TypeError, ValueError, OverflowError):
                            schema_version = int(row[0])
        return {
            "path": path.relative_to(root).as_posix(),
            "kind": kind,
            "size": size,
            "sha256": _sha256_file(path),
            "schema_version": schema_version,
        }

    @staticmethod
    def _validate_sqlite_snapshot(path: Path, *, require_workspace: bool) -> None:
        try:
            with contextlib.closing(sqlite3.connect(_immutable_read_only_uri(path), uri=True)) as conn:
                integrity = conn.execute("PRAGMA integrity_check").fetchone()
                if integrity is None or integrity[0] != "ok":
                    raise WorkspaceSchemaMismatch(f"SQLite integrity check failed: {path.name}")
                if require_workspace:
                    row = conn.execute(
                        "SELECT value FROM runtime_meta WHERE key='schema_version'",
                    ).fetchone()
                    if row is None or int(row[0]) != WORKSPACE_SCHEMA_VERSION:
                        raise WorkspaceSchemaMismatch("bundle workspace schema does not match this release")
        except (sqlite3.DatabaseError, TypeError, ValueError, OverflowError) as exc:
            if isinstance(exc, WorkspaceSchemaMismatch):
                raise
            raise WorkspaceSchemaMismatch(f"cannot validate SQLite bundle file: {path.name}") from exc

    @classmethod
    def _verify_bundle(cls, bundle: Path) -> tuple[dict[str, Any], ...]:
        if not bundle.is_dir() or bundle.is_symlink():
            raise ValueError("bundle source must be a regular directory")
        manifest_path = bundle / "manifest.json"
        if not manifest_path.is_file() or manifest_path.is_symlink():
            raise WorkspaceSchemaMismatch("bundle manifest.json is missing")
        if os.name == "posix":
            for directory in (bundle, *[path for path in bundle.rglob("*") if path.is_dir()]):
                if stat.S_IMODE(directory.stat().st_mode) & 0o077:
                    raise WorkspaceSchemaMismatch(
                        f"workspace bundle directory permissions are too broad: {directory.name}",
                    )
            if stat.S_IMODE(manifest_path.stat().st_mode) & 0o077:
                raise WorkspaceSchemaMismatch(
                    "workspace bundle manifest permissions are too broad",
                )
        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise WorkspaceSchemaMismatch("bundle manifest.json is invalid") from exc
        if not isinstance(payload, Mapping) or payload.get("format") != "lipas-workspace-bundle":
            raise WorkspaceSchemaMismatch("unsupported workspace bundle format")
        if payload.get("format_version") != 1 or payload.get("schema_version") != WORKSPACE_SCHEMA_VERSION:
            raise WorkspaceSchemaMismatch("workspace bundle schema does not match this release")
        raw_entries = payload.get("files")
        if not isinstance(raw_entries, list) or not raw_entries:
            raise WorkspaceSchemaMismatch("workspace bundle has no files")
        entries: list[dict[str, Any]] = []
        seen: set[str] = set()
        for raw in raw_entries:
            if not isinstance(raw, Mapping):
                raise WorkspaceSchemaMismatch("workspace bundle file entry is invalid")
            raw_path = raw.get("path")
            if not isinstance(raw_path, str) or not raw_path or "\\" in raw_path:
                raise WorkspaceSchemaMismatch("workspace bundle path is invalid")
            relative = PurePosixPath(raw_path)
            if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
                raise WorkspaceSchemaMismatch("workspace bundle path must be relative")
            normalized = relative.as_posix()
            if normalized != raw_path or normalized in seen:
                raise WorkspaceSchemaMismatch("workspace bundle contains duplicate paths")
            if (
                normalized != WORKSPACE_DATABASE_NAME
                and normalized != ".installation.json"
                and not normalized.startswith("runs/")
            ):
                raise WorkspaceSchemaMismatch("workspace bundle path is outside workspace data")
            kind = raw.get("kind")
            if kind not in {"file", "sqlite", "manifest"}:
                raise WorkspaceSchemaMismatch("workspace bundle file kind is invalid")
            if normalized == ".installation.json" and kind != "manifest":
                raise WorkspaceSchemaMismatch("installation manifest bundle entry is invalid")
            size = raw.get("size")
            digest = raw.get("sha256")
            if isinstance(size, bool) or not isinstance(size, int) or size < 0:
                raise WorkspaceSchemaMismatch("workspace bundle file size is invalid")
            if not isinstance(digest, str) or len(digest) != 64:
                raise WorkspaceSchemaMismatch("workspace bundle file hash is invalid")
            entry_schema = raw.get("schema_version")
            if entry_schema is not None and (
                isinstance(entry_schema, bool)
                or not isinstance(entry_schema, int)
                or entry_schema < 1
            ):
                raise WorkspaceSchemaMismatch("workspace bundle file schema is invalid")
            path = bundle / Path(normalized)
            if not path.is_file() or path.is_symlink():
                raise WorkspaceSchemaMismatch(f"workspace bundle file is missing: {normalized}")
            file_stat = path.stat()
            if os.name == "posix" and stat.S_IMODE(file_stat.st_mode) & 0o077:
                raise WorkspaceSchemaMismatch(
                    f"workspace bundle file permissions are too broad: {normalized}",
                )
            if file_stat.st_size != size:
                raise WorkspaceSchemaMismatch(f"workspace bundle file size mismatch: {normalized}")
            actual = _sha256_file(path)
            if actual != digest:
                raise WorkspaceSchemaMismatch(f"workspace bundle file hash mismatch: {normalized}")
            if normalized == WORKSPACE_DATABASE_NAME:
                if kind != "sqlite":
                    raise WorkspaceSchemaMismatch("workspace.db bundle entry must be SQLite")
                if entry_schema != WORKSPACE_SCHEMA_VERSION:
                    raise WorkspaceSchemaMismatch("workspace.db bundle schema is invalid")
                cls._validate_sqlite_snapshot(path, require_workspace=True)
            elif kind == "sqlite":
                cls._validate_sqlite_snapshot(path, require_workspace=False)
            seen.add(normalized)
            entries.append(dict(raw))
        if WORKSPACE_DATABASE_NAME not in seen:
            raise WorkspaceSchemaMismatch("workspace bundle has no workspace.db")
        for path in bundle.rglob("*"):
            if path.is_symlink():
                raise WorkspaceSchemaMismatch("workspace bundle contains a symlink")
            if path.is_file() and path.relative_to(bundle).as_posix() != "manifest.json":
                if path.relative_to(bundle).as_posix() not in seen:
                    raise WorkspaceSchemaMismatch("workspace bundle contains an unlisted file")
            if not path.is_file() and not path.is_dir():
                raise WorkspaceSchemaMismatch("workspace bundle contains a special file")
        return tuple(entries)

    def restore(self, source: str | Path, *, keep_backup: bool = True) -> WorkspaceBackup:
        """Restore a validated backup while fencing active Runtime processes."""
        if not isinstance(keep_backup, bool):
            raise TypeError("keep_backup must be bool")
        raw_source = Path(source).expanduser()
        if raw_source.is_symlink():
            raise ValueError("restore source must not be a symlink")
        source = raw_source.resolve()
        if not source.is_file():
            raise FileNotFoundError(source)
        if source == self.database_path or (
            self.database_path.exists()
            and os.path.samefile(source, self.database_path)
        ):
            # Restoring a workspace from itself is ambiguous in WAL mode and
            # would replace the file with a snapshot of the same live state.
            # Callers should use ``backup`` for a no-op snapshot or provide a
            # distinct, validated backup path for an actual restore.
            raise ValueError("restore source must differ from workspace.db")
        lease = self.acquire_runtime_lease(exclusive=True)
        try:
            self.home.mkdir(parents=True, exist_ok=True)
            backup_path: Path | None = None
            if self.database_path.exists() and keep_backup:
                backup_path = self.home / f"workspace.db.pre-restore-{int(time.time())}-{uuid.uuid4().hex[:8]}"
                # A raw copy can omit committed pages still resident in the
                # source WAL.  Use the same online-backup API as the primary
                # restore path so the rollback artifact is itself valid.
                _sqlite_backup(self.database_path, backup_path)
            temporary = self.home / f".workspace.db.restore-{uuid.uuid4().hex}"
            # Copy through SQLite's online-backup API rather than a raw file
            # copy.  A source database may be in WAL mode or be written by a
            # separate backup process; the API takes a consistent snapshot
            # while retaining the same schema/integrity checks used below.
            try:
                with sqlite3.connect(_read_only_uri(source), uri=True) as source_conn:
                    row = source_conn.execute(
                        "SELECT value FROM runtime_meta WHERE key='schema_version'",
                    ).fetchone()
                    try:
                        version = None if row is None else int(row[0])
                    except (TypeError, ValueError, OverflowError) as exc:
                        raise WorkspaceSchemaMismatch(
                            "backup schema version is malformed",
                        ) from exc
                    if version != WORKSPACE_SCHEMA_VERSION:
                        raise WorkspaceSchemaMismatch(
                            "backup schema does not match this release",
                        )
                    integrity = source_conn.execute("PRAGMA integrity_check").fetchone()
                    if integrity is None or integrity[0] != "ok":
                        raise WorkspaceSchemaMismatch(
                            "backup failed SQLite integrity_check",
                        )
                    with sqlite3.connect(temporary) as target_conn:
                        source_conn.backup(target_conn)
            except sqlite3.DatabaseError as exc:
                raise WorkspaceSchemaMismatch(
                    f"cannot read workspace backup: {exc}",
                ) from exc
            temporary.replace(self.database_path)
            # A previous database may have left sidecars behind after an
            # unclean stop.  They belong to the replaced file and must not be
            # mistaken for WAL pages of the restored snapshot.
            for suffix in ("-wal", "-shm"):
                stale_sidecar = Path(str(self.database_path) + suffix)
                try:
                    stale_sidecar.unlink()
                except FileNotFoundError:
                    pass
        finally:
            lease.close()
            if 'temporary' in locals() and temporary.exists():
                temporary.unlink()
            if 'temporary' in locals():
                for suffix in ("-wal", "-shm"):
                    Path(str(temporary) + suffix).unlink(missing_ok=True)
        return WorkspaceBackup(self.database_path, backup_path, True)

    @property
    def legacy_files(self) -> tuple[Path, ...]:
        return tuple(
            self.home / name
            for name, _ in _LEGACY_DATABASES
            if (self.home / name).is_file()
        )

    def acquire_runtime_lease(self, *, exclusive: bool = False) -> _WorkspaceLease:
        """Fence migration/rollback against first-party active Runtimes."""
        self.home.mkdir(parents=True, exist_ok=True)
        lock_path = self.home / _RUNTIME_LOCK_NAME
        file_descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        if _fcntl is None:
            return _WorkspaceLease(file_descriptor)
        operation = _fcntl.LOCK_EX if exclusive else _fcntl.LOCK_SH
        try:
            _fcntl.flock(file_descriptor, operation | _fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(file_descriptor)
            action = "maintenance" if exclusive else "runtime startup"
            raise WorkspaceMigrationRequired(
                f"workspace is busy; stop active Runtime/worker processes before {action}",
            ) from exc
        except BaseException:
            os.close(file_descriptor)
            raise
        return _WorkspaceLease(file_descriptor)

    def _migration_lock_issue(self) -> RuntimeStorageIssue | None:
        lock_path = self.home / _MIGRATION_LOCK_NAME
        try:
            raw = lock_path.read_text(encoding="ascii")
            stat = lock_path.stat()
        except FileNotFoundError:
            return None
        except OSError as exc:
            return RuntimeStorageIssue(
                "migration_lock_unreadable",
                f"cannot inspect migration lock {lock_path}: {exc}",
                severity="warning",
            )
        pid: int | None = None
        for line in raw.splitlines():
            key, separator, value = line.partition("=")
            if separator and key.strip() == "pid":
                try:
                    pid = int(value.strip())
                except ValueError:
                    pid = None
                break
        if pid is not None and _pid_is_running(pid):
            return RuntimeStorageIssue(
                "active_migration_lock",
                f"migration lock is owned by active pid {pid}",
                severity="warning",
                context={"pid": pid, "path": str(lock_path)},
            )
        age_seconds = max(0.0, time.time() - stat.st_mtime)
        if pid is not None or age_seconds >= _MALFORMED_LOCK_GRACE_SECONDS:
            return RuntimeStorageIssue(
                "stale_migration_lock",
                (
                    f"migration lock belongs to inactive pid {pid}"
                    if pid is not None
                    else "migration lock is malformed and stale"
                ),
                severity="warning",
                context={
                    "pid": pid,
                    "path": str(lock_path),
                    "age_seconds": age_seconds,
                },
            )
        return RuntimeStorageIssue(
            "migration_lock_initializing",
            "migration lock has no valid pid yet and is too new to recover safely",
            severity="warning",
            context={"path": str(lock_path), "age_seconds": age_seconds},
        )

    @contextlib.contextmanager
    def _migration_lock(self) -> Iterator[None]:
        lock_path = self.home / _MIGRATION_LOCK_NAME
        file_descriptor: int | None = None
        for _ in range(2):
            try:
                file_descriptor = os.open(
                    lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600,
                )
                break
            except FileExistsError as exc:
                try:
                    previous = lock_path.stat()
                except FileNotFoundError:
                    continue
                issue = self._migration_lock_issue()
                if issue is None:
                    continue
                if issue.code != "stale_migration_lock":
                    raise WorkspaceMigrationRequired(issue.message) from exc
                try:
                    current = lock_path.stat()
                except FileNotFoundError:
                    continue
                if (current.st_dev, current.st_ino) != (
                    previous.st_dev, previous.st_ino,
                ):
                    continue
                lock_path.unlink()
        if file_descriptor is None:
            raise WorkspaceMigrationRequired(
                f"could not acquire migration lock: {lock_path}",
            )
        lock_stat = os.fstat(file_descriptor)
        try:
            os.write(file_descriptor, f"pid={os.getpid()}\n".encode("ascii"))
            yield
        finally:
            os.close(file_descriptor)
            try:
                current = lock_path.stat()
            except FileNotFoundError:
                pass
            else:
                if (current.st_dev, current.st_ino) == (
                    lock_stat.st_dev, lock_stat.st_ino,
                ):
                    lock_path.unlink()

    def inspect(self) -> WorkspaceStatus:
        legacy = self.legacy_files
        lock_issue = self._migration_lock_issue()
        lock_issues = () if lock_issue is None else (lock_issue,)
        pending_restore = self.home / _RESTORE_MARKER_NAME
        pending_issues: tuple[RuntimeStorageIssue, ...] = ()
        if pending_restore.exists():
            pending_issues = (RuntimeStorageIssue(
                "pending_restore",
                "workspace has an unsettled bundle restore marker; start Runtime or run recovery",
            ),)
        layout_issues: list[RuntimeStorageIssue] = []
        if self.database_path.is_symlink():
            layout_issues.append(RuntimeStorageIssue(
                "workspace_symlink",
                "workspace.db must not be a symlink",
            ))
        runs_root = self.home / "runs"
        if runs_root.is_symlink():
            layout_issues.append(RuntimeStorageIssue(
                "runs_symlink",
                "runs directory must not be a symlink",
            ))
        installation_manifest = self.home / ".installation.json"
        if installation_manifest.is_symlink():
            layout_issues.append(RuntimeStorageIssue(
                "manifest_symlink",
                "installation manifest must not be a symlink",
            ))
        if not self.database_path.exists():
            early_issues = lock_issues + pending_issues + tuple(layout_issues)
            return WorkspaceStatus(
                home=self.home,
                database_path=self.database_path,
                state=(
                    "invalid"
                    if any(issue.severity == "error" for issue in early_issues)
                    else ("migration_required" if legacy else "uninitialized")
                ),
                schema_version=None,
                legacy_files=legacy,
                issues=early_issues,
            )
        issues: list[RuntimeStorageIssue] = list(lock_issues + pending_issues)
        issues.extend(layout_issues)
        if self.database_path.is_symlink():
            return WorkspaceStatus(
                home=self.home,
                database_path=self.database_path,
                state="invalid",
                schema_version=None,
                legacy_files=legacy,
                issues=tuple(issues),
            )
        version: int | None = None
        try:
            with contextlib.closing(
                sqlite3.connect(self.database_path),
            ) as connection:
                # A schema stamp alone does not prove that pages are usable.
                # Opening a corrupted workspace as "current" would let
                # component constructors fail halfway through Runtime startup
                # and could strand a lifecycle lease.  Inspect is a
                # fail-closed preflight, so run SQLite's bounded quick check
                # before declaring the workspace current.
                quick = connection.execute("PRAGMA quick_check").fetchone()
                if quick is None or quick[0] != "ok":
                    issues.append(RuntimeStorageIssue(
                        "sqlite_integrity_failed",
                        f"SQLite quick_check failed: {quick!r}",
                    ))
                table = connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' "
                    "AND name='runtime_meta'",
                ).fetchone()
                if table is None:
                    issues.append(RuntimeStorageIssue(
                        "missing_runtime_meta",
                        "workspace.db has no runtime_meta table",
                    ))
                else:
                    row = connection.execute(
                        "SELECT value FROM runtime_meta WHERE key='schema_version'",
                    ).fetchone()
                    if row is None:
                        issues.append(RuntimeStorageIssue(
                            "missing_schema_version",
                            "runtime_meta has no schema_version",
                        ))
                    else:
                        try:
                            version = int(row[0])
                        except (TypeError, ValueError):
                            issues.append(RuntimeStorageIssue(
                                "invalid_schema_version",
                                f"workspace schema version is not an int: {row[0]!r}",
                            ))
                        if version is not None and version != WORKSPACE_SCHEMA_VERSION:
                            issues.append(RuntimeStorageIssue(
                                "unsupported_schema_version",
                                f"workspace schema is {version}; this release supports "
                                f"{WORKSPACE_SCHEMA_VERSION}",
                            ))
        except sqlite3.DatabaseError as exc:
            issues.append(RuntimeStorageIssue(
                "sqlite_open_failed", f"cannot inspect workspace.db: {exc}",
            ))
        return WorkspaceStatus(
            home=self.home,
            database_path=self.database_path,
            state=(
                "invalid"
                if any(issue.severity == "error" for issue in issues)
                else "current"
            ),
            schema_version=version,
            legacy_files=legacy,
            issues=tuple(issues),
        )

    def require_current(self, *, create: bool = False) -> Path:
        status = self.inspect()
        if status.current:
            return self.database_path
        if status.state == "uninitialized" and create:
            # Direct callers (for example a CLI bootstrap) do not otherwise
            # hold the Runtime lifecycle fence.  Re-check while holding an
            # exclusive lease so two first opens cannot both observe an
            # empty directory and race through schema creation.  Runtime's
            # own constructor acquires this same lease before initialization;
            # it performs the bootstrap under that existing lease and then
            # reaches the current fast path above.
            bootstrap_lease: _WorkspaceLease | None = None
            # Another first opener may be between its atomic bootstrap
            # publish and component construction.  Unlike normal Runtime
            # startup (which should fail fast when a current workspace is
            # busy), an uninitialized workspace has no legitimate shared
            # owner, so boundedly wait for that initializer to finish.
            for attempt in range(100):
                try:
                    bootstrap_lease = self.acquire_runtime_lease(exclusive=True)
                    break
                except WorkspaceMigrationRequired:
                    if attempt == 99:
                        raise
                    time.sleep(0.05)
            assert bootstrap_lease is not None
            with bootstrap_lease:
                status = self.inspect()
                if status.current:
                    return self.database_path
                if status.state != "uninitialized":
                    if status.migration_required:
                        names = ", ".join(path.name for path in status.legacy_files)
                        raise WorkspaceMigrationRequired(
                            f"legacy LIPAS state detected ({names}); run "
                            f"`lipas migrate plan --home {self.home}` then "
                            "`lipas migrate apply --yes`",
                        )
                    detail = "; ".join(issue.message for issue in status.issues)
                    raise WorkspaceSchemaMismatch(
                        detail or "workspace is not initialized",
                    )
                self.home.mkdir(parents=True, exist_ok=True)
                self._initialize_database(self.database_path, migrated_from=None)
            return self.database_path
        if status.migration_required:
            names = ", ".join(path.name for path in status.legacy_files)
            raise WorkspaceMigrationRequired(
                f"legacy LIPAS state detected ({names}); run "
                f"`lipas migrate plan --home {self.home}` then "
                "`lipas migrate apply --yes`"
            )
        detail = "; ".join(issue.message for issue in status.issues)
        raise WorkspaceSchemaMismatch(detail or "workspace is not initialized")

    def plan_migration(self) -> WorkspaceMigrationPlan:
        status = self.inspect()
        if status.current:
            return WorkspaceMigrationPlan(
                self.home, self.database_path, status.legacy_files, {}, False,
            )
        issues = list(status.issues)
        if status.state == "invalid":
            issues.append(RuntimeStorageIssue(
                "target_already_exists",
                "an invalid workspace.db already exists; move it aside before migration",
            ))
        table_rows: dict[str, int] = {}
        for path in status.legacy_files:
            tables = dict(_LEGACY_DATABASES)[path.name]
            try:
                with contextlib.closing(
                    sqlite3.connect(_read_only_uri(path), uri=True),
                ) as source:
                    quick = source.execute("PRAGMA quick_check").fetchone()
                    if quick is None or quick[0] != "ok":
                        issues.append(RuntimeStorageIssue(
                            "legacy_integrity_failed",
                            f"{path.name} failed SQLite quick_check: {quick!r}",
                        ))
                    for table in tables:
                        exists = source.execute(
                            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                            (table,),
                        ).fetchone()
                        if exists is None:
                            continue
                        count = int(source.execute(
                            f"SELECT COUNT(*) FROM {_quote_identifier(table)}",
                        ).fetchone()[0])
                        table_rows[f"{path.name}:{table}"] = count
            except sqlite3.DatabaseError as exc:
                issues.append(RuntimeStorageIssue(
                    "legacy_open_failed", f"cannot inspect {path.name}: {exc}",
                ))
        if not status.legacy_files and status.state != "invalid":
            issues.append(RuntimeStorageIssue(
                "no_legacy_state",
                "no legacy databases were found",
                severity="info",
            ))
        return WorkspaceMigrationPlan(
            home=self.home,
            database_path=self.database_path,
            legacy_files=status.legacy_files,
            table_rows=table_rows,
            required=bool(status.legacy_files) and not self.database_path.exists(),
            issues=tuple(issues),
        )

    def migrate(self) -> WorkspaceMigrationResult:
        plan = self.plan_migration()
        if not plan.required:
            if self.inspect().current:
                return WorkspaceMigrationResult(
                    self.database_path, None, {}, already_current=True,
                )
            raise WorkspaceMigrationRequired("there is no applicable legacy migration")
        if not plan.can_apply:
            detail = "; ".join(issue.message for issue in plan.issues)
            raise WorkspaceMigrationRequired(detail)

        self.home.mkdir(parents=True, exist_ok=True)
        temporary = self.home / f".{WORKSPACE_DATABASE_NAME}.migrating-{uuid.uuid4().hex}"
        backup: Path | None = None
        try:
            with self._migration_lock(), self.acquire_runtime_lease(exclusive=True):
                # A competing migrator may have completed after our initial
                # read but before this process acquired the lock. Re-plan
                # under the exclusive fence before activating any target.
                plan = self.plan_migration()
                if not plan.required:
                    if self.inspect().current:
                        return WorkspaceMigrationResult(
                            self.database_path, None, {}, already_current=True,
                        )
                    raise WorkspaceMigrationRequired(
                        "there is no applicable legacy migration",
                    )
                if not plan.can_apply:
                    detail = "; ".join(issue.message for issue in plan.issues)
                    raise WorkspaceMigrationRequired(detail)
                backup = self._backup_legacy(plan)
                self._initialize_database(temporary, migrated_from="legacy-v1")
                self._bootstrap_component_schemas(temporary)
                copied = self._copy_legacy_databases(backup, temporary)
                issues = self._audit_path(temporary)
                errors = [issue for issue in issues if issue.severity == "error"]
                if errors:
                    raise WorkspaceSchemaMismatch(
                        "; ".join(issue.message for issue in errors),
                    )
                for key, expected in plan.table_rows.items():
                    actual = copied.get(key)
                    if actual != expected:
                        raise WorkspaceSchemaMismatch(
                            f"migration count mismatch for {key}: {actual} != {expected}",
                        )
                os.replace(temporary, self.database_path)
                assert backup is not None
                manifest = backup / "manifest.json"
                payload = json.loads(manifest.read_text(encoding="utf-8"))
                payload["completed_at"] = time.time()
                payload["target"] = str(self.database_path)
                payload["table_rows"] = copied
                manifest.write_text(
                    json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8",
                )
                return WorkspaceMigrationResult(self.database_path, backup, copied)
        finally:
            temporary.unlink(missing_ok=True)

    def rollback(self) -> Path:
        self.home.mkdir(parents=True, exist_ok=True)
        with self._migration_lock(), self.acquire_runtime_lease(exclusive=True):
            status = self.inspect()
            if not status.current:
                raise WorkspaceSchemaMismatch(
                    "only a current workspace can be rolled back",
                )
            if not status.legacy_files:
                raise WorkspaceMigrationRequired(
                    "legacy databases are not available; rollback would lose state",
                )
            destination = self._new_backup_directory("rollback-v2")
            destination.mkdir(parents=True, exist_ok=False)
            preserved = destination / WORKSPACE_DATABASE_NAME
            readme = destination / "README.txt"
            deactivated = False
            try:
                self._backup_current_database(preserved)
                readme.write_text(
                    "This workspace.db contains all v2 writes made before rollback.\n"
                    "Legacy v1 files in the workspace root were not modified.\n",
                    encoding="utf-8",
                )
                self.database_path.unlink()
                deactivated = True
                Path(f"{self.database_path}-wal").unlink(missing_ok=True)
                Path(f"{self.database_path}-shm").unlink(missing_ok=True)
                return preserved
            except BaseException:
                if not deactivated:
                    preserved.unlink(missing_ok=True)
                    readme.unlink(missing_ok=True)
                    with contextlib.suppress(OSError):
                        destination.rmdir()
                raise

    def audit(self) -> tuple[RuntimeStorageIssue, ...]:
        status = self.inspect()
        if not status.current:
            return status.issues or (RuntimeStorageIssue(
                "workspace_not_current",
                f"workspace state is {status.state}",
            ),)
        return (*status.issues, *self._audit_path(self.database_path))

    def _backup_current_database(self, destination: Path) -> None:
        """Checkpoint and preserve a transactionally consistent v2 database."""
        source = sqlite3.connect(
            self.database_path, timeout=0.0, isolation_level=None,
        )
        try:
            source.execute("PRAGMA busy_timeout = 0")
            checkpoint = source.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            if checkpoint is not None and int(checkpoint[0]) != 0:
                raise WorkspaceMigrationRequired(
                    "workspace WAL is busy; stop active writers before rollback",
                )
            source.execute("BEGIN EXCLUSIVE")
            quick = source.execute("PRAGMA quick_check").fetchall()
            if quick != [("ok",)]:
                raise WorkspaceSchemaMismatch(
                    f"cannot preserve corrupt workspace database: {quick!r}",
                )
            source.rollback()
            with contextlib.closing(sqlite3.connect(destination)) as target:
                source.backup(target)
        except sqlite3.OperationalError as exc:
            if source.in_transaction:
                source.rollback()
            raise WorkspaceMigrationRequired(
                "workspace database is busy; stop active workers and Runtime "
                "instances before rollback",
            ) from exc
        except BaseException:
            if source.in_transaction:
                source.rollback()
            raise
        finally:
            source.close()
        with contextlib.closing(
            sqlite3.connect(_read_only_uri(destination), uri=True),
        ) as preserved:
            quick = preserved.execute("PRAGMA quick_check").fetchall()
        if quick != [("ok",)]:
            raise WorkspaceSchemaMismatch(
                f"rollback backup failed integrity verification: {quick!r}",
            )

    @staticmethod
    def _initialize_database(path: Path, *, migrated_from: str | None) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Build the tiny metadata database off to the side and atomically
        # publish it.  Direct readers must never observe a file that exists
        # but has not committed ``runtime_meta`` yet; that intermediate state
        # otherwise looks like corruption to a competing first opener.
        temporary = path.with_name(f".{path.name}.init-{uuid.uuid4().hex}")
        try:
            with contextlib.closing(sqlite3.connect(temporary)) as connection:
                with connection:
                    connection.executescript(_RUNTIME_SCHEMA)
                    values = {
                        "schema_version": str(WORKSPACE_SCHEMA_VERSION),
                        "created_at": repr(time.time()),
                        "layout": "unified-global-with-isolated-run-evidence",
                    }
                    if migrated_from is not None:
                        values["migrated_from"] = migrated_from
                    connection.executemany(
                        "INSERT OR IGNORE INTO runtime_meta(key,value) VALUES(?,?)",
                        tuple(values.items()),
                    )
                    row = connection.execute(
                        "SELECT value FROM runtime_meta WHERE key='schema_version'",
                    ).fetchone()
                    try:
                        version = int(row[0]) if row is not None else -1
                    except (TypeError, ValueError, OverflowError) as exc:
                        raise WorkspaceSchemaMismatch(
                            "workspace schema version is malformed during bootstrap",
                        ) from exc
                    if version != WORKSPACE_SCHEMA_VERSION:
                        raise WorkspaceSchemaMismatch(
                            "workspace schema version does not match this release",
                        )
            # A crashed pre-atomic bootstrap may have left sidecars beside a
            # now-missing main file.  They belong to that abandoned inode and
            # must not be attached to the newly published snapshot.
            for suffix in ("-wal", "-shm"):
                Path(str(path) + suffix).unlink(missing_ok=True)
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
            for suffix in ("-wal", "-shm"):
                Path(str(temporary) + suffix).unlink(missing_ok=True)

    def _bootstrap_component_schemas(self, path: Path) -> None:
        # Local imports avoid making low-level stores depend on this composition
        # module and keep legacy direct constructors available.
        from .conversation_store import SQLiteSessionStore
        from .operations import OperationJournal
        from .orchestration import Mailbox
        from .session import open_session
        from .workbench import Workbench

        claims = None
        workbench = None
        operations = None
        mailbox = None
        conversations = None

        def resources() -> tuple[Any, ...]:
            claims_store = None if claims is None else claims.store
            return conversations, mailbox, operations, workbench, claims_store

        try:
            claims = open_session(path)
            workbench = Workbench(
                self.home, rowset=claims, sandbox="local", database_path=path,
            )
            operations = OperationJournal(path, rowset=claims)
            mailbox = Mailbox(path, rowset=claims)
            conversations = SQLiteSessionStore(path)
        except BaseException:
            _close_all(resources())
            raise
        cleanup_error = _close_all(resources())
        if cleanup_error is not None:
            raise cleanup_error

    def _backup_legacy(self, plan: WorkspaceMigrationPlan) -> Path:
        backup = self._new_backup_directory("migration-v1")
        backup.mkdir(parents=True, exist_ok=False)
        for source_path in plan.legacy_files:
            destination_path = backup / source_path.name
            with (
                contextlib.closing(
                    sqlite3.connect(_read_only_uri(source_path), uri=True),
                ) as source,
                contextlib.closing(
                    sqlite3.connect(destination_path),
                ) as destination,
            ):
                source.backup(destination)
        (backup / "manifest.json").write_text(
            json.dumps({
                "created_at": time.time(),
                "source_home": str(self.home),
                "schema_from": "legacy-v1",
                "schema_to": WORKSPACE_SCHEMA_VERSION,
                "files": [path.name for path in plan.legacy_files],
                "planned_table_rows": dict(plan.table_rows),
            }, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        return backup

    def _new_backup_directory(self, prefix: str) -> Path:
        stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
        return self.home / "backups" / f"{prefix}-{stamp}-{uuid.uuid4().hex[:8]}"

    @staticmethod
    def _copy_legacy_databases(backup: Path, target: Path) -> dict[str, int]:
        copied: dict[str, int] = {}
        connection = sqlite3.connect(target)
        try:
            connection.execute("PRAGMA foreign_keys = OFF")
            connection.execute("BEGIN IMMEDIATE")
            for name, tables in _LEGACY_DATABASES:
                source_path = backup / name
                if not source_path.is_file():
                    continue
                source = sqlite3.connect(_read_only_uri(source_path), uri=True)
                try:
                    for table in tables:
                        exists = source.execute(
                            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                            (table,),
                        ).fetchone()
                        if exists is None:
                            continue
                        columns = tuple(
                            row[1] for row in source.execute(
                                f"PRAGMA table_info({_quote_identifier(table)})",
                            )
                        )
                        if not columns:
                            continue
                        rows = source.execute(
                            f"SELECT * FROM {_quote_identifier(table)}",
                        ).fetchall()
                        names = ",".join(_quote_identifier(value) for value in columns)
                        placeholders = ",".join("?" for _ in columns)
                        verb = "INSERT OR REPLACE" if table.endswith("_meta") or table == "meta" else "INSERT"
                        if rows:
                            connection.executemany(
                                f"{verb} INTO {_quote_identifier(table)} ({names}) "
                                f"VALUES ({placeholders})",
                                rows,
                            )
                        copied[f"{name}:{table}"] = len(rows)
                finally:
                    source.close()
            connection.commit()
            connection.execute("PRAGMA foreign_keys = ON")
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()
        return copied

    def _audit_path(self, path: Path) -> tuple[RuntimeStorageIssue, ...]:
        issues: list[RuntimeStorageIssue] = []
        try:
            with contextlib.closing(
                sqlite3.connect(_read_only_uri(path), uri=True),
            ) as connection:
                quick = connection.execute("PRAGMA quick_check").fetchall()
                if quick != [("ok",)]:
                    issues.append(RuntimeStorageIssue(
                        "sqlite_integrity_failed",
                        f"SQLite quick_check failed: {quick!r}",
                    ))
                foreign = connection.execute("PRAGMA foreign_key_check").fetchall()
                for table, rowid, parent, constraint in foreign:
                    issues.append(RuntimeStorageIssue(
                        "foreign_key_violation",
                        f"{table} row {rowid} references missing {parent}",
                        context={"constraint": constraint},
                    ))
                tables = {
                    row[0] for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'",
                    )
                }
                required = {
                    "runtime_meta", "execution_tasks", "execution_runs",
                    "execution_interrupts", "execution_agent_events",
                    "workbench_run_sessions", "workbench_events",
                    "operations", "claims", "lipas_conversations",
                }
                for missing_table in sorted(required - tables):
                    issues.append(RuntimeStorageIssue(
                        "missing_component_table",
                        f"workspace is missing component table {missing_table}",
                        context={"table": missing_table},
                    ))
                if "execution_agent_events" in tables:
                    for run_id, minimum, maximum, count in connection.execute(
                        "SELECT run_id,MIN(sequence),MAX(sequence),COUNT(*) "
                        "FROM execution_agent_events GROUP BY run_id",
                    ):
                        if minimum != 1 or maximum != count:
                            issues.append(RuntimeStorageIssue(
                                "agent_event_gap",
                                f"run {run_id} has a non-contiguous event cursor",
                                context={
                                    "run_id": run_id, "minimum": minimum,
                                    "maximum": maximum, "count": count,
                                },
                            ))
                if {"execution_runs", "execution_interrupts"} <= tables:
                    invalid = connection.execute(
                        "SELECT i.id,i.run_id,r.state FROM execution_interrupts i "
                        "JOIN execution_runs r ON r.id=i.run_id "
                        "WHERE i.state='pending' AND r.state!='waiting'",
                    ).fetchall()
                    for interrupt_id, run_id, state in invalid:
                        issues.append(RuntimeStorageIssue(
                            "pending_interrupt_state_mismatch",
                            f"pending interrupt {interrupt_id} belongs to {state} run",
                            context={"run_id": run_id},
                        ))
                    waiting_without_interrupt = connection.execute(
                        "SELECT r.id FROM execution_runs r WHERE r.state='waiting' "
                        "AND NOT EXISTS (SELECT 1 FROM execution_interrupts i "
                        "WHERE i.run_id=r.id AND i.state='pending')",
                    ).fetchall()
                    for (run_id,) in waiting_without_interrupt:
                        issues.append(RuntimeStorageIssue(
                            "waiting_run_without_interrupt",
                            f"waiting run {run_id} has no pending interrupt",
                            context={"run_id": run_id},
                        ))
                if "workbench_run_sessions" in tables:
                    for run_id, raw_path in connection.execute(
                        "SELECT run_id,claims_path FROM workbench_run_sessions",
                    ):
                        candidate = (self.home / raw_path).resolve()
                        if not candidate.is_relative_to(self.home):
                            issues.append(RuntimeStorageIssue(
                                "run_evidence_path_escape",
                                f"run {run_id} evidence path escapes the workspace",
                                context={"run_id": run_id, "path": raw_path},
                            ))
        except sqlite3.DatabaseError as exc:
            issues.append(RuntimeStorageIssue(
                "sqlite_audit_failed", f"cannot audit workspace database: {exc}",
            ))
        return tuple(issues)
