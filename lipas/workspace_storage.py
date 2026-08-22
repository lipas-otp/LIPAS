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
import json
import os
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
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
    "WorkspaceStorage",
]


WORKSPACE_DATABASE_NAME = "workspace.db"
WORKSPACE_SCHEMA_VERSION = 2
_MIGRATION_LOCK_NAME = ".migration.lock"
_RUNTIME_LOCK_NAME = ".runtime.lock"
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
        ("lipas_conversation_meta", "lipas_conversations"),
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
        if not self.database_path.exists():
            return WorkspaceStatus(
                home=self.home,
                database_path=self.database_path,
                state="migration_required" if legacy else "uninitialized",
                schema_version=None,
                legacy_files=legacy,
                issues=lock_issues,
            )
        issues: list[RuntimeStorageIssue] = list(lock_issues)
        version: int | None = None
        try:
            with contextlib.closing(
                sqlite3.connect(self.database_path),
            ) as connection:
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
        with contextlib.closing(sqlite3.connect(path)) as connection:
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
                    "INSERT INTO runtime_meta(key,value) VALUES(?,?)",
                    tuple(values.items()),
                )

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
