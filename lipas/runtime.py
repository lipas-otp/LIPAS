"""Composition root for the LIPAS execution and product surfaces."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .conversation_store import SQLiteSessionStore
from .lint import lint_store
from .operations import OperationJournal
from .orchestration import Mailbox
from .rows import RowSet
from .session import open_session
from .workbench import Artifact, Workbench
from .workspace_storage import (
    WORKSPACE_SCHEMA_VERSION,
    RuntimeStorageIssue,
    WorkspaceStorage,
)

__all__ = [
    "ArtifactRepository",
    "LIPASRuntime",
    "RuntimeAuditReport",
    "RuntimeClaimIssue",
]


def _close_all(resources: tuple[Any, ...]) -> BaseException | None:
    """Close every resource and return the first failure, if any.

    Cleanup must not stop at the first broken backend: the remaining SQLite
    connections may otherwise retain locks and make recovery harder.  Callers
    decide whether a cleanup error should be raised or suppressed in favour of
    an exception that is already in flight.
    """
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


class ArtifactRepository:
    """Narrow artifact boundary backed by the runtime's Workbench."""

    def __init__(self, workbench: Workbench) -> None:
        self._workbench = workbench

    def add(
        self,
        *,
        task_id: str,
        run_id: str,
        kind: str,
        path: str | Path | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> Artifact:
        return self._workbench.add_artifact(
            task_id=task_id,
            run_id=run_id,
            kind=kind,
            path=None if path is None else str(path),
            metadata=metadata,
        )

    def list(self, task_id: str) -> tuple[Artifact, ...]:
        return self._workbench.artifacts(task_id)


@dataclass(frozen=True, slots=True)
class RuntimeClaimIssue:
    """One scoped Claim lint/open failure from global or Run evidence."""

    scope: str
    path: Path
    issue: Any

    def __str__(self) -> str:
        return f"{self.scope} ({self.path}): {self.issue}"


@dataclass(frozen=True, slots=True)
class RuntimeAuditReport:
    claim_issues: tuple[RuntimeClaimIssue, ...]
    storage_issues: tuple[RuntimeStorageIssue, ...] = ()
    execution_events_repaired: int = 0
    operation_events_repaired: int = 0
    handoff_events_repaired: int = 0
    workspace_schema_version: int = WORKSPACE_SCHEMA_VERSION

    @property
    def healthy(self) -> bool:
        return not self.claim_issues and not any(
            issue.severity == "error" for issue in self.storage_issues
        )


class LIPASRuntime:
    """Own all stores needed by one local LIPAS deployment.

    Schema v2 physically consolidates compatible global tables in
    ``workspace.db``. Per-Run Claim/Effect sessions remain isolated under
    ``runs/<run_id>`` so budget and evidence projections never bleed between
    Runs. Control state remains authoritative in ``ExecutionStore``.
    """

    def __init__(self, home: str | Path, *, sandbox: str = "auto") -> None:
        self.home = Path(home).expanduser().resolve()
        self.storage = WorkspaceStorage(self.home)
        preflight = self.storage.inspect()
        if preflight.state not in {"current", "uninitialized"}:
            # Preserve the actionable schema/migration exception without
            # creating a runtime lock in a legacy or invalid workspace.
            self.storage.require_current(create=True)
        initializing = preflight.state == "uninitialized"
        self._workspace_lease = self.storage.acquire_runtime_lease(
            exclusive=initializing,
        )
        try:
            self.database_path = self.storage.require_current(create=True)
            self.claims_path = self.database_path
            self.operations_path = self.database_path
            self.claims: RowSet = open_session(self.claims_path)
            self.workbench = Workbench(
                self.home,
                rowset=self.claims,
                sandbox=sandbox,
                database_path=self.database_path,
            )
            self.operations = OperationJournal(
                self.operations_path,
                rowset=self.claims,
            )
            self.handoffs = Mailbox(self.database_path, rowset=self.claims)
            self.sessions = SQLiteSessionStore(self.database_path)
            self.artifacts = ArtifactRepository(self.workbench)
            # Workbench owns one mutable evidence attachment. Serialize the
            # convenience durable entry points so concurrent callers cannot
            # close one another's ExecutionStore mid-Run.
            self._durable_lock = asyncio.Lock()
            if initializing:
                self._workspace_lease.make_shared()
        except BaseException:
            _close_all((
                getattr(self, "sessions", None),
                getattr(self, "handoffs", None),
                getattr(self, "operations", None),
                getattr(self, "workbench", None),
                getattr(getattr(self, "claims", None), "store", None),
                self._workspace_lease,
            ))
            raise
        self._closed = False

    @property
    def execution(self):
        """The current authoritative store owned by the Workbench.

        Workbench may reattach its audit projection to a Run-local evidence
        tape. Keeping this as a property prevents callers retaining the closed
        pre-attachment store.
        """
        return self.workbench.execution

    @classmethod
    def open(
        cls,
        path: str | Path,
        *,
        sandbox: str = "auto",
    ) -> "LIPASRuntime":
        return cls(path, sandbox=sandbox)

    def claims_for_run(self, run_id: str) -> RowSet:
        """Open the isolated evidence tape assigned to a Workbench Run."""
        return open_session(self.workbench.claims_path_for_run(run_id))

    def agent_for_run(self, run_id: str, *, adapter: Any, **kwargs: Any):
        """Build an Agent bound to this runtime's isolated Run evidence."""
        if "session_path" in kwargs or "session" in kwargs:
            raise ValueError(
                "agent_for_run owns session_path; do not pass another store",
            )
        if self.execution.get_run(run_id) is None:
            raise KeyError(run_id)
        from .agent import Agent
        return Agent(
            adapter=adapter,
            session_path=self.workbench.claims_path_for_run(run_id),
            **kwargs,
        )

    async def run_durable(
        self,
        agent: Any,
        prompt: Any,
        *,
        run_id: str,
        **kwargs: Any,
    ):
        """Run without making the application pass this runtime's store."""
        if "execution_store" in kwargs:
            raise ValueError("LIPASRuntime owns execution_store")
        async with self._durable_lock:
            return await self._invoke_durable(
                agent, prompt=prompt, run_id=run_id, resume=False, kwargs=kwargs,
            )

    async def _invoke_durable(
        self,
        agent: Any,
        *,
        prompt: Any,
        run_id: str,
        resume: bool,
        kwargs: Mapping[str, Any],
    ):
        rowset = getattr(agent, "rowset", None)
        if rowset is None:
            raise TypeError("runtime durable execution requires an Agent rowset")
        self.workbench.attach_rowset(rowset, run_id=run_id)
        try:
            if resume:
                result = await agent.resume_durable(
                    execution_store=self.execution,
                    run_id=run_id,
                    **kwargs,
                )
            else:
                result = await agent.run_durable(
                    prompt,
                    execution_store=self.execution,
                    run_id=run_id,
                    **kwargs,
                )
        except BaseException:
            try:
                self.workbench.attach_global_rowset(self.claims)
            except BaseException:
                # Preserve RunSuspended, cancellation, and execution failures.
                # The caller can still close the runtime to release resources.
                pass
            raise
        else:
            self.workbench.attach_global_rowset(self.claims)
            return result

    async def resume_durable(
        self,
        agent: Any,
        *,
        run_id: str,
        **kwargs: Any,
    ):
        if "execution_store" in kwargs:
            raise ValueError("LIPASRuntime owns execution_store")
        async with self._durable_lock:
            return await self._invoke_durable(
                agent, prompt=None, run_id=run_id, resume=True, kwargs=kwargs,
            )

    def audit(self, *, repair: bool = False) -> RuntimeAuditReport:
        """Lint evidence and optionally repair recoverable audit outboxes."""
        execution_repaired = self.execution.repair_audit() if repair else 0
        operation_repaired = self.operations.repair_audit() if repair else 0
        handoff_repaired = self.handoffs.repair_audit() if repair else 0
        claim_issues, run_execution_repaired = self._claim_audit(repair=repair)
        return RuntimeAuditReport(
            claim_issues=claim_issues,
            storage_issues=self.storage.audit(),
            execution_events_repaired=(
                execution_repaired + run_execution_repaired
            ),
            operation_events_repaired=operation_repaired,
            handoff_events_repaired=handoff_repaired,
        )

    def _claim_audit(
        self, *, repair: bool,
    ) -> tuple[tuple[RuntimeClaimIssue, ...], int]:
        issues = [
            RuntimeClaimIssue("global", self.claims_path, violation)
            for violation in lint_store(self.claims.store)
        ]
        execution_repaired = 0
        for run_id, path in self.workbench.claim_session_paths():
            if not path.is_file():
                run = self.execution.get_run(run_id)
                if run is not None and run.state.value != "pending":
                    issues.append(RuntimeClaimIssue(
                        f"run:{run_id}",
                        path,
                        "registered evidence tape is missing",
                    ))
                continue
            rowset = None
            try:
                rowset = open_session(path)
                if repair:
                    from .execution import ExecutionStore
                    with ExecutionStore(
                        self.database_path,
                        audit_run_id=run_id,
                    ) as execution:
                        execution.rowset = rowset
                        execution_repaired += execution.repair_audit()
                issues.extend(
                    RuntimeClaimIssue(f"run:{run_id}", path, violation)
                    for violation in lint_store(rowset.store)
                )
            except Exception as exc:
                issues.append(RuntimeClaimIssue(
                    f"run:{run_id}", path,
                    f"cannot lint evidence tape: {type(exc).__name__}: {exc}",
                ))
            finally:
                close = getattr(
                    None if rowset is None else rowset.store, "close", None,
                )
                if callable(close):
                    try:
                        close()
                    except Exception as exc:
                        issues.append(RuntimeClaimIssue(
                            f"run:{run_id}", path,
                            f"cannot close evidence tape: {type(exc).__name__}: {exc}",
                        ))
        return tuple(issues), execution_repaired

    def close(self) -> None:
        if self._closed:
            return
        error = _close_all((
            self.sessions,
            self.handoffs,
            self.operations,
            self.workbench,
            self.claims.store,
            getattr(self, "_workspace_lease", None),
        ))
        self._closed = True
        if error is not None:
            raise error

    def __enter__(self) -> "LIPASRuntime":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()
