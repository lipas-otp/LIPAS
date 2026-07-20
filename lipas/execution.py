"""Durable application-level execution state for the LIPAS workbench.

This module deliberately sits above the Claim/Effect runtime.  A Claim records
what happened inside an execution; ``ExecutionStore`` records which user task
owns that execution, who currently owns the run lease, where it can resume,
and whether it is waiting for a durable interrupt such as an approval.

The first implementation is local SQLite and uses conditional transitions plus
lease tokens.  It does not claim distributed exactly-once execution: an
expired worker can be replaced, while stable effect ids and the runtime's
operation reconciliation remain responsible for safe writes.

The execution database is authoritative for control state and carries an
explicit schema version. Each transition also commits a Claim-shaped local
outbox event. Passing a RowSet mirrors those events into the evidence tape;
``repair_audit`` closes a crash window between the two databases.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import math
import sqlite3
import time
import uuid
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Iterator, Mapping

from .calculus import Claim
from .rows import RowSet
from .serialization.store_sqlite import ensure_sqlite_parent
from .serialization.codec import decode, encode, make_default_codec_registry

__all__ = [
    "Checkpoint",
    "CheckpointConflict",
    "ExecutionLeaseError",
    "ExecutionSchemaVersionMismatch",
    "ExecutionStateError",
    "ExecutionStore",
    "Interrupt",
    "InterruptState",
    "Run",
    "RunState",
    "Task",
    "TaskState",
    "RunSuspended",
    "EXECUTION_SCHEMA_VERSION",
    "TAG_EXECUTION_TASK_CREATED",
    "TAG_EXECUTION_TASK_COMPLETED",
    "TAG_EXECUTION_TASK_CANCELLED",
    "TAG_EXECUTION_RUN_CREATED",
    "TAG_EXECUTION_RUN_CLAIMED",
    "TAG_EXECUTION_LEASE_RENEWED",
    "TAG_EXECUTION_CHECKPOINT_SAVED",
    "TAG_EXECUTION_INTERRUPT_REQUESTED",
    "TAG_EXECUTION_INTERRUPT_RESOLVED",
    "TAG_EXECUTION_CANCEL_REQUESTED",
    "TAG_EXECUTION_RUN_COMPLETED",
    "TAG_EXECUTION_RUN_FAILED",
    "TAG_EXECUTION_RUN_CANCELLED",
]


EXECUTION_SCHEMA_VERSION = 1

TAG_EXECUTION_TASK_CREATED = "execution_task_created"
TAG_EXECUTION_TASK_COMPLETED = "execution_task_completed"
TAG_EXECUTION_TASK_CANCELLED = "execution_task_cancelled"
TAG_EXECUTION_RUN_CREATED = "execution_run_created"
TAG_EXECUTION_RUN_CLAIMED = "execution_run_claimed"
TAG_EXECUTION_LEASE_RENEWED = "execution_lease_renewed"
TAG_EXECUTION_CHECKPOINT_SAVED = "execution_checkpoint_saved"
TAG_EXECUTION_INTERRUPT_REQUESTED = "execution_interrupt_requested"
TAG_EXECUTION_INTERRUPT_RESOLVED = "execution_interrupt_resolved"
TAG_EXECUTION_CANCEL_REQUESTED = "execution_cancel_requested"
TAG_EXECUTION_RUN_COMPLETED = "execution_run_completed"
TAG_EXECUTION_RUN_FAILED = "execution_run_failed"
TAG_EXECUTION_RUN_CANCELLED = "execution_run_cancelled"


class TaskState(str, Enum):
    OPEN = "open"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class RunState(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    WAITING = "waiting"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class InterruptState(str, Enum):
    PENDING = "pending"
    ALLOWED = "allowed"
    DENIED = "denied"


class ExecutionStateError(RuntimeError):
    """An execution object cannot make the requested state transition."""


class ExecutionLeaseError(ExecutionStateError):
    """A worker does not own the run's current, unexpired lease."""


class ExecutionSchemaVersionMismatch(ExecutionStateError):
    """An execution database uses a schema this release cannot open."""


class CheckpointConflict(ExecutionStateError):
    """A checkpoint was based on a stale checkpoint version."""


class RunSuspended(ExecutionStateError):
    """Control-flow signal carrying a durably persisted interrupt."""

    def __init__(self, interrupt: "Interrupt") -> None:
        self.interrupt = interrupt
        super().__init__(
            f"run {interrupt.run_id!r} is waiting for "
            f"{interrupt.kind} interrupt {interrupt.id!r}",
        )


@dataclass(frozen=True, slots=True)
class Task:
    id: str
    goal: str
    workspace: str
    state: TaskState
    created_at: float
    updated_at: float


@dataclass(frozen=True, slots=True)
class Run:
    id: str
    task_id: str
    state: RunState
    attempt: int
    checkpoint_version: int
    lease_token: str | None
    lease_expires: float | None
    cancel_requested: bool
    result: Any | None
    error: Mapping[str, Any] | None
    created_at: float
    updated_at: float


@dataclass(frozen=True, slots=True)
class Checkpoint:
    run_id: str
    version: int
    phase: str
    state: Mapping[str, Any]
    created_at: float


@dataclass(frozen=True, slots=True)
class Interrupt:
    id: str
    run_id: str
    kind: str
    request: Mapping[str, Any]
    state: InterruptState
    response: Any | None
    created_at: float
    resolved_at: float | None


_SCHEMA = """
CREATE TABLE IF NOT EXISTS execution_meta (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS execution_tasks (
    id          TEXT PRIMARY KEY,
    goal        TEXT NOT NULL,
    workspace   TEXT NOT NULL,
    state       TEXT NOT NULL CHECK(state IN ('open','completed','cancelled')),
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS execution_runs (
    id                  TEXT PRIMARY KEY,
    task_id             TEXT NOT NULL REFERENCES execution_tasks(id),
    state               TEXT NOT NULL CHECK(state IN
                            ('pending','running','waiting','completed','failed','cancelled')),
    attempt             INTEGER NOT NULL DEFAULT 0,
    checkpoint_version  INTEGER NOT NULL DEFAULT 0,
    lease_token         TEXT,
    lease_expires       REAL,
    cancel_requested    INTEGER NOT NULL DEFAULT 0 CHECK(cancel_requested IN (0,1)),
    result_json         TEXT,
    error_json          TEXT,
    created_at          REAL NOT NULL,
    updated_at          REAL NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_execution_one_active_run
ON execution_runs(task_id)
WHERE state IN ('pending','running','waiting');

CREATE TABLE IF NOT EXISTS execution_checkpoints (
    run_id      TEXT NOT NULL REFERENCES execution_runs(id),
    version     INTEGER NOT NULL,
    phase       TEXT NOT NULL,
    state_json  TEXT NOT NULL,
    created_at  REAL NOT NULL,
    PRIMARY KEY(run_id, version)
);

CREATE TABLE IF NOT EXISTS execution_interrupts (
    id            TEXT PRIMARY KEY,
    run_id        TEXT NOT NULL REFERENCES execution_runs(id),
    kind          TEXT NOT NULL,
    request_json  TEXT NOT NULL,
    state         TEXT NOT NULL CHECK(state IN ('pending','allowed','denied')),
    response_json TEXT,
    created_at    REAL NOT NULL,
    resolved_at   REAL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_execution_one_pending_interrupt
ON execution_interrupts(run_id)
WHERE state = 'pending';

CREATE TABLE IF NOT EXISTS execution_audit_events (
    claim_id     TEXT PRIMARY KEY,
    tag          TEXT NOT NULL,
    fields_json  TEXT NOT NULL,
    created_at   REAL NOT NULL
);
"""


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


_CODECS = make_default_codec_registry()


def _json(value: Any) -> str:
    return json.dumps(
        encode(value, _CODECS),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _from_json(value: str) -> Any:
    return decode(json.loads(value), _CODECS)


def _mapping_json(value: Mapping[str, Any]) -> str:
    return _json(dict(value))


def _positive_duration(value: float, name: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or value <= 0
    ):
        raise ValueError(f"{name} must be a finite positive number")
    return float(value)


def _timestamp(value: float | None) -> float:
    if value is None:
        return time.time()
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
    ):
        raise ValueError("now must be a finite number")
    return float(value)


class ExecutionStore:
    """SQLite task/run/checkpoint store with lease-fenced transitions.

    A worker must call :meth:`claim_run` and retain the returned lease token.
    Checkpoint, suspend, completion, and failure calls require that token.  If
    the process dies, another worker may claim the same run only after the
    lease expires; the changed token fences the stale worker from later writes.

    ``rowset`` is an optional evidence sink. Control decisions always read this
    execution database; mirrored Claims are audit/reporting evidence and are
    repaired from the transactional outbox after an interrupted mirror write.
    """

    def __init__(
        self,
        path: str | Path = ":memory:",
        *,
        rowset: RowSet | None = None,
    ) -> None:
        ensure_sqlite_parent(path)
        self._path = path
        self._conn = sqlite3.connect(path, timeout=5.0)
        self._conn.execute("PRAGMA foreign_keys = ON")
        self.rowset = rowset
        self._audit_cursor = 0
        self._closed = False
        try:
            self._init_schema()
            with self._transaction():
                self._seed_legacy_audit_events()
            self.repair_audit()
        except BaseException:
            self._conn.close()
            self._closed = True
            raise

    def _init_schema(self) -> None:
        """Create schema v1 or reject a database stamped by another version."""
        had_execution_schema = self._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='execution_tasks'",
        ).fetchone() is not None
        with self._conn:
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS execution_meta "
                "(key TEXT PRIMARY KEY,value TEXT NOT NULL)",
            )
        row = self._conn.execute(
            "SELECT value FROM execution_meta WHERE key='schema_version'",
        ).fetchone()
        if row is None:
            # Databases created by the pre-release durable slice had the same
            # v1 tables but no metadata. Adopt them once instead of making
            # development checkpoints unreadable.
            with self._conn:
                self._conn.executemany(
                    "INSERT INTO execution_meta(key,value) VALUES(?,?)",
                    (
                        ("schema_version", str(EXECUTION_SCHEMA_VERSION)),
                        ("created_at", repr(time.time())),
                        ("adopted_legacy_schema", "1" if had_execution_schema else "0"),
                    ),
                )
        else:
            try:
                existing = int(row[0])
            except (TypeError, ValueError) as exc:
                raise ExecutionSchemaVersionMismatch(
                    f"execution schema version is not an int: {row[0]!r}",
                ) from exc
            if existing != EXECUTION_SCHEMA_VERSION:
                raise ExecutionSchemaVersionMismatch(
                    f"execution store at {self._path!r} is schema version "
                    f"{existing}; this LIPAS release supports "
                    f"{EXECUTION_SCHEMA_VERSION}. No automatic migration is available.",
                )
        with self._conn:
            self._conn.executescript(_SCHEMA)

    @property
    def schema_version(self) -> int:
        """The durable execution schema understood by this store."""
        return EXECUTION_SCHEMA_VERSION

    def close(self) -> None:
        if self._closed:
            return
        self._conn.close()
        self._closed = True

    def __enter__(self) -> "ExecutionStore":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    @contextlib.contextmanager
    def _transaction(self) -> Iterator[None]:
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self._conn.rollback()
            raise
        else:
            self._conn.commit()

    # -- Claim audit mirror -------------------------------------------

    @staticmethod
    def _audit_claim_id(identity: str) -> str:
        encoded = f"execution\0{identity}".encode("utf-8")
        return f"execution_audit_{hashlib.sha256(encoded).hexdigest()}"

    def _record_audit_event(
        self,
        identity: str,
        tag: str,
        fields: Mapping[str, Any],
        *,
        created_at: float,
    ) -> None:
        """Append one Claim-shaped event in the execution transaction."""
        self._conn.execute(
            "INSERT OR IGNORE INTO execution_audit_events"
            "(claim_id,tag,fields_json,created_at) VALUES(?,?,?,?)",
            (
                self._audit_claim_id(identity),
                tag,
                _mapping_json(fields),
                created_at,
            ),
        )

    def _seed_legacy_audit_events(self) -> None:
        """Make databases from the pre-release execution slice auditable.

        Current Task/Run state cannot reconstruct every historical lease, but
        checkpoints and interrupts retain their complete identities. New
        transitions are recorded transactionally from their first release.
        """
        tasks = self._conn.execute(
            "SELECT id,goal,workspace,state,created_at,updated_at "
            "FROM execution_tasks ORDER BY created_at,id",
        ).fetchall()
        for task_id, goal, workspace, state, created_at, updated_at in tasks:
            self._record_audit_event(
                f"task:{task_id}:created",
                TAG_EXECUTION_TASK_CREATED,
                {
                    "task_id": task_id,
                    "goal": goal,
                    "workspace": workspace,
                    "state": TaskState.OPEN.value,
                },
                created_at=created_at,
            )
            if state in {TaskState.COMPLETED.value, TaskState.CANCELLED.value}:
                tag = (
                    TAG_EXECUTION_TASK_COMPLETED
                    if state == TaskState.COMPLETED.value
                    else TAG_EXECUTION_TASK_CANCELLED
                )
                self._record_audit_event(
                    f"task:{task_id}:state:{state}",
                    tag,
                    {"task_id": task_id, "state": state},
                    created_at=updated_at,
                )

        runs = self._conn.execute(
            "SELECT id,task_id,state,attempt,checkpoint_version,lease_expires,"
            "cancel_requested,created_at,updated_at FROM execution_runs "
            "ORDER BY created_at,id",
        ).fetchall()
        for (
            run_id, task_id, state, attempt, checkpoint_version, lease_expires,
            cancel_requested, created_at, updated_at,
        ) in runs:
            self._record_audit_event(
                f"run:{run_id}:created",
                TAG_EXECUTION_RUN_CREATED,
                {
                    "run_id": run_id,
                    "task_id": task_id,
                    "state": RunState.PENDING.value,
                },
                created_at=created_at,
            )
            if attempt:
                self._record_audit_event(
                    f"run:{run_id}:claimed:{attempt}",
                    TAG_EXECUTION_RUN_CLAIMED,
                    {
                        "run_id": run_id,
                        "task_id": task_id,
                        "attempt": attempt,
                        "lease_expires": lease_expires,
                    },
                    created_at=updated_at,
                )
            if cancel_requested:
                self._record_audit_event(
                    f"run:{run_id}:cancel-requested",
                    TAG_EXECUTION_CANCEL_REQUESTED,
                    {"run_id": run_id, "task_id": task_id},
                    created_at=updated_at,
                )
            terminal_tag = {
                RunState.COMPLETED.value: TAG_EXECUTION_RUN_COMPLETED,
                RunState.FAILED.value: TAG_EXECUTION_RUN_FAILED,
                RunState.CANCELLED.value: TAG_EXECUTION_RUN_CANCELLED,
            }.get(state)
            if terminal_tag is not None:
                self._record_audit_event(
                    f"run:{run_id}:state:{state}",
                    terminal_tag,
                    {
                        "run_id": run_id,
                        "task_id": task_id,
                        "state": state,
                        "checkpoint_version": checkpoint_version,
                    },
                    created_at=updated_at,
                )

        for run_id, version, phase, created_at in self._conn.execute(
            "SELECT run_id,version,phase,created_at FROM execution_checkpoints "
            "ORDER BY created_at,run_id,version",
        ):
            self._record_audit_event(
                f"run:{run_id}:checkpoint:{version}",
                TAG_EXECUTION_CHECKPOINT_SAVED,
                {"run_id": run_id, "version": version, "phase": phase},
                created_at=created_at,
            )

        interrupts = self._conn.execute(
            "SELECT id,run_id,kind,state,created_at,resolved_at "
            "FROM execution_interrupts ORDER BY created_at,id",
        ).fetchall()
        for interrupt_id, run_id, kind, state, created_at, resolved_at in interrupts:
            self._record_audit_event(
                f"interrupt:{interrupt_id}:requested",
                TAG_EXECUTION_INTERRUPT_REQUESTED,
                {
                    "interrupt_id": interrupt_id,
                    "run_id": run_id,
                    "kind": kind,
                    "state": InterruptState.PENDING.value,
                },
                created_at=created_at,
            )
            if state != InterruptState.PENDING.value:
                self._record_audit_event(
                    f"interrupt:{interrupt_id}:resolved",
                    TAG_EXECUTION_INTERRUPT_RESOLVED,
                    {
                        "interrupt_id": interrupt_id,
                        "run_id": run_id,
                        "kind": kind,
                        "state": state,
                    },
                    created_at=resolved_at if resolved_at is not None else created_at,
                )

    def repair_audit(self) -> int:
        """Idempotently mirror committed execution events into a Claim tape.

        The execution database remains authoritative for control state. The
        optional Claim mirror is evidence for inspection and reporting; a
        crash between the two databases is repaired from this outbox.
        """
        if self.rowset is None:
            return 0
        events = self._conn.execute(
            "SELECT rowid,claim_id,tag,fields_json FROM execution_audit_events "
            "WHERE rowid>? ORDER BY rowid",
            (self._audit_cursor,),
        ).fetchall()
        if not events:
            return 0
        known = {claim.claim_id for claim in self.rowset.store}
        repaired = 0
        for rowid, claim_id, tag, fields_json in events:
            fields = _from_json(fields_json)
            if not isinstance(fields, Mapping):
                raise TypeError(f"execution audit {claim_id!r} fields are not a mapping")
            self.rowset.fold(Claim(
                tag=tag,
                fields=dict(fields),
                source="execution.store",
                claim_id=claim_id,
            ))
            if claim_id not in known:
                repaired += 1
                known.add(claim_id)
            self._audit_cursor = rowid
        return repaired

    # -- tasks ---------------------------------------------------------

    def create_task(
        self,
        goal: str,
        workspace: str | Path,
        *,
        task_id: str | None = None,
    ) -> Task:
        goal = goal.strip()
        if not goal:
            raise ValueError("task goal must be non-empty")
        root = Path(workspace).expanduser().resolve()
        if not root.is_dir():
            raise ValueError(f"workspace is not a directory: {root}")
        task_id = task_id or _id("task")
        now = time.time()
        try:
            with self._transaction():
                self._conn.execute(
                    "INSERT INTO execution_tasks"
                    "(id,goal,workspace,state,created_at,updated_at) "
                    "VALUES(?,?,?,'open',?,?)",
                    (task_id, goal, str(root), now, now),
                )
                self._record_audit_event(
                    f"task:{task_id}:created",
                    TAG_EXECUTION_TASK_CREATED,
                    {
                        "task_id": task_id,
                        "goal": goal,
                        "workspace": str(root),
                        "state": TaskState.OPEN.value,
                    },
                    created_at=now,
                )
        except sqlite3.IntegrityError as exc:
            raise ExecutionStateError(f"task id already exists: {task_id}") from exc
        task = self.get_task(task_id)
        assert task is not None
        self.repair_audit()
        return task

    def get_task(self, task_id: str) -> Task | None:
        row = self._conn.execute(
            "SELECT id,goal,workspace,state,created_at,updated_at "
            "FROM execution_tasks WHERE id=?",
            (task_id,),
        ).fetchone()
        if row is None:
            return None
        return Task(row[0], row[1], row[2], TaskState(row[3]), row[4], row[5])

    def list_tasks(self, *, state: TaskState | None = None) -> tuple[Task, ...]:
        """Return tasks newest first for workbench and operator projections."""
        if state is not None and not isinstance(state, TaskState):
            raise TypeError("state must be TaskState or None")
        sql = (
            "SELECT id,goal,workspace,state,created_at,updated_at "
            "FROM execution_tasks"
        )
        params: tuple[Any, ...] = ()
        if state is not None:
            sql += " WHERE state=?"
            params = (state.value,)
        sql += " ORDER BY created_at DESC,id DESC"
        return tuple(
            Task(row[0], row[1], row[2], TaskState(row[3]), row[4], row[5])
            for row in self._conn.execute(sql, params)
        )

    # -- runs and leases -----------------------------------------------

    def create_run(self, task_id: str, *, run_id: str | None = None) -> Run:
        task = self.get_task(task_id)
        if task is None:
            raise KeyError(task_id)
        if task.state is not TaskState.OPEN:
            raise ExecutionStateError(
                f"task {task_id!r} is {task.state.value}, not open",
            )
        run_id = run_id or _id("run")
        now = time.time()
        try:
            with self._transaction():
                self._conn.execute(
                    "INSERT INTO execution_runs"
                    "(id,task_id,state,created_at,updated_at) "
                    "VALUES(?,?,'pending',?,?)",
                    (run_id, task_id, now, now),
                )
                self._record_audit_event(
                    f"run:{run_id}:created",
                    TAG_EXECUTION_RUN_CREATED,
                    {
                        "run_id": run_id,
                        "task_id": task_id,
                        "state": RunState.PENDING.value,
                    },
                    created_at=now,
                )
        except sqlite3.IntegrityError as exc:
            raise ExecutionStateError(
                f"task {task_id!r} already has an active run or run id {run_id!r} exists",
            ) from exc
        run = self.get_run(run_id)
        assert run is not None
        self.repair_audit()
        return run

    def get_run(self, run_id: str) -> Run | None:
        row = self._conn.execute(
            "SELECT id,task_id,state,attempt,checkpoint_version,lease_token,"
            "lease_expires,cancel_requested,result_json,error_json,created_at,updated_at "
            "FROM execution_runs WHERE id=?",
            (run_id,),
        ).fetchone()
        if row is None:
            return None
        error = _from_json(row[9]) if row[9] is not None else None
        return Run(
            id=row[0],
            task_id=row[1],
            state=RunState(row[2]),
            attempt=row[3],
            checkpoint_version=row[4],
            lease_token=row[5],
            lease_expires=row[6],
            cancel_requested=bool(row[7]),
            result=_from_json(row[8]) if row[8] is not None else None,
            error=error,
            created_at=row[10],
            updated_at=row[11],
        )

    def list_runs(self, *, task_id: str | None = None) -> tuple[Run, ...]:
        """Return runs newest first, optionally restricted to one task."""
        sql = (
            "SELECT id,task_id,state,attempt,checkpoint_version,lease_token,"
            "lease_expires,cancel_requested,result_json,error_json,created_at,updated_at "
            "FROM execution_runs"
        )
        params: tuple[Any, ...] = ()
        if task_id is not None:
            sql += " WHERE task_id=?"
            params = (task_id,)
        sql += " ORDER BY created_at DESC,id DESC"
        runs: list[Run] = []
        for row in self._conn.execute(sql, params):
            runs.append(Run(
                id=row[0],
                task_id=row[1],
                state=RunState(row[2]),
                attempt=row[3],
                checkpoint_version=row[4],
                lease_token=row[5],
                lease_expires=row[6],
                cancel_requested=bool(row[7]),
                result=_from_json(row[8]) if row[8] is not None else None,
                error=_from_json(row[9]) if row[9] is not None else None,
                created_at=row[10],
                updated_at=row[11],
            ))
        return tuple(runs)

    def list_claimable_runs(self, *, now: float | None = None) -> tuple[Run, ...]:
        """Return open pending/expired runs in FIFO dispatch order.

        Discovery is intentionally not a claim. Multiple dispatchers may see
        the same candidate; the later conditional ``claim_run`` transition is
        the authoritative race boundary and only one worker can win it.
        """
        now = _timestamp(now)
        run_ids = tuple(
            row[0]
            for row in self._conn.execute(
                "SELECT r.id FROM execution_runs AS r "
                "JOIN execution_tasks AS t ON t.id=r.task_id "
                "WHERE "
                "(t.state='open' AND r.state='pending' "
                " AND r.cancel_requested=0) OR "
                "(r.state='running' AND r.lease_expires IS NOT NULL "
                " AND r.lease_expires<=?) "
                "ORDER BY r.created_at,r.id",
                (now,),
            )
        )
        runs: list[Run] = []
        for run_id in run_ids:
            run = self.get_run(run_id)
            if run is not None:
                runs.append(run)
        return tuple(runs)

    def claim_run(
        self,
        run_id: str,
        *,
        lease_seconds: float = 60.0,
        now: float | None = None,
    ) -> Run:
        lease_seconds = _positive_duration(lease_seconds, "lease_seconds")
        now = _timestamp(now)
        token = _id("lease")
        with self._transaction():
            cursor = self._conn.execute(
                "UPDATE execution_runs SET state='running',attempt=attempt+1,"
                "lease_token=?,lease_expires=?,updated_at=? "
                "WHERE id=? AND "
                "(state='pending' AND cancel_requested=0 OR "
                " state='running' AND lease_expires IS NOT NULL AND lease_expires<=?)",
                (token, now + lease_seconds, now, run_id, now),
            )
            if cursor.rowcount != 1:
                run = self.get_run(run_id)
                if run is None:
                    raise KeyError(run_id)
                raise ExecutionLeaseError(
                    f"run {run_id!r} cannot be claimed from state {run.state.value!r}",
                )
            claimed = self.get_run(run_id)
            assert claimed is not None
            self._record_audit_event(
                f"run:{run_id}:claimed:{claimed.attempt}",
                TAG_EXECUTION_RUN_CLAIMED,
                {
                    "run_id": run_id,
                    "task_id": claimed.task_id,
                    "attempt": claimed.attempt,
                    "lease_expires": claimed.lease_expires,
                },
                created_at=now,
            )
        run = self.get_run(run_id)
        assert run is not None
        self.repair_audit()
        return run

    def renew_lease(
        self,
        run_id: str,
        lease_token: str,
        *,
        lease_seconds: float = 60.0,
        now: float | None = None,
    ) -> Run:
        lease_seconds = _positive_duration(lease_seconds, "lease_seconds")
        now = _timestamp(now)
        with self._transaction():
            cursor = self._conn.execute(
                "UPDATE execution_runs SET lease_expires=?,updated_at=? "
                "WHERE id=? AND state='running' AND lease_token=? "
                "AND lease_expires>?",
                (now + lease_seconds, now, run_id, lease_token, now),
            )
            if cursor.rowcount != 1:
                self._raise_lease(run_id)
            renewed = self.get_run(run_id)
            assert renewed is not None
            self._record_audit_event(
                f"run:{run_id}:lease-renewed:{renewed.attempt}:{renewed.lease_expires!r}",
                TAG_EXECUTION_LEASE_RENEWED,
                {
                    "run_id": run_id,
                    "task_id": renewed.task_id,
                    "attempt": renewed.attempt,
                    "lease_expires": renewed.lease_expires,
                },
                created_at=now,
            )
        run = self.get_run(run_id)
        assert run is not None
        self.repair_audit()
        return run

    def _require_lease(
        self,
        run_id: str,
        lease_token: str,
        *,
        now: float,
    ) -> Run:
        run = self.get_run(run_id)
        if run is None:
            raise KeyError(run_id)
        if (
            run.state is not RunState.RUNNING
            or run.lease_token != lease_token
            or run.lease_expires is None
            or run.lease_expires <= now
        ):
            raise ExecutionLeaseError(
                f"run {run_id!r} is not owned by this active lease",
            )
        return run

    def _raise_lease(self, run_id: str) -> None:
        if self.get_run(run_id) is None:
            raise KeyError(run_id)
        raise ExecutionLeaseError(
            f"run {run_id!r} is not owned by this active lease",
        )

    # -- checkpoints and interrupts -----------------------------------

    def save_checkpoint(
        self,
        run_id: str,
        lease_token: str,
        *,
        expected_version: int,
        phase: str,
        state: Mapping[str, Any],
        now: float | None = None,
    ) -> Checkpoint:
        if expected_version < 0:
            raise ValueError("expected_version must be non-negative")
        if not phase:
            raise ValueError("checkpoint phase must be non-empty")
        state_json = _mapping_json(state)
        now = _timestamp(now)
        with self._transaction():
            run = self._require_lease(run_id, lease_token, now=now)
            if run.checkpoint_version != expected_version:
                raise CheckpointConflict(
                    f"run {run_id!r} checkpoint is version "
                    f"{run.checkpoint_version}, expected {expected_version}",
                )
            version = expected_version + 1
            self._conn.execute(
                "INSERT INTO execution_checkpoints"
                "(run_id,version,phase,state_json,created_at) VALUES(?,?,?,?,?)",
                (run_id, version, phase, state_json, now),
            )
            cursor = self._conn.execute(
                "UPDATE execution_runs SET checkpoint_version=?,updated_at=? "
                "WHERE id=? AND checkpoint_version=? AND lease_token=?",
                (version, now, run_id, expected_version, lease_token),
            )
            if cursor.rowcount != 1:
                raise CheckpointConflict(f"run {run_id!r} changed concurrently")
            self._record_audit_event(
                f"run:{run_id}:checkpoint:{version}",
                TAG_EXECUTION_CHECKPOINT_SAVED,
                {"run_id": run_id, "version": version, "phase": phase},
                created_at=now,
            )
        checkpoint = self.get_checkpoint(run_id, version=version)
        assert checkpoint is not None
        self.repair_audit()
        return checkpoint

    def get_checkpoint(
        self,
        run_id: str,
        *,
        version: int | None = None,
    ) -> Checkpoint | None:
        if version is None:
            row = self._conn.execute(
                "SELECT run_id,version,phase,state_json,created_at "
                "FROM execution_checkpoints WHERE run_id=? "
                "ORDER BY version DESC LIMIT 1",
                (run_id,),
            ).fetchone()
        else:
            row = self._conn.execute(
                "SELECT run_id,version,phase,state_json,created_at "
                "FROM execution_checkpoints WHERE run_id=? AND version=?",
                (run_id, version),
            ).fetchone()
        if row is None:
            return None
        return Checkpoint(row[0], row[1], row[2], _from_json(row[3]), row[4])

    def suspend(
        self,
        run_id: str,
        lease_token: str,
        *,
        expected_version: int,
        phase: str,
        checkpoint_state: Mapping[str, Any],
        kind: str,
        request: Mapping[str, Any],
        interrupt_id: str | None = None,
        now: float | None = None,
    ) -> Interrupt:
        """Atomically checkpoint a run and suspend it for external input."""
        if not kind:
            raise ValueError("interrupt kind must be non-empty")
        if not phase:
            raise ValueError("checkpoint phase must be non-empty")
        state_json = _mapping_json(checkpoint_state)
        request_json = _mapping_json(request)
        interrupt_id = interrupt_id or _id("interrupt")
        now = _timestamp(now)
        try:
            with self._transaction():
                run = self._require_lease(run_id, lease_token, now=now)
                if run.checkpoint_version != expected_version:
                    raise CheckpointConflict(
                        f"run {run_id!r} checkpoint is version "
                        f"{run.checkpoint_version}, expected {expected_version}",
                    )
                version = expected_version + 1
                self._conn.execute(
                    "INSERT INTO execution_checkpoints"
                    "(run_id,version,phase,state_json,created_at) VALUES(?,?,?,?,?)",
                    (run_id, version, phase, state_json, now),
                )
                self._conn.execute(
                    "INSERT INTO execution_interrupts"
                    "(id,run_id,kind,request_json,state,created_at) "
                    "VALUES(?,?,?,?,'pending',?)",
                    (interrupt_id, run_id, kind, request_json, now),
                )
                cursor = self._conn.execute(
                    "UPDATE execution_runs SET state='waiting',checkpoint_version=?,"
                    "lease_token=NULL,lease_expires=NULL,updated_at=? "
                    "WHERE id=? AND state='running' AND lease_token=? "
                    "AND checkpoint_version=?",
                    (version, now, run_id, lease_token, expected_version),
                )
                if cursor.rowcount != 1:
                    raise CheckpointConflict(f"run {run_id!r} changed concurrently")
                self._record_audit_event(
                    f"run:{run_id}:checkpoint:{version}",
                    TAG_EXECUTION_CHECKPOINT_SAVED,
                    {"run_id": run_id, "version": version, "phase": phase},
                    created_at=now,
                )
                self._record_audit_event(
                    f"interrupt:{interrupt_id}:requested",
                    TAG_EXECUTION_INTERRUPT_REQUESTED,
                    {
                        "interrupt_id": interrupt_id,
                        "run_id": run_id,
                        "kind": kind,
                        "state": InterruptState.PENDING.value,
                    },
                    created_at=now,
                )
        except sqlite3.IntegrityError as exc:
            raise ExecutionStateError(
                f"interrupt id {interrupt_id!r} already exists or the run "
                "already has a pending interrupt",
            ) from exc
        interrupt = self.get_interrupt(interrupt_id)
        assert interrupt is not None
        self.repair_audit()
        return interrupt

    def get_interrupt(self, interrupt_id: str) -> Interrupt | None:
        row = self._conn.execute(
            "SELECT id,run_id,kind,request_json,state,response_json,"
            "created_at,resolved_at FROM execution_interrupts WHERE id=?",
            (interrupt_id,),
        ).fetchone()
        if row is None:
            return None
        return Interrupt(
            id=row[0],
            run_id=row[1],
            kind=row[2],
            request=_from_json(row[3]),
            state=InterruptState(row[4]),
            response=_from_json(row[5]) if row[5] is not None else None,
            created_at=row[6],
            resolved_at=row[7],
        )

    def list_interrupts(
        self,
        *,
        run_id: str | None = None,
        state: InterruptState | None = None,
    ) -> tuple[Interrupt, ...]:
        """Return durable approval/interrupt records newest first."""
        if state is not None and not isinstance(state, InterruptState):
            raise TypeError("state must be InterruptState or None")
        conditions: list[str] = []
        params: list[Any] = []
        if run_id is not None:
            conditions.append("run_id=?")
            params.append(run_id)
        if state is not None:
            conditions.append("state=?")
            params.append(state.value)
        sql = (
            "SELECT id,run_id,kind,request_json,state,response_json,"
            "created_at,resolved_at FROM execution_interrupts"
        )
        if conditions:
            sql += " WHERE " + " AND ".join(conditions)
        sql += " ORDER BY created_at DESC,id DESC"
        return tuple(
            Interrupt(
                id=row[0], run_id=row[1], kind=row[2],
                request=_from_json(row[3]), state=InterruptState(row[4]),
                response=_from_json(row[5]) if row[5] is not None else None,
                created_at=row[6], resolved_at=row[7],
            )
            for row in self._conn.execute(sql, tuple(params))
        )

    def resolve_interrupt(
        self,
        interrupt_id: str,
        *,
        allow: bool,
        response: Any = None,
        now: float | None = None,
    ) -> Interrupt:
        response_json = _json(response)
        target = InterruptState.ALLOWED if allow else InterruptState.DENIED
        run_state = RunState.PENDING if allow else RunState.CANCELLED
        now = _timestamp(now)
        existing = self.get_interrupt(interrupt_id)
        if existing is None:
            raise KeyError(interrupt_id)
        if existing.state is not InterruptState.PENDING:
            if existing.state is target and existing.response == response:
                self.repair_audit()
                return existing
            raise ExecutionStateError(
                f"interrupt {interrupt_id!r} was already {existing.state.value}",
            )
        with self._transaction():
            current = self.get_interrupt(interrupt_id)
            if current is None:
                raise KeyError(interrupt_id)
            if current.state is not InterruptState.PENDING:
                raise ExecutionStateError(
                    f"interrupt {interrupt_id!r} changed concurrently",
                )
            cursor = self._conn.execute(
                "UPDATE execution_interrupts SET state=?,response_json=?,resolved_at=? "
                "WHERE id=? AND state='pending'",
                (target.value, response_json, now, interrupt_id),
            )
            if cursor.rowcount != 1:
                raise ExecutionStateError(
                    f"interrupt {interrupt_id!r} changed concurrently",
                )
            cursor = self._conn.execute(
                "UPDATE execution_runs SET state=?,updated_at=? "
                "WHERE id=? AND state='waiting'",
                (run_state.value, now, current.run_id),
            )
            if cursor.rowcount != 1:
                raise ExecutionStateError(
                    f"run {current.run_id!r} is no longer waiting",
                )
            self._record_audit_event(
                f"interrupt:{interrupt_id}:resolved",
                TAG_EXECUTION_INTERRUPT_RESOLVED,
                {
                    "interrupt_id": interrupt_id,
                    "run_id": current.run_id,
                    "kind": current.kind,
                    "state": target.value,
                },
                created_at=now,
            )
            if not allow:
                run = self.get_run(current.run_id)
                assert run is not None
                self._record_audit_event(
                    f"run:{current.run_id}:state:{RunState.CANCELLED.value}",
                    TAG_EXECUTION_RUN_CANCELLED,
                    {
                        "run_id": current.run_id,
                        "task_id": run.task_id,
                        "state": RunState.CANCELLED.value,
                        "checkpoint_version": run.checkpoint_version,
                    },
                    created_at=now,
                )
        resolved = self.get_interrupt(interrupt_id)
        assert resolved is not None
        self.repair_audit()
        return resolved

    # -- terminal transitions and cancellation ------------------------

    def complete_run(
        self,
        run_id: str,
        lease_token: str,
        *,
        result: Any,
        now: float | None = None,
    ) -> Run:
        result_json = _json(result)
        now = _timestamp(now)
        with self._transaction():
            run = self._require_lease(run_id, lease_token, now=now)
            if run.cancel_requested:
                raise ExecutionStateError(
                    f"run {run_id!r} has a pending cancellation request",
                )
            cursor = self._conn.execute(
                "UPDATE execution_runs SET state='completed',result_json=?,"
                "lease_token=NULL,lease_expires=NULL,updated_at=? "
                "WHERE id=? AND state='running' AND lease_token=?",
                (result_json, now, run_id, lease_token),
            )
            if cursor.rowcount != 1:
                self._raise_lease(run_id)
            self._conn.execute(
                "UPDATE execution_tasks SET state='completed',updated_at=? "
                "WHERE id=? AND state='open'",
                (now, run.task_id),
            )
            self._record_audit_event(
                f"run:{run_id}:state:{RunState.COMPLETED.value}",
                TAG_EXECUTION_RUN_COMPLETED,
                {
                    "run_id": run_id,
                    "task_id": run.task_id,
                    "state": RunState.COMPLETED.value,
                    "checkpoint_version": run.checkpoint_version,
                },
                created_at=now,
            )
            self._record_audit_event(
                f"task:{run.task_id}:state:{TaskState.COMPLETED.value}",
                TAG_EXECUTION_TASK_COMPLETED,
                {"task_id": run.task_id, "state": TaskState.COMPLETED.value},
                created_at=now,
            )
        completed = self.get_run(run_id)
        assert completed is not None
        self.repair_audit()
        return completed

    def fail_run(
        self,
        run_id: str,
        lease_token: str,
        *,
        error: Mapping[str, Any],
        now: float | None = None,
    ) -> Run:
        error_json = _mapping_json(error)
        now = _timestamp(now)
        with self._transaction():
            self._require_lease(run_id, lease_token, now=now)
            cursor = self._conn.execute(
                "UPDATE execution_runs SET state='failed',error_json=?,"
                "lease_token=NULL,lease_expires=NULL,updated_at=? "
                "WHERE id=? AND state='running' AND lease_token=?",
                (error_json, now, run_id, lease_token),
            )
            if cursor.rowcount != 1:
                self._raise_lease(run_id)
            failed = self.get_run(run_id)
            assert failed is not None
            self._record_audit_event(
                f"run:{run_id}:state:{RunState.FAILED.value}",
                TAG_EXECUTION_RUN_FAILED,
                {
                    "run_id": run_id,
                    "task_id": failed.task_id,
                    "state": RunState.FAILED.value,
                    "checkpoint_version": failed.checkpoint_version,
                    "error": dict(error),
                },
                created_at=now,
            )
        failed = self.get_run(run_id)
        assert failed is not None
        self.repair_audit()
        return failed

    def request_cancel(self, run_id: str, *, now: float | None = None) -> Run:
        """Request cooperative cancellation or cancel an unowned run now."""
        now = _timestamp(now)
        current = self.get_run(run_id)
        if current is None:
            raise KeyError(run_id)
        if current.state in {RunState.COMPLETED, RunState.FAILED, RunState.CANCELLED}:
            self.repair_audit()
            return current
        with self._transaction():
            run = self.get_run(run_id)
            if run is None:
                raise KeyError(run_id)
            if run.state in {RunState.COMPLETED, RunState.FAILED, RunState.CANCELLED}:
                raise ExecutionStateError(
                    f"run {run_id!r} became terminal while cancellation was requested",
                )
            self._record_audit_event(
                f"run:{run_id}:cancel-requested",
                TAG_EXECUTION_CANCEL_REQUESTED,
                {"run_id": run_id, "task_id": run.task_id},
                created_at=now,
            )
            if run.state is RunState.RUNNING:
                self._conn.execute(
                    "UPDATE execution_runs SET cancel_requested=1,updated_at=? WHERE id=?",
                    (now, run_id),
                )
            else:
                pending_interrupt = (
                    self._conn.execute(
                        "SELECT id,kind FROM execution_interrupts "
                        "WHERE run_id=? AND state='pending'",
                        (run_id,),
                    ).fetchone()
                    if run.state is RunState.WAITING else None
                )
                self._conn.execute(
                    "UPDATE execution_runs SET state='cancelled',cancel_requested=1,"
                    "lease_token=NULL,lease_expires=NULL,updated_at=? WHERE id=?",
                    (now, run_id),
                )
                if run.state is RunState.WAITING:
                    self._conn.execute(
                        "UPDATE execution_interrupts SET state='denied',"
                        "response_json=?,resolved_at=? "
                        "WHERE run_id=? AND state='pending'",
                        (_json({"reason": "run_cancelled"}), now, run_id),
                    )
                self._record_audit_event(
                    f"run:{run_id}:state:{RunState.CANCELLED.value}",
                    TAG_EXECUTION_RUN_CANCELLED,
                    {
                        "run_id": run_id,
                        "task_id": run.task_id,
                        "state": RunState.CANCELLED.value,
                        "checkpoint_version": run.checkpoint_version,
                    },
                    created_at=now,
                )
                if pending_interrupt is not None:
                    interrupt_id, kind = pending_interrupt
                    self._record_audit_event(
                        f"interrupt:{interrupt_id}:resolved",
                        TAG_EXECUTION_INTERRUPT_RESOLVED,
                        {
                            "interrupt_id": interrupt_id,
                            "run_id": run_id,
                            "kind": kind,
                            "state": InterruptState.DENIED.value,
                        },
                        created_at=now,
                    )
        cancelled = self.get_run(run_id)
        assert cancelled is not None
        self.repair_audit()
        return cancelled

    def finish_cancelled(
        self,
        run_id: str,
        lease_token: str,
        *,
        now: float | None = None,
    ) -> Run:
        now = _timestamp(now)
        with self._transaction():
            run = self._require_lease(run_id, lease_token, now=now)
            if not run.cancel_requested:
                raise ExecutionStateError(
                    f"run {run_id!r} has no cancellation request",
                )
            cursor = self._conn.execute(
                "UPDATE execution_runs SET state='cancelled',lease_token=NULL,"
                "lease_expires=NULL,updated_at=? WHERE id=? AND lease_token=?",
                (now, run_id, lease_token),
            )
            if cursor.rowcount != 1:
                self._raise_lease(run_id)
            cancelled = self.get_run(run_id)
            assert cancelled is not None
            self._record_audit_event(
                f"run:{run_id}:state:{RunState.CANCELLED.value}",
                TAG_EXECUTION_RUN_CANCELLED,
                {
                    "run_id": run_id,
                    "task_id": cancelled.task_id,
                    "state": RunState.CANCELLED.value,
                    "checkpoint_version": cancelled.checkpoint_version,
                },
                created_at=now,
            )
        cancelled = self.get_run(run_id)
        assert cancelled is not None
        self.repair_audit()
        return cancelled

    def cancel_task(self, task_id: str, *, now: float | None = None) -> Task:
        """Cancel a Task and cooperatively stop its active Run, if any."""
        now = _timestamp(now)
        current = self.get_task(task_id)
        if current is None:
            raise KeyError(task_id)
        if current.state is TaskState.CANCELLED:
            self.repair_audit()
            return current
        if current.state is TaskState.COMPLETED:
            raise ExecutionStateError(
                f"task {task_id!r} is completed and cannot be cancelled",
            )

        with self._transaction():
            task = self.get_task(task_id)
            if task is None:
                raise KeyError(task_id)
            if task.state is not TaskState.OPEN:
                raise ExecutionStateError(
                    f"task {task_id!r} is {task.state.value}, not open",
                )
            run = self._conn.execute(
                "SELECT id FROM execution_runs WHERE task_id=? "
                "AND state IN ('pending','running','waiting')",
                (task_id,),
            ).fetchone()
            if run is not None:
                run_id = run[0]
                active = self.get_run(run_id)
                assert active is not None
                self._record_audit_event(
                    f"run:{run_id}:cancel-requested",
                    TAG_EXECUTION_CANCEL_REQUESTED,
                    {"run_id": run_id, "task_id": task_id},
                    created_at=now,
                )
                if active.state is RunState.RUNNING:
                    self._conn.execute(
                        "UPDATE execution_runs SET cancel_requested=1,updated_at=? "
                        "WHERE id=? AND state='running'",
                        (now, run_id),
                    )
                else:
                    pending_interrupt = (
                        self._conn.execute(
                            "SELECT id,kind FROM execution_interrupts "
                            "WHERE run_id=? AND state='pending'",
                            (run_id,),
                        ).fetchone()
                        if active.state is RunState.WAITING else None
                    )
                    self._conn.execute(
                        "UPDATE execution_runs SET state='cancelled',"
                        "cancel_requested=1,lease_token=NULL,lease_expires=NULL,"
                        "updated_at=? WHERE id=?",
                        (now, run_id),
                    )
                    if pending_interrupt is not None:
                        interrupt_id, kind = pending_interrupt
                        self._conn.execute(
                            "UPDATE execution_interrupts SET state='denied',"
                            "response_json=?,resolved_at=? WHERE id=? AND state='pending'",
                            (_json({"reason": "task_cancelled"}), now, interrupt_id),
                        )
                        self._record_audit_event(
                            f"interrupt:{interrupt_id}:resolved",
                            TAG_EXECUTION_INTERRUPT_RESOLVED,
                            {
                                "interrupt_id": interrupt_id,
                                "run_id": run_id,
                                "kind": kind,
                                "state": InterruptState.DENIED.value,
                            },
                            created_at=now,
                        )
                    self._record_audit_event(
                        f"run:{run_id}:state:{RunState.CANCELLED.value}",
                        TAG_EXECUTION_RUN_CANCELLED,
                        {
                            "run_id": run_id,
                            "task_id": task_id,
                            "state": RunState.CANCELLED.value,
                            "checkpoint_version": active.checkpoint_version,
                        },
                        created_at=now,
                    )
            cursor = self._conn.execute(
                "UPDATE execution_tasks SET state='cancelled',updated_at=? "
                "WHERE id=? AND state='open'",
                (now, task_id),
            )
            if cursor.rowcount != 1:
                raise ExecutionStateError(f"task {task_id!r} changed concurrently")
            self._record_audit_event(
                f"task:{task_id}:state:{TaskState.CANCELLED.value}",
                TAG_EXECUTION_TASK_CANCELLED,
                {"task_id": task_id, "state": TaskState.CANCELLED.value},
                created_at=now,
            )
        cancelled_task = self.get_task(task_id)
        assert cancelled_task is not None
        self.repair_audit()
        return cancelled_task
