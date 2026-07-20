"""First-party local workspace task workbench.

The workbench is a product layer over the reusable runtime.  It owns local
workspace policy, evidence, artifacts and reports while reusing
``ExecutionStore`` for authoritative Task/Run/Interrupt control state and the
runtime Effect tape for model/tool audit history.
"""
from __future__ import annotations

import hashlib
import difflib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .behaviour import FinalResult
from .execution import ExecutionStore, Interrupt, Run, Task
from .rows import RowSet
from .tools import SideEffectClass, Tool, tool
from .security import SecretDetected, SecretPolicy
from .sandbox import CommandSandbox, sandbox_from_name

__all__ = [
    "Approval",
    "Artifact",
    "ChangeSet",
    "TaskReport",
    "Verification",
    "RunEvent",
    "Workbench",
    "WorkbenchSchemaVersionMismatch",
    "Workspace",
    "WorkspacePolicyError",
    "workbench_approval_policy",
]


WORKBENCH_SCHEMA_VERSION = 1


class WorkspacePolicyError(ValueError):
    """A requested local operation is outside the workbench policy."""


class WorkbenchSchemaVersionMismatch(RuntimeError):
    """The product database belongs to an incompatible LIPAS release."""


def workbench_approval_policy(
    tool_value: Tool,
    arguments: Mapping[str, Any],
) -> Mapping[str, Any] | None:
    """Request write approval without copying file bodies into control state."""
    if tool_value.side_effect not in {
        SideEffectClass.IDEMPOTENT_WRITE,
        SideEffectClass.EXTERNAL_WRITE,
    }:
        return None
    SecretPolicy().check_tool_arguments(tool_value.name, arguments)
    summarized: dict[str, Any] = {}
    for key, value in arguments.items():
        if key == "content" and isinstance(value, str):
            encoded = value.encode("utf-8")
            summarized[key] = {
                "sha256": hashlib.sha256(encoded).hexdigest(),
                "bytes": len(encoded),
                "redacted": True,
            }
        else:
            summarized[key] = value
    return {
        "tool_name": tool_value.name,
        "arguments": summarized,
        "side_effect": tool_value.side_effect.value,
    }


@dataclass(frozen=True, slots=True)
class Workspace:
    root: str


@dataclass(frozen=True, slots=True)
class Approval:
    id: str
    run_id: str
    request: Mapping[str, Any]
    state: str
    response: Any | None
    created_at: float
    resolved_at: float | None

    @classmethod
    def from_interrupt(cls, value: Interrupt) -> "Approval":
        return cls(
            id=value.id,
            run_id=value.run_id,
            request=dict(value.request),
            state=value.state.value,
            response=value.response,
            created_at=value.created_at,
            resolved_at=value.resolved_at,
        )


@dataclass(frozen=True, slots=True)
class Artifact:
    id: str
    task_id: str
    run_id: str
    kind: str
    path: str | None
    sha256: str | None
    metadata: Mapping[str, Any]
    created_at: float


@dataclass(frozen=True, slots=True)
class ChangeSet:
    task_id: str
    run_id: str
    source_root: str
    stage_root: str
    baseline: Mapping[str, str]
    state: str
    created_at: float
    updated_at: float


@dataclass(frozen=True, slots=True)
class Verification:
    command: tuple[str, ...]
    exit_code: int | None
    timed_out: bool
    stdout: str
    stderr: str
    created_at: float
    sandbox: str = "unknown"
    isolated: bool = False
    network_isolated: bool = False

    @property
    def passed(self) -> bool:
        return not self.timed_out and self.exit_code == 0


@dataclass(frozen=True, slots=True)
class TaskReport:
    task_id: str
    run_id: str
    status: str
    final_text: str
    changed_files: tuple[str, ...]
    artifacts: tuple[Artifact, ...]
    verifications: tuple[Verification, ...]
    verified: bool
    diff: str
    unresolved_risks: tuple[str, ...]
    change_set_state: str | None
    created_at: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "run_id": self.run_id,
            "status": self.status,
            "final_text": self.final_text,
            "changed_files": list(self.changed_files),
            "artifacts": [asdict(value) for value in self.artifacts],
            "verifications": [asdict(value) for value in self.verifications],
            "verified": self.verified,
            "diff": self.diff,
            "unresolved_risks": list(self.unresolved_risks),
            "change_set_state": self.change_set_state,
            "created_at": self.created_at,
        }


@dataclass(frozen=True, slots=True)
class RunEvent:
    id: str
    task_id: str
    run_id: str
    kind: str
    data: Mapping[str, Any]
    created_at: float


_SCHEMA = """
CREATE TABLE IF NOT EXISTS workbench_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS workbench_artifacts (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    path TEXT,
    sha256 TEXT,
    metadata_json TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS workbench_artifacts_task
    ON workbench_artifacts(task_id, created_at);
CREATE TABLE IF NOT EXISTS workbench_reports (
    task_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    report_json TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS workbench_run_sessions (
    run_id TEXT PRIMARY KEY,
    claims_path TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS workbench_change_sets (
    run_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL UNIQUE,
    source_root TEXT NOT NULL,
    stage_root TEXT NOT NULL,
    baseline_json TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('open','ready','applied','discarded')),
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS workbench_events (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    data_json TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS workbench_events_task
    ON workbench_events(task_id, created_at);
"""


class Workbench:
    """Local product state and bounded workspace capabilities."""

    def __init__(
        self,
        home: str | Path,
        *,
        rowset: RowSet | None = None,
        sandbox: str = "auto",
    ) -> None:
        self.home = Path(home).expanduser().resolve()
        self.home.mkdir(parents=True, exist_ok=True)
        self.execution_path = self.home / "execution.db"
        # Compatibility path for runs created before per-Run Claim sessions.
        self.claims_path = self.home / "claims.db"
        self.runs_path = self.home / "runs"
        self.product_path = self.home / "workbench.db"
        self.command_sandbox = sandbox_from_name(sandbox)
        self.execution = ExecutionStore(self.execution_path, rowset=rowset)
        self._conn = sqlite3.connect(self.product_path)
        try:
            self._init_schema()
        except BaseException:
            self.execution.close()
            self._conn.close()
            raise

    def _init_schema(self) -> None:
        with self._conn:
            self._conn.executescript(_SCHEMA)
            row = self._conn.execute(
                "SELECT value FROM workbench_meta WHERE key='schema_version'",
            ).fetchone()
            if row is None:
                self._conn.execute(
                    "INSERT INTO workbench_meta(key,value) VALUES('schema_version',?)",
                    (str(WORKBENCH_SCHEMA_VERSION),),
                )
            elif row[0] != str(WORKBENCH_SCHEMA_VERSION):
                raise WorkbenchSchemaVersionMismatch(
                    f"workbench database schema is {row[0]}; this release supports "
                    f"{WORKBENCH_SCHEMA_VERSION}",
                )

    def close(self) -> None:
        self.execution.close()
        self._conn.close()

    def attach_rowset(self, rowset: RowSet) -> None:
        """Attach the Agent evidence tape before executing/resuming a run."""
        self.execution.close()
        self.execution = ExecutionStore(self.execution_path, rowset=rowset)

    def __enter__(self) -> "Workbench":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    def create_task(
        self,
        goal: str,
        workspace: str | Path,
        *,
        isolate_changes: bool = False,
    ) -> tuple[Task, Run]:
        SecretPolicy().check(goal, path="task.goal")
        task = self.execution.create_task(goal, workspace)
        run = self.execution.create_run(task.id)
        self.claims_path_for_run(run.id)
        if isolate_changes:
            self.create_change_set(task.id, run.id)
        self.add_event(
            task_id=task.id,
            run_id=run.id,
            kind="task_created",
            data={"goal": task.goal, "workspace": task.workspace},
            event_id=f"task:{task.id}:created",
        )
        self.add_event(
            task_id=task.id,
            run_id=run.id,
            kind="run_created",
            data={"state": run.state.value},
            event_id=f"run:{run.id}:created",
        )
        return task, run

    def create_change_set(self, task_id: str, run_id: str) -> ChangeSet:
        task = self.execution.get_task(task_id)
        run = self.execution.get_run(run_id)
        if task is None or run is None or run.task_id != task_id:
            raise KeyError(run_id)
        existing = self.change_set(task_id)
        if existing is not None:
            return existing
        source = Path(task.workspace).resolve()
        stage = (self.runs_path / run_id / "workspace").resolve()
        if stage.exists():
            raise WorkspacePolicyError(
                f"staging workspace already exists without a ChangeSet: {stage}",
            )
        stage.mkdir(parents=True)
        baseline: dict[str, str] = {}
        total_bytes = 0
        excluded_secret_files = 0
        excluded_large_files = 0
        try:
            for relative, source_path in _snapshot_source_files(source):
                if (
                    self.home.is_relative_to(source)
                    and source_path.is_relative_to(self.home)
                ):
                    continue
                size = source_path.stat().st_size
                if size > _CHANGESET_MAX_FILE_BYTES:
                    excluded_large_files += 1
                    continue
                if _snapshot_file_contains_secret(source_path, relative):
                    excluded_secret_files += 1
                    continue
                total_bytes += size
                if (
                    len(baseline) >= _CHANGESET_MAX_FILES
                    or total_bytes > _CHANGESET_MAX_BYTES
                ):
                    raise WorkspacePolicyError(
                        "workspace snapshot exceeds the ChangeSet file/size limit",
                    )
                destination = stage / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source_path, destination)
                baseline[relative] = _file_sha256(source_path)
        except BaseException:
            shutil.rmtree(stage, ignore_errors=True)
            raise
        now = time.time()
        with self._conn:
            self._conn.execute(
                "INSERT INTO workbench_change_sets"
                "(run_id,task_id,source_root,stage_root,baseline_json,state,"
                "created_at,updated_at) VALUES(?,?,?,?,?,'open',?,?)",
                (
                    run_id, task_id, str(source), str(stage),
                    json.dumps(baseline, sort_keys=True), now, now,
                ),
            )
        self.add_event(
            task_id=task_id,
            run_id=run_id,
            kind="change_set_created",
            data={
                "files": len(baseline),
                "bytes": total_bytes,
                "excluded_secret_files": excluded_secret_files,
                "excluded_large_files": excluded_large_files,
            },
            event_id=f"run:{run_id}:change-set:created",
        )
        value = self.change_set(task_id)
        assert value is not None
        return value

    def change_set(self, task_id: str) -> ChangeSet | None:
        row = self._conn.execute(
            "SELECT task_id,run_id,source_root,stage_root,baseline_json,state,"
            "created_at,updated_at FROM workbench_change_sets WHERE task_id=?",
            (task_id,),
        ).fetchone()
        if row is None:
            return None
        source = Path(row[2]).resolve()
        stage = Path(row[3]).resolve()
        task = self.execution.get_task(task_id)
        if task is None or source != Path(task.workspace).resolve():
            raise WorkspacePolicyError(
                "persisted ChangeSet source does not match its Task workspace",
            )
        if not stage.is_relative_to(self.runs_path):
            raise WorkspacePolicyError("persisted ChangeSet stage escapes workbench home")
        baseline = json.loads(row[4])
        if not isinstance(baseline, dict):
            raise TypeError("ChangeSet baseline must be a mapping")
        return ChangeSet(
            task_id=row[0], run_id=row[1], source_root=str(source),
            stage_root=str(stage), baseline=baseline, state=row[5],
            created_at=row[6], updated_at=row[7],
        )

    def prepare_change_set(self, task_id: str) -> ChangeSet | None:
        value = self.change_set(task_id)
        if value is None or value.state != "open":
            return value
        now = time.time()
        with self._conn:
            self._conn.execute(
                "UPDATE workbench_change_sets SET state='ready',updated_at=? "
                "WHERE task_id=? AND state='open'",
                (now, task_id),
            )
        paths = self.change_set_paths(task_id)
        self.add_event(
            task_id=task_id,
            run_id=value.run_id,
            kind="change_set_ready",
            data={"changed_paths": list(paths)},
            event_id=f"run:{value.run_id}:change-set:ready",
        )
        return self.change_set(task_id)

    def change_set_paths(self, task_id: str) -> tuple[str, ...]:
        value = self.change_set(task_id)
        if value is None:
            return ()
        stage_manifest = _stage_manifest(Path(value.stage_root))
        return tuple(sorted(
            relative
            for relative in set(value.baseline) | set(stage_manifest)
            if value.baseline.get(relative) != stage_manifest.get(relative)
        ))

    def change_set_diff(self, task_id: str) -> str:
        value = self.change_set(task_id)
        if value is None:
            return ""
        if value.state == "discarded":
            raise WorkspacePolicyError("discarded ChangeSet has no staged diff")
        source = Path(value.source_root)
        stage = Path(value.stage_root)
        chunks: list[str] = []
        for relative in self.change_set_paths(task_id):
            before_path = source / relative
            after_path = stage / relative
            before = before_path.read_bytes() if before_path.is_file() else b""
            after = after_path.read_bytes() if after_path.is_file() else b""
            try:
                before_text = before.decode("utf-8").splitlines(keepends=True)
                after_text = after.decode("utf-8").splitlines(keepends=True)
            except UnicodeDecodeError:
                chunks.append(
                    f"Binary change: {relative} ({len(before)} -> {len(after)} bytes)\n",
                )
                continue
            chunks.extend(difflib.unified_diff(
                before_text,
                after_text,
                fromfile=f"a/{relative}" if before else "/dev/null",
                tofile=f"b/{relative}" if after else "/dev/null",
            ))
        return _redact_text("".join(chunks)[-_CHANGESET_MAX_DIFF_BYTES:])

    def apply_change_set(self, task_id: str) -> tuple[str, ...]:
        value = self.change_set(task_id)
        if value is None:
            raise ValueError(f"task {task_id!r} has no ChangeSet")
        if value.state == "discarded":
            raise WorkspacePolicyError("discarded ChangeSet cannot be applied")
        run = self.execution.get_run(value.run_id)
        if run is None or run.state.value != "completed":
            raise WorkspacePolicyError(
                "ChangeSet can be applied only after its Run completes",
            )
        if value.state not in {"ready", "applied"}:
            raise WorkspacePolicyError(
                "ChangeSet is not ready; finish the task before applying it",
            )
        paths = self.change_set_paths(task_id)
        source = Path(value.source_root)
        stage = Path(value.stage_root)
        stage_manifest = _stage_manifest(stage)

        # Preflight every destination before changing any file. A destination
        # may equal either its baseline or desired state, which makes a retry
        # after process interruption idempotent.
        for relative in paths:
            target = _contained_change_path(source, relative)
            current = _optional_file_sha256(target)
            baseline = value.baseline.get(relative)
            desired = stage_manifest.get(relative)
            if current not in {baseline, desired}:
                raise WorkspacePolicyError(
                    f"workspace drift conflicts with ChangeSet path: {relative}",
                )

        for relative in paths:
            target = _contained_change_path(source, relative)
            desired = stage_manifest.get(relative)
            if _optional_file_sha256(target) == desired:
                continue
            if desired is None:
                target.unlink(missing_ok=True)
                continue
            staged = _contained_change_path(stage, relative)
            data = staged.read_bytes()
            target.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as handle:
                temporary = Path(handle.name)
                handle.write(data)
            try:
                os.replace(temporary, target)
                target.chmod(staged.stat().st_mode & 0o777)
            finally:
                temporary.unlink(missing_ok=True)
        now = time.time()
        with self._conn:
            self._conn.execute(
                "UPDATE workbench_change_sets SET state='applied',updated_at=? "
                "WHERE task_id=? AND state!='discarded'",
                (now, task_id),
            )
        self.add_event(
            task_id=task_id,
            run_id=value.run_id,
            kind="change_set_applied",
            data={"changed_paths": list(paths)},
            event_id=f"run:{value.run_id}:change-set:applied",
        )
        self._update_report_change_set_state(task_id, "applied")
        return paths

    def discard_change_set(self, task_id: str) -> None:
        value = self.change_set(task_id)
        if value is None:
            raise ValueError(f"task {task_id!r} has no ChangeSet")
        if value.state == "applied":
            raise WorkspacePolicyError("applied ChangeSet cannot be discarded")
        run = self.execution.get_run(value.run_id)
        if run is None or run.state.value in {"pending", "running", "waiting"}:
            raise WorkspacePolicyError(
                "active ChangeSet cannot be discarded; cancel or finish its Run first",
            )
        shutil.rmtree(value.stage_root, ignore_errors=True)
        now = time.time()
        with self._conn:
            self._conn.execute(
                "UPDATE workbench_change_sets SET state='discarded',updated_at=? "
                "WHERE task_id=? AND state!='applied'",
                (now, task_id),
            )
        self.add_event(
            task_id=task_id,
            run_id=value.run_id,
            kind="change_set_discarded",
            data={},
            event_id=f"run:{value.run_id}:change-set:discarded",
        )
        self._update_report_change_set_state(task_id, "discarded")

    def _update_report_change_set_state(self, task_id: str, state: str) -> None:
        row = self._conn.execute(
            "SELECT report_json FROM workbench_reports WHERE task_id=?",
            (task_id,),
        ).fetchone()
        if row is None:
            return
        report = json.loads(row[0])
        risks = [
            value for value in report.get("unresolved_risks", ())
            if not str(value).startswith("change set is ")
        ]
        if state == "discarded":
            risks.append("change set was discarded; original workspace is unchanged")
        report["unresolved_risks"] = risks
        report["change_set_state"] = state
        with self._conn:
            self._conn.execute(
                "UPDATE workbench_reports SET report_json=? WHERE task_id=?",
                (json.dumps(report, sort_keys=True), task_id),
            )

    def claims_path_for_run(self, run_id: str) -> Path:
        """Return the stable isolated Effect/Claim session for one Run.

        A checkpoint created before this mapping existed is bound to the
        legacy shared ``claims.db`` store_id, so it must continue there. New
        Runs receive separate sessions and can execute concurrently without
        sharing a single-writer Claim sequence or budget projection.
        """
        run = self.execution.get_run(run_id)
        if run is None:
            raise KeyError(run_id)
        row = self._conn.execute(
            "SELECT claims_path FROM workbench_run_sessions WHERE run_id=?",
            (run_id,),
        ).fetchone()
        if row is not None:
            return self._resolve_claims_path(row[0])
        path = (
            self.claims_path
            if self.execution.get_checkpoint(run_id) is not None
            else self.runs_path / run_id / "claims.db"
        ).resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn:
            self._conn.execute(
                "INSERT OR IGNORE INTO workbench_run_sessions"
                "(run_id,claims_path,created_at) VALUES(?,?,?)",
                (run_id, path.relative_to(self.home).as_posix(), time.time()),
            )
        row = self._conn.execute(
            "SELECT claims_path FROM workbench_run_sessions WHERE run_id=?",
            (run_id,),
        ).fetchone()
        assert row is not None
        return self._resolve_claims_path(row[0])

    def _resolve_claims_path(self, stored: str) -> Path:
        raw = Path(stored)
        path = (raw if raw.is_absolute() else self.home / raw).resolve()
        if path != self.claims_path and not path.is_relative_to(self.runs_path):
            raise WorkspacePolicyError(
                "persisted Run claim session escapes the workbench home",
            )
        return path

    def list_tasks(self) -> tuple[Task, ...]:
        return self.execution.list_tasks()

    def approvals(self, *, pending_only: bool = False) -> tuple[Approval, ...]:
        from .execution import InterruptState
        state = InterruptState.PENDING if pending_only else None
        return tuple(
            Approval.from_interrupt(value)
            for value in self.execution.list_interrupts(state=state)
        )

    def resolve_approval(
        self,
        approval_id: str,
        *,
        allow: bool,
        response: Any = None,
    ) -> Approval:
        interrupt = self.execution.resolve_interrupt(
            approval_id, allow=allow, response=response,
        )
        run = self.execution.get_run(interrupt.run_id)
        if run is None:
            raise KeyError(interrupt.run_id)
        self.add_event(
            task_id=run.task_id,
            run_id=run.id,
            kind="approval_resolved",
            data={"approval_id": approval_id, "allowed": allow},
            event_id=f"approval:{approval_id}:resolved",
        )
        return Approval.from_interrupt(interrupt)

    def record_approval_required(self, interrupt: Interrupt) -> RunEvent:
        run = self.execution.get_run(interrupt.run_id)
        if run is None:
            raise KeyError(interrupt.run_id)
        return self.add_event(
            task_id=run.task_id,
            run_id=run.id,
            kind="approval_required",
            data={
                "approval_id": interrupt.id,
                "request": dict(interrupt.request),
            },
            event_id=f"approval:{interrupt.id}:required",
        )

    def record_run_state(self, run_id: str) -> RunEvent:
        run = self.execution.get_run(run_id)
        if run is None:
            raise KeyError(run_id)
        return self.add_event(
            task_id=run.task_id,
            run_id=run.id,
            kind="run_state",
            data={"state": run.state.value, "attempt": run.attempt},
            event_id=f"run:{run.id}:state:{run.state.value}:{run.attempt}",
        )

    def add_event(
        self,
        *,
        task_id: str,
        run_id: str,
        kind: str,
        data: Mapping[str, Any],
        event_id: str | None = None,
    ) -> RunEvent:
        event = RunEvent(
            id=event_id or f"event_{uuid.uuid4().hex}",
            task_id=task_id,
            run_id=run_id,
            kind=kind,
            data=dict(data),
            created_at=time.time(),
        )
        with self._conn:
            self._conn.execute(
                "INSERT OR IGNORE INTO workbench_events"
                "(id,task_id,run_id,kind,data_json,created_at) VALUES(?,?,?,?,?,?)",
                (
                    event.id, event.task_id, event.run_id, event.kind,
                    json.dumps(dict(event.data), sort_keys=True), event.created_at,
                ),
            )
        row = self._conn.execute(
            "SELECT id,task_id,run_id,kind,data_json,created_at "
            "FROM workbench_events WHERE id=?",
            (event.id,),
        ).fetchone()
        assert row is not None
        return RunEvent(
            id=row[0], task_id=row[1], run_id=row[2], kind=row[3],
            data=json.loads(row[4]), created_at=row[5],
        )

    def events(self, task_id: str) -> tuple[RunEvent, ...]:
        return tuple(
            RunEvent(
                id=row[0], task_id=row[1], run_id=row[2], kind=row[3],
                data=json.loads(row[4]), created_at=row[5],
            )
            for row in self._conn.execute(
                "SELECT id,task_id,run_id,kind,data_json,created_at "
                "FROM workbench_events WHERE task_id=? ORDER BY created_at,id",
                (task_id,),
            )
        )

    def add_artifact(
        self,
        *,
        task_id: str,
        run_id: str,
        kind: str,
        path: str | None = None,
        sha256: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> Artifact:
        artifact = Artifact(
            id=f"artifact_{uuid.uuid4().hex}",
            task_id=task_id,
            run_id=run_id,
            kind=kind,
            path=path,
            sha256=sha256,
            metadata=dict(metadata or {}),
            created_at=time.time(),
        )
        encoded = json.dumps(dict(artifact.metadata), sort_keys=True)
        with self._conn:
            self._conn.execute(
                "INSERT INTO workbench_artifacts"
                "(id,task_id,run_id,kind,path,sha256,metadata_json,created_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (
                    artifact.id, artifact.task_id, artifact.run_id,
                    artifact.kind, artifact.path, artifact.sha256, encoded,
                    artifact.created_at,
                ),
            )
        self.add_event(
            task_id=task_id,
            run_id=run_id,
            kind=(
                "verification_recorded" if kind == "verification"
                else "artifact_created"
            ),
            data={"artifact_id": artifact.id, "kind": kind, "path": path},
            event_id=f"artifact:{artifact.id}:created",
        )
        return artifact

    def artifacts(self, task_id: str) -> tuple[Artifact, ...]:
        return tuple(
            Artifact(
                id=row[0], task_id=row[1], run_id=row[2], kind=row[3],
                path=row[4], sha256=row[5], metadata=json.loads(row[6]),
                created_at=row[7],
            )
            for row in self._conn.execute(
                "SELECT id,task_id,run_id,kind,path,sha256,metadata_json,created_at "
                "FROM workbench_artifacts WHERE task_id=? ORDER BY created_at,id",
                (task_id,),
            )
        )

    def workspace_tools(self, task_id: str, run_id: str) -> tuple[Tool, ...]:
        task = self.execution.get_task(task_id)
        if task is None:
            raise KeyError(task_id)
        change_set = self.change_set(task_id)
        workspace_root = Path(
            change_set.stage_root if change_set is not None else task.workspace,
        )
        capabilities = _WorkspaceCapabilities(
            workspace_root,
            sandbox=self.command_sandbox,
            git_status_provider=(
                (lambda: "\n".join(
                    f" M {path}" for path in self.change_set_paths(task_id)
                )) if change_set is not None else None
            ),
            git_diff_provider=(
                (lambda: self.change_set_diff(task_id))
                if change_set is not None else None
            ),
            evidence=lambda **values: self.add_artifact(
                task_id=task_id, run_id=run_id, **values,
            ),
        )
        return capabilities.tools()

    def approval_policy(
        self, task_id: str,
    ) -> Callable[[Tool, Mapping[str, Any]], Mapping[str, Any] | None]:
        """Return policy for direct or staged workspace execution."""
        staged = self.change_set(task_id) is not None

        def decide(
            tool_value: Tool,
            arguments: Mapping[str, Any],
        ) -> Mapping[str, Any] | None:
            if staged and tool_value.name == "write_workspace_file":
                SecretPolicy().check_tool_arguments(tool_value.name, arguments)
                return None
            return workbench_approval_policy(tool_value, arguments)

        return decide

    def build_report(self, task_id: str, result: FinalResult | None = None) -> TaskReport:
        task = self.execution.get_task(task_id)
        if task is None:
            raise KeyError(task_id)
        runs = self.execution.list_runs(task_id=task_id)
        if not runs:
            raise ValueError(f"task {task_id!r} has no run")
        run = runs[0]
        artifacts = self.artifacts(task_id)
        verifications = tuple(
            Verification(
                command=tuple(value.metadata.get("argv", ())),
                exit_code=value.metadata.get("exit_code"),
                timed_out=bool(value.metadata.get("timed_out", False)),
                stdout=str(value.metadata.get("stdout", "")),
                stderr=str(value.metadata.get("stderr", "")),
                created_at=value.created_at,
                sandbox=str(value.metadata.get("sandbox", "unknown")),
                isolated=bool(value.metadata.get("isolated", False)),
                network_isolated=bool(value.metadata.get("network_isolated", False)),
            )
            for value in artifacts if value.kind == "verification"
        )
        change_set = self.prepare_change_set(task_id)
        changed_files = (
            self.change_set_paths(task_id)
            if change_set is not None
            else tuple(sorted({
                value.path for value in artifacts
                if value.kind == "file_write" and value.path is not None
            }))
        )
        diff = (
            self.change_set_diff(task_id)
            if change_set is not None
            else _git_capture(Path(task.workspace), ["diff", "--no-ext-diff"])
        )
        risks: list[str] = []
        verified = bool(verifications) and all(value.passed for value in verifications)
        if not verified:
            risks.append("task has no complete successful verification evidence")
        if any(value.timed_out for value in verifications):
            risks.append("one or more verification commands timed out")
        if any(value.exit_code not in (0, None) for value in verifications):
            risks.append("one or more verification commands failed")
        if any(not value.isolated for value in verifications):
            risks.append("verification ran without an OS isolation boundary")
        if any(not value.network_isolated for value in verifications):
            risks.append("verification ran without network egress isolation")
        if run.state.value != "completed":
            risks.append(f"run ended in state {run.state.value}")
        if change_set is not None and change_set.state != "applied":
            risks.append(
                f"change set is {change_set.state}; original workspace is unchanged",
            )
        final_text = result.text if result is not None else ""
        report = TaskReport(
            task_id=task.id,
            run_id=run.id,
            status=run.state.value,
            final_text=final_text,
            changed_files=changed_files,
            artifacts=artifacts,
            verifications=verifications,
            verified=verified,
            diff=diff,
            unresolved_risks=tuple(risks),
            change_set_state=change_set.state if change_set is not None else None,
            created_at=time.time(),
        )
        with self._conn:
            self._conn.execute(
                "INSERT INTO workbench_reports(task_id,run_id,report_json,created_at) "
                "VALUES(?,?,?,?) ON CONFLICT(task_id) DO UPDATE SET "
                "run_id=excluded.run_id,report_json=excluded.report_json,"
                "created_at=excluded.created_at",
                (task.id, run.id, json.dumps(report.as_dict(), sort_keys=True), report.created_at),
            )
        self.add_event(
            task_id=task.id,
            run_id=run.id,
            kind="report_created",
            data={"status": report.status, "verified": report.verified},
        )
        return report

    def get_report(self, task_id: str) -> Mapping[str, Any] | None:
        row = self._conn.execute(
            "SELECT report_json FROM workbench_reports WHERE task_id=?",
            (task_id,),
        ).fetchone()
        return json.loads(row[0]) if row is not None else None


class _WorkspaceCapabilities:
    _MAX_FILE_BYTES = 1_000_000
    _MAX_OUTPUT_BYTES = 64_000
    _MAX_FILES = 500
    _MAX_TIMEOUT = 300
    _COMMANDS = {
        "cargo", "cmake", "ctest", "dart", "flutter", "go", "make",
        "mypy", "npm", "pnpm", "pyright", "pytest", "python", "python3",
        "ruff", "yarn",
    }

    def __init__(
        self,
        root: Path,
        *,
        sandbox: CommandSandbox,
        evidence: Callable[..., Artifact],
        git_status_provider: Callable[[], str] | None = None,
        git_diff_provider: Callable[[], str] | None = None,
    ) -> None:
        self.root = root.expanduser().resolve()
        self.sandbox = sandbox
        self._evidence = evidence
        self._git_status_provider = git_status_provider
        self._git_diff_provider = git_diff_provider

    def _path(self, relative_path: str) -> Path:
        raw = Path(relative_path)
        if raw.is_absolute():
            raise WorkspacePolicyError("absolute workspace paths are denied")
        candidate = (self.root / raw).resolve(strict=False)
        if not candidate.is_relative_to(self.root):
            raise WorkspacePolicyError("path escapes the selected workspace")
        relative = candidate.relative_to(self.root)
        if relative.parts and relative.parts[0] == ".git":
            raise WorkspacePolicyError("direct access to .git internals is denied")
        if _sensitive_path(relative):
            raise WorkspacePolicyError("access to likely secret material is denied")
        return candidate

    def tools(self) -> tuple[Tool, ...]:
        owner = self

        @tool(side_effect="read_only")
        def list_workspace_files(relative_path: str = ".") -> list[str]:
            """List ordinary files below a workspace-relative directory."""
            base = owner._path(relative_path)
            if not base.is_dir():
                raise WorkspacePolicyError(f"not a directory: {relative_path}")
            files: list[str] = []
            for path in sorted(base.rglob("*")):
                if ".git" in path.relative_to(owner.root).parts:
                    continue
                if _sensitive_path(path.relative_to(owner.root)):
                    continue
                if path.is_file():
                    files.append(path.relative_to(owner.root).as_posix())
                    if len(files) >= owner._MAX_FILES:
                        break
            return files

        @tool(side_effect="read_only")
        def read_workspace_file(relative_path: str) -> str:
            """Read one UTF-8 text file inside the selected workspace."""
            path = owner._path(relative_path)
            if not path.is_file():
                raise WorkspacePolicyError(f"not a file: {relative_path}")
            data = path.read_bytes()
            if len(data) > owner._MAX_FILE_BYTES:
                raise WorkspacePolicyError("file exceeds the 1 MB read limit")
            try:
                return _redact_text(data.decode("utf-8"))
            except UnicodeDecodeError as exc:
                raise WorkspacePolicyError("binary/non-UTF-8 files are not readable") from exc

        @tool(side_effect="idempotent_write")
        async def write_workspace_file(
            relative_path: str, content: str,
        ) -> dict[str, object]:
            """Atomically replace one UTF-8 file inside the selected workspace."""
            encoded = content.encode("utf-8")
            if len(encoded) > owner._MAX_FILE_BYTES:
                raise WorkspacePolicyError("file exceeds the 1 MB write limit")
            path = owner._path(relative_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            before = path.read_bytes() if path.is_file() else None
            before_hash = hashlib.sha256(before).hexdigest() if before is not None else None
            with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
                temporary = Path(handle.name)
                handle.write(encoded)
            try:
                os.replace(temporary, path)
            finally:
                temporary.unlink(missing_ok=True)
            after_hash = hashlib.sha256(encoded).hexdigest()
            relative = path.relative_to(owner.root).as_posix()
            owner._evidence(
                kind="file_write", path=relative, sha256=after_hash,
                metadata={"before_sha256": before_hash, "bytes": len(encoded)},
            )
            return {"path": relative, "sha256": after_hash, "bytes": len(encoded)}

        @tool(side_effect="external_write")
        async def run_workspace_command(
            argv: list[str], timeout_seconds: int = 120,
        ) -> dict[str, object]:
            """Run an approved, bounded verification command in the workspace."""
            return await owner._run_command(argv, timeout_seconds)

        @tool(side_effect="read_only")
        def git_status() -> str:
            """Show concise Git working-tree status without changing it."""
            if owner._git_status_provider is not None:
                return owner._git_status_provider()
            return _git_capture(owner.root, ["status", "--short"])

        @tool(side_effect="read_only")
        def git_diff() -> str:
            """Show the current Git working-tree diff without changing it."""
            if owner._git_diff_provider is not None:
                return owner._git_diff_provider()
            return _git_capture(owner.root, ["diff", "--no-ext-diff"])

        return (
            list_workspace_files, read_workspace_file, write_workspace_file,
            run_workspace_command, git_status, git_diff,
        )

    async def _run_command(
        self, argv: Sequence[str], timeout_seconds: int,
    ) -> dict[str, object]:
        if not argv or not all(isinstance(value, str) and value for value in argv):
            raise WorkspacePolicyError("argv must contain non-empty strings")
        executable = argv[0]
        if Path(executable).name != executable or executable not in self._COMMANDS:
            raise WorkspacePolicyError(f"command is not allowed: {executable}")
        if executable in {"python", "python3"}:
            if "-c" in argv or (len(argv) > 1 and argv[1] not in {"-m"}):
                raise WorkspacePolicyError("Python commands must use an approved -m module")
            if len(argv) < 3 or argv[2] not in {"compileall", "pytest", "unittest"}:
                raise WorkspacePolicyError("Python module is not allowed")
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, int)
            or timeout_seconds <= 0
        ):
            raise WorkspacePolicyError("timeout_seconds must be a positive integer")
        timeout = min(timeout_seconds, self._MAX_TIMEOUT)
        environment = {
            key: value for key, value in os.environ.items()
            if key in {"CI", "HOME", "LANG", "LC_ALL", "PATH", "TERM", "TMPDIR"}
        }
        result = await self.sandbox.run(
            argv,
            workspace=self.root,
            environment=environment,
            timeout_s=timeout,
        )
        stdout = _redact_text(result.stdout[-self._MAX_OUTPUT_BYTES:])
        stderr = _redact_text(result.stderr[-self._MAX_OUTPUT_BYTES:])
        metadata: dict[str, Any] = {
            "argv": list(result.argv), "exit_code": result.exit_code,
            "timed_out": result.timed_out, "stdout": stdout, "stderr": stderr,
            "duration_seconds": result.duration_seconds,
            "sandbox": result.sandbox,
            "isolated": result.isolated,
            "network_isolated": result.network_isolated,
        }
        self._evidence(kind="verification", metadata=metadata)
        return metadata


_CHANGESET_MAX_FILES = 20_000
_CHANGESET_MAX_BYTES = 256 * 1024 * 1024
_CHANGESET_MAX_FILE_BYTES = 10 * 1024 * 1024
_CHANGESET_MAX_DIFF_BYTES = 1_000_000
_CHANGESET_IGNORED_PARTS = frozenset({
    ".git", ".mypy_cache", ".pytest_cache", ".ruff_cache", "__pycache__",
})


def _snapshot_source_files(root: Path) -> tuple[tuple[str, Path], ...]:
    completed = subprocess.run(
        ["git", "ls-files", "-co", "--exclude-standard", "-z"],
        cwd=root,
        capture_output=True,
        timeout=30,
        check=False,
        env={
            key: value for key, value in os.environ.items()
            if key in {"HOME", "LANG", "LC_ALL", "PATH", "TMPDIR"}
        },
    )
    if completed.returncode == 0:
        candidates = (
            Path(value.decode("utf-8", errors="surrogateescape"))
            for value in completed.stdout.split(b"\0") if value
        )
    else:
        candidates = (path.relative_to(root) for path in root.rglob("*"))
    found: list[tuple[str, Path]] = []
    seen: set[str] = set()
    for relative in candidates:
        if (
            relative.is_absolute()
            or any(part in _CHANGESET_IGNORED_PARTS for part in relative.parts)
            or _sensitive_path(relative)
        ):
            continue
        raw_source = root / relative
        if raw_source.is_symlink():
            continue
        source = raw_source.resolve(strict=False)
        if (
            not source.is_relative_to(root)
            or not source.is_file()
        ):
            continue
        name = relative.as_posix()
        if name not in seen:
            found.append((name, source))
            seen.add(name)
    return tuple(sorted(found))


def _stage_manifest(root: Path) -> dict[str, str]:
    manifest: dict[str, str] = {}
    total = 0
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if any(part in _CHANGESET_IGNORED_PARTS for part in relative.parts):
            continue
        if path.is_symlink():
            raise WorkspacePolicyError(
                f"ChangeSet symlinks are not supported: {relative.as_posix()}",
            )
        if not path.is_file() or _sensitive_path(relative):
            continue
        if path.stat().st_size > _CHANGESET_MAX_FILE_BYTES:
            raise WorkspacePolicyError(
                f"staged file exceeds the per-file limit: {relative.as_posix()}",
            )
        if _snapshot_file_contains_secret(path, relative.as_posix()):
            raise WorkspacePolicyError(
                f"staged ChangeSet contains potential secret material: "
                f"{relative.as_posix()}",
            )
        total += path.stat().st_size
        if len(manifest) >= _CHANGESET_MAX_FILES or total > _CHANGESET_MAX_BYTES:
            raise WorkspacePolicyError(
                "staged ChangeSet exceeds the file/size limit",
            )
        manifest[relative.as_posix()] = _file_sha256(path)
    return manifest


def _snapshot_file_contains_secret(path: Path, relative: str) -> bool:
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return False
    try:
        SecretPolicy().check(text, path=f"snapshot:{relative}")
    except SecretDetected:
        return True
    return False


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _optional_file_sha256(path: Path) -> str | None:
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file():
        return "unsupported"
    return _file_sha256(path)


def _contained_change_path(root: Path, relative: str) -> Path:
    raw = Path(relative)
    if raw.is_absolute():
        raise WorkspacePolicyError("absolute ChangeSet paths are denied")
    candidate = (root / raw).resolve(strict=False)
    if not candidate.is_relative_to(root) or _sensitive_path(raw):
        raise WorkspacePolicyError(f"unsafe ChangeSet path: {relative}")
    return candidate


_SECRET_ASSIGNMENT = re.compile(
    r"(?im)^(\s*(?:api[_-]?key|access[_-]?token|auth[_-]?token|client[_-]?secret|"
    r"password|private[_-]?key|secret)[\w.-]*\s*[:=]\s*).+$",
)
_BEARER_TOKEN = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{12,}")
_COMMON_TOKEN = re.compile(r"\b(?:sk-[A-Za-z0-9_-]{12,}|AKIA[A-Z0-9]{16})\b")
_PRIVATE_KEY = re.compile(
    r"-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----",
    re.DOTALL,
)


def _sensitive_path(relative: Path) -> bool:
    name = relative.name.lower()
    return (
        name == ".env" or name.startswith(".env.")
        or name in {"credentials.json", "id_dsa", "id_ed25519", "id_rsa"}
        or relative.suffix.lower() in {".key", ".p12", ".pem"}
    )


def _redact_text(value: str) -> str:
    value = _PRIVATE_KEY.sub("[REDACTED PRIVATE KEY]", value)
    value = _SECRET_ASSIGNMENT.sub(r"\1[REDACTED]", value)
    value = _BEARER_TOKEN.sub("Bearer [REDACTED]", value)
    return _COMMON_TOKEN.sub("[REDACTED TOKEN]", value)


def _git_capture(root: Path, arguments: Sequence[str]) -> str:
    try:
        completed = subprocess.run(
            ["git", *arguments], cwd=root, capture_output=True, text=True,
            timeout=30, check=False,
            env={
                key: value for key, value in os.environ.items()
                if key in {"HOME", "LANG", "LC_ALL", "PATH", "TMPDIR"}
            },
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"git unavailable: {type(exc).__name__}: {exc}"
    output = completed.stdout if completed.returncode == 0 else completed.stderr
    return _redact_text(output[-64_000:])
