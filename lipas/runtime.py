"""Composition root for the LIPAS execution and product surfaces."""
from __future__ import annotations

from dataclasses import dataclass
import asyncio
import hashlib
import copy
import time
import dataclasses
import json
import inspect
from collections.abc import Iterable, Mapping as ABCMapping
import math
from pathlib import Path
from typing import Any, Mapping

from .conversation_store import (
    Attachment,
    Conversation,
    ConversationEventPage,
    Message,
    SQLiteSessionStore,
)
from .lint import lint_store
from .operations import OperationJournal
from .orchestration import Mailbox
from .coordination_policy import WorkspacePolicyStore
from .rows import RowSet
from .session import open_session
from .workbench import Artifact, Workbench
from .workspace_storage import (
    WORKSPACE_SCHEMA_VERSION,
    RuntimeStorageIssue,
    WorkspaceStorage,
    WorkspaceMigrationRequired,
)
from .execution import ExecutionLeaseError, ExecutionStateError, Run, RunState, Task
from .effect import (
    EffectDecision,
    EffectObservation,
    EffectProposal,
    EffectTarget,
    LLMTarget,
    ToolTarget,
)

__all__ = [
    "ArtifactRepository",
    "AgentRuntime",
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


def _normalise_capabilities(
    value: Mapping[str, Any] | Iterable[str],
) -> set[str]:
    """Validate and copy host capability declarations.

    A string is technically iterable, but accepting one here would turn
    ``"email.send"`` into individual character capabilities and accidentally
    deny or (with a future policy change) admit the wrong effect.  Mappings
    intentionally use their keys; values are host metadata and are not
    interpreted as authority by this small admission façade.
    """
    if isinstance(value, (str, bytes, bytearray)):
        raise TypeError("available_capabilities must not be a string")
    if isinstance(value, ABCMapping):
        items: Iterable[Any] = value.keys()
    else:
        if not isinstance(value, Iterable):
            raise TypeError("available_capabilities must be a mapping or iterable")
        items = value
    capabilities: set[str] = set()
    for item in items:
        if not isinstance(item, str) or not item.strip():
            raise ValueError("available_capabilities must contain non-empty strings")
        normalized = item.strip()
        if normalized in capabilities:
            raise ValueError(
                "available_capabilities contains duplicate values after normalization",
            )
        capabilities.add(normalized)
    return capabilities


def _normalise_budget(
    value: Mapping[str, float] | None,
) -> dict[str, float] | None:
    """Validate a remaining-budget snapshot without coercing invalid values."""
    if value is None:
        return None
    if not isinstance(value, ABCMapping):
        raise TypeError("budget_remaining must be a mapping")
    remaining: dict[str, float] = {}
    for bucket, amount in value.items():
        if not isinstance(bucket, str) or not bucket.strip():
            raise ValueError("budget_remaining keys must be non-empty strings")
        try:
            valid_amount = (
                not isinstance(amount, bool)
                and isinstance(amount, (int, float))
                and math.isfinite(float(amount))
                and amount >= 0
            )
        except (OverflowError, ValueError, TypeError):
            valid_amount = False
        if not valid_amount:
            raise ValueError(
                "budget_remaining values must be finite non-negative numbers",
            )
        normalized_bucket = bucket.strip()
        if normalized_bucket in remaining:
            raise ValueError(
                "budget_remaining contains duplicate keys after normalization",
            )
        remaining[normalized_bucket] = float(amount)
    return remaining


def _event_safe(value: Any, *, _active: set[int] | None = None) -> Any:
    """Project an observation value onto the strict JSON event boundary.

    Effect tapes may retain typed values such as ``Reply`` or a tool's
    application object through the Claim codec.  ``AgentEvent`` is a smaller
    control-plane projection and intentionally accepts JSON only.  Preserve
    ordinary structures exactly, expose dataclass fields recursively, and
    use a deterministic opaque marker for values that cannot safely cross the
    projection boundary.  The detailed typed value remains on the Effect
    tape, so this conversion never changes execution evidence.
    """
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        # AgentEvent rejects non-finite numbers. Keep the projection usable
        # even when a provider supplied malformed diagnostic data.
        if math.isfinite(value):
            return value
        nonfinite_marker = "nan" if math.isnan(value) else "-inf" if value < 0 else "inf"
        return {"__lipas_nonfinite__": nonfinite_marker}
    if _active is None:
        _active = set()
    identity = id(value)
    if identity in _active:
        return {"type": "opaque", "python_type": type(value).__name__}
    if isinstance(value, ABCMapping):
        _active.add(identity)
        try:
            if any(not isinstance(key, str) for key in value):
                # Converting object keys to text can collide with an
                # existing string key (and may invoke a secret-bearing
                # ``__str__``).  Use an explicit entry projection whenever
                # one key is not already JSON-compatible.
                return {
                    "__lipas_mapping__": [
                        {
                            "key": _event_safe(key, _active=_active),
                            "value": _event_safe(item, _active=_active),
                        }
                        for key, item in value.items()
                    ],
                }
            result: dict[str, Any] = {}
            for key, item in value.items():
                result[key] = _event_safe(item, _active=_active)
            return result
        finally:
            _active.remove(identity)
    if isinstance(value, (list, tuple, set, frozenset)):
        _active.add(identity)
        try:
            if isinstance(value, (set, frozenset)):
                projected = [_event_safe(item, _active=_active) for item in value]
                return sorted(
                    projected,
                    key=lambda item: json.dumps(
                        item, ensure_ascii=False, sort_keys=True,
                        separators=(",", ":"), allow_nan=False,
                    ),
                )
            return [_event_safe(item, _active=_active) for item in value]
        finally:
            _active.remove(identity)
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        _active.add(identity)
        try:
            return {
                field.name: _event_safe(getattr(value, field.name), _active=_active)
                for field in dataclasses.fields(value)
            }
        finally:
            _active.remove(identity)
    # Do not leak an object's repr (which may contain memory addresses or
    # secrets) into a durable event. This marker is stable and intentionally
    # tells operators to inspect the Effect tape for the full value.
    return {"type": "opaque", "python_type": f"{type(value).__module__}.{type(value).__qualname__}"}


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
        # A bundle restore publishes workspace.db and runs/ separately.  If
        # the host died between those renames, settle the durable marker before
        # preflight so no component ever opens a mixed old/new workspace.
        recover_pending_restore = getattr(self.storage, "recover_pending_restore", None)
        if callable(recover_pending_restore):
            recover_pending_restore()
        preflight = self.storage.inspect()
        if preflight.state not in {"current", "uninitialized"}:
            # Preserve the actionable schema/migration exception without
            # creating a runtime lock in a legacy or invalid workspace.
            self.storage.require_current(create=True)
        initializing = preflight.state == "uninitialized"
        if initializing:
            # Multiple processes may discover an empty workspace at once.
            # The first opener needs an exclusive bootstrap lease; followers
            # wait briefly for the atomic publish, then downgrade to a shared
            # runtime lease once the workspace is current.  A normal current
            # workspace still takes the fail-fast shared path below.
            workspace_lease = None
            for attempt in range(100):
                try:
                    workspace_lease = self.storage.acquire_runtime_lease(
                        exclusive=True,
                    )
                    break
                except WorkspaceMigrationRequired:
                    latest = self.storage.inspect()
                    if latest.current:
                        try:
                            workspace_lease = self.storage.acquire_runtime_lease(
                                exclusive=False,
                            )
                            initializing = False
                            break
                        except WorkspaceMigrationRequired:
                            pass
                    if attempt == 99:
                        raise
                    time.sleep(0.05)
            assert workspace_lease is not None
            self._workspace_lease = workspace_lease
        else:
            workspace_lease = None
            for attempt in range(100):
                try:
                    workspace_lease = self.storage.acquire_runtime_lease(
                        exclusive=False,
                    )
                    break
                except WorkspaceMigrationRequired:
                    # A current workspace can still be in the tiny window
                    # where another process holds an exclusive bootstrap or
                    # maintenance lease.  Shared runtime startup is safe to
                    # wait for that fence to clear; the operation itself is
                    # still bounded and fail-closed after the retry budget.
                    if attempt == 99:
                        raise
                    time.sleep(0.05)
            assert workspace_lease is not None
            self._workspace_lease = workspace_lease
        try:
            if initializing:
                # The lifecycle lease above is the authority for first
                # bootstrap.  Do not call ``require_current(create=True)``
                # while already holding it: a second non-blocking flock on
                # the same workspace would look like another process.  A
                # competing opener may have initialized the database between
                # preflight and lease acquisition, so re-check before
                # creating the schema.
                latest = self.storage.inspect()
                if latest.state == "uninitialized":
                    self.storage._initialize_database(
                        self.storage.database_path,
                        migrated_from=None,
                    )
                elif latest.state != "current":
                    self.storage.require_current(create=True)
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
            self.workspace_policies = WorkspacePolicyStore(self.database_path)
            self.artifacts = ArtifactRepository(self.workbench)
            # Harness replay cursors are mutable.  Keep one private cursor per
            # (caller harness, Run) pair so a scoped execution never advances
            # caller-owned state, while successive Effects in the same Run
            # still consume the source tape in order.
            self._scoped_replay_cursors: dict[tuple[int, str], Any] = {}
            if initializing:
                self._workspace_lease.make_shared()
        except BaseException:
            _close_all((
                getattr(self, "sessions", None),
                getattr(self, "workspace_policies", None),
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
        """The stable authoritative control store owned by the Workbench."""
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
        self._ensure_open()
        if self.execution.get_run(run_id) is None:
            raise KeyError(run_id)
        return open_session(self.workbench.claims_path_for_run(run_id))

    async def execute_effect_for_run(
        self,
        run_id: str,
        proposal: EffectProposal,
        *,
        harness: Any,
        target: EffectTarget,
        available_capabilities: Mapping[str, Any] | Iterable[str] = (),
        budget_remaining: Mapping[str, float] | None = None,
        approved: bool = False,
        lease_token: str | None = None,
    ) -> EffectObservation:
        """Execute an effect on the Run-owned durable evidence tape.

        ``execute_effect`` remains a low-level compatibility bridge for
        callers that deliberately supply an in-memory ClaimStore.  Product
        paths should use this Run-scoped helper: it verifies the canonical Run
        exists, clones the supplied Harness configuration, and attaches its
        claims to ``runs/<run_id>`` for replay/audit.  The caller's harness is
        never mutated and the temporary SQLite connection is always closed.
        """
        self._ensure_open()
        if not isinstance(run_id, str) or not run_id.strip():
            raise ValueError("run_id must be a non-empty string")
        run = self.execution.get_run(run_id)
        if run is None:
            raise KeyError(run_id)
        # A pending Run has no owner yet.  Claim it before executing so the
        # effect has a fenced worker even when callers use the ergonomic
        # pending-run form (without passing ``lease_token``).  Running Runs
        # always require the caller's current token below.
        owned_lease_token = lease_token
        if run.state is RunState.PENDING:
            if lease_token is not None:
                raise ExecutionStateError(
                    "lease_token must be omitted when executing a pending Run",
                )
            claimed = self.execution.claim_run(run_id)
            owned_lease_token = claimed.lease_token
            run = claimed
        if run.state in {RunState.COMPLETED, RunState.FAILED, RunState.CANCELLED}:
            raise ExecutionStateError(
                f"cannot execute an Effect for terminal Run {run_id!r}"
            )
        if run.cancel_requested:
            # A cancellation request is durable authorization to stop.  Do
            # not start a new Effect merely because a worker still holds a
            # valid lease; the worker should settle the Run as cancelled.
            raise ExecutionStateError(
                f"cannot execute an Effect for cancelled Run {run_id!r}",
            )
        if run.state is RunState.WAITING:
            raise ExecutionStateError(
                f"cannot execute an Effect while Run {run_id!r} is waiting"
            )
        if run.state is RunState.RUNNING:
            if not isinstance(owned_lease_token, str) or not owned_lease_token.strip():
                raise ExecutionStateError(
                    "lease_token is required when executing an Effect for a running Run",
                )
            if run.lease_token != owned_lease_token or (
                run.lease_expires is not None and run.lease_expires <= time.time()
            ):
                raise ExecutionStateError("Run lease is stale or owned by another worker")
        from .harness import LLMHarness
        from .tool_harness import ToolHarness
        if not isinstance(harness, (LLMHarness, ToolHarness)):
            raise TypeError("harness must be an LLMHarness or ToolHarness")
        rowset = self.claims_for_run(run_id)
        scoped = copy.copy(harness)
        scoped.rowset = rowset
        if isinstance(harness, LLMHarness) and harness.replay_cursor is not None:
            cursor_key = (id(harness), run_id)
            replay_cursor = self._scoped_replay_cursors.get(cursor_key)
            if replay_cursor is None:
                replay_cursor = copy.deepcopy(harness.replay_cursor)
                self._scoped_replay_cursors[cursor_key] = replay_cursor
            assert isinstance(scoped, LLMHarness)
            scoped.replay_cursor = replay_cursor
        # ``copy.copy`` intentionally preserves the immutable harness
        # configuration, but replay bookkeeping belongs to the target Run's
        # evidence tape.  Carrying these mutable fields over from a harness
        # used with another RowSet can skip the per-tape session-init claim
        # and/or mark source recordings as consumed before this Run sees
        # them.  Start from a clean local cursor; ToolHarness restores any
        # already-consumed source ids from the newly opened tape itself.
        if hasattr(scoped, "_replay_session_started"):
            scoped._replay_session_started = False
        if hasattr(scoped, "_consumed_replay_effect_ids"):
            scoped._consumed_replay_effect_ids = set()
        try:
            observation = await self.execute_effect(
                proposal,
                harness=scoped,
                target=target,
                available_capabilities=available_capabilities,
                budget_remaining=budget_remaining,
                approved=approved,
            )
            # Product-facing effect observations are also part of the
            # canonical Run event stream.  The Claim/Effect tape remains the
            # detailed evidence; this idempotent event is the reconnectable
            # control-plane projection used by Web/CLI observers.
            self.execution.append_agent_event(
                run_id,
                "effect_observed",
                # The product-facing proposal id is useful in the payload,
                # but the durable event identity must also distinguish LLM
                # and Tool claim ids. Otherwise two valid proposals that use
                # the same business id would collide in the Run event log.
                identity=f"effect:{observation.claim_id or observation.effect_id}",
                # The Run event stream is a strict-JSON projection.  LLM
                # observations commonly contain a typed ``Reply`` and tool
                # observations may contain application objects; keep those
                # values on the Claim tape and project them safely here.
                data=_event_safe(observation.as_dict()),
            )
            if owned_lease_token is not None:
                latest = self.execution.get_run(run_id)
                if (
                    latest is None
                    or latest.lease_token != owned_lease_token
                    or latest.lease_expires is None
                    or latest.lease_expires <= time.time()
                ):
                    raise ExecutionStateError(
                        "Run lease changed or expired while the Effect was executing",
                    )
            return observation
        finally:
            close = getattr(rowset.store, "close", None)
            if callable(close):
                close()

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

    # -- Conversation kernel -----------------------------------------

    def create_conversation(
        self,
        *,
        conversation_id: str | None = None,
        title: str = "New conversation",
        workspace: str | Path | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> Conversation:
        self._ensure_open()
        return self.sessions.create_conversation(
            conversation_id=conversation_id,
            title=title,
            workspace=self.home if workspace is None else workspace,
            metadata=metadata,
        )

    def conversations(self, *, limit: int = 100) -> tuple[Conversation, ...]:
        self._ensure_open()
        return self.sessions.list_conversations(limit=limit)

    def conversation_messages(
        self, conversation_id: str, *, limit: int = 500,
    ) -> tuple[Message, ...]:
        self._ensure_open()
        return self.sessions.list_messages(conversation_id, limit=limit)

    def save_attachment(self, conversation_id: str, content: bytes, **kwargs: Any) -> Attachment:
        self._ensure_open()
        return self.sessions.save_attachment(conversation_id, content, **kwargs)

    def conversation_attachments(self, conversation_id: str, *, limit: int = 100) -> tuple[Attachment, ...]:
        self._ensure_open()
        return self.sessions.list_attachments(conversation_id, limit=limit)

    def read_attachment(self, attachment_id: str) -> tuple[Attachment, bytes]:
        self._ensure_open()
        return self.sessions.read_attachment(attachment_id)

    def append_message(self, conversation_id: str, **kwargs: Any) -> Message:
        self._ensure_open()
        task_id = kwargs.get("task_id")
        run_id = kwargs.get("run_id")
        if (task_id is None) != (run_id is None):
            raise ValueError("task_id and run_id must be provided together")
        if task_id is not None:
            task = self.execution.get_task(task_id)
            run = self.execution.get_run(run_id)
            if task is None or run is None or run.task_id != task.id:
                raise KeyError("linked task/run does not exist")
        return self.sessions.append_message(conversation_id, **kwargs)

    def decide_effect(
        self,
        proposal: EffectProposal,
        *,
        available_capabilities: Mapping[str, Any] | Iterable[str] = (),
        budget_remaining: Mapping[str, float] | None = None,
        approved: bool = False,
    ) -> EffectDecision:
        """Apply the small common Runtime admission contract.

        This is intentionally a pure decision over a proposal. Existing
        Harnesses, ExecutionStore transitions, and connectors remain the
        systems that record and perform the admitted effect; this method does
        not create a second scheduler or silently execute anything.
        """
        self._ensure_open()
        if not isinstance(proposal, EffectProposal):
            raise TypeError("proposal must be an EffectProposal")
        if not isinstance(approved, bool):
            raise TypeError("approved must be bool")
        capabilities = _normalise_capabilities(available_capabilities)
        remaining = _normalise_budget(budget_remaining)
        # Unknown risk labels must never silently fall through as low risk.
        # Providers and tools may extend their own metadata, but admission is
        # fail-closed until the host maps a label to a known ordering.
        if _risk_rank(proposal.risk) is None:
            return EffectDecision(
                False,
                "risk_unknown",
                detail={"risk": proposal.risk},
            )
        missing = sorted(proposal.capabilities - capabilities)
        if missing:
            return EffectDecision(
                False, "capability_denied", detail={"missing": missing},
            )
        if proposal.risk in {"external_write", "destructive", "high"} and not approved:
            return EffectDecision(
                False, "approval_required", detail={"risk": proposal.risk},
            )
        if remaining is not None:
            exceeded = {
                bucket: amount
                for bucket, amount in proposal.estimate.items()
                if amount > remaining.get(bucket, 0.0)
            }
            if exceeded:
                return EffectDecision(
                    False, "budget_exceeded", detail={"exceeded": exceeded},
                )
        return EffectDecision(True)

    def compile_workflow(self, goal: Any, **kwargs: Any) -> Any:
        """Compile a bounded mixed workflow in this Runtime's workspace.

        Planning is intentionally side-effect free: this helper returns an
        inspectable ``CompiledWorkflow`` and does not create a Task, Run, or
        Effect.  Callers can promote an actionable conversation message or
        explicitly hand off a compiled step through the existing authority.
        The Runtime workspace is used as the default when the caller omits
        ``workspace``.
        """
        self._ensure_open()
        from .workflow import AutonomousWorkflowCompiler, WorkflowGoal

        if isinstance(goal, WorkflowGoal):
            if "workspace" not in kwargs:
                # Preserve the goal's explicit workspace.  A caller may
                # still pass it as a normal compiler argument for string
                # goals; WorkflowGoal is immutable so do not mutate it here.
                return AutonomousWorkflowCompiler().compile(goal, **kwargs)
        kwargs.setdefault("workspace", self.home)
        return AutonomousWorkflowCompiler().compile(goal, **kwargs)

    def compile_plan(self, goal: Any, **kwargs: Any) -> Any:
        """Compatibility alias for :meth:`compile_workflow`."""
        return self.compile_workflow(goal, **kwargs)

    async def execute_workflow(
        self,
        workflow: Any,
        executor: Any,
        *,
        task_id: str | None = None,
        run_id: str | None = None,
        lease_seconds: float = 60.0,
        lease_token: str | None = None,
        context: Mapping[str, Any] | None = None,
        cancel_check: Any = None,
    ) -> Any:
        """Execute a compiled workflow as one durable Task/Run.

        The workflow module owns bounded ordering; this Runtime method adds
        the missing product lifecycle.  Step callbacks remain host-owned and
        must route world-changing actions through ``execute_effect_for_run``
        when they need Effect evidence.  Reusing a deterministic plan identity
        never creates a second Task/Run.
        """
        self._ensure_open()
        from .workflow import CompiledWorkflow, execute_compiled_workflow

        if not isinstance(workflow, CompiledWorkflow):
            raise TypeError("workflow must be a CompiledWorkflow")
        if not callable(executor):
            raise TypeError("executor must be callable")
        if cancel_check is not None and not callable(cancel_check):
            raise TypeError("cancel_check must be callable or None")
        if (
            isinstance(lease_seconds, bool)
            or not isinstance(lease_seconds, (int, float))
            or not math.isfinite(float(lease_seconds))
            or lease_seconds <= 0
        ):
            raise ValueError("lease_seconds must be a positive finite number")
        lease_seconds = float(lease_seconds)
        root = Path(workflow.goal.workspace).expanduser().resolve()
        if not root.is_dir():
            raise ValueError(f"workflow workspace is not a directory: {root}")
        deterministic_task_id = f"task_plan_{workflow.fingerprint[:32]}"
        deterministic_run_id = f"run_plan_{workflow.fingerprint[32:64]}"
        selected_task_id = deterministic_task_id if task_id is None else task_id
        selected_run_id = deterministic_run_id if run_id is None else run_id
        if not isinstance(selected_task_id, str) or not selected_task_id.strip():
            raise ValueError("task_id must be a non-empty string")
        if not isinstance(selected_run_id, str) or not selected_run_id.strip():
            raise ValueError("run_id must be a non-empty string")
        task = self.execution.get_task(selected_task_id)
        if task is None:
            task = self.execution.create_task(
                workflow.goal.goal,
                root,
                task_id=selected_task_id,
            )
        elif task.goal != workflow.goal.goal or Path(task.workspace) != root:
            raise ExecutionStateError(
                f"task id {selected_task_id!r} is bound to a different workflow",
            )
        run = self.execution.get_run(selected_run_id)
        if run is None:
            run = self.execution.create_run(task.id, run_id=selected_run_id)
        elif run.task_id != task.id:
            raise ExecutionStateError("workflow Run belongs to another Task")
        if run.state in {RunState.COMPLETED, RunState.FAILED, RunState.CANCELLED}:
            raise ExecutionStateError("compiled workflow Run is already terminal")
        if run.state is RunState.WAITING:
            raise ExecutionStateError("compiled workflow Run is waiting for input or approval")
        if run.state is RunState.PENDING:
            if lease_token is not None:
                raise ExecutionStateError("lease_token must be omitted for a pending workflow Run")
            run = self.execution.claim_run(run.id, lease_seconds=lease_seconds)
            lease_token = run.lease_token
        elif run.state is RunState.RUNNING:
            if lease_token is None:
                # A process may have crashed after persisting a step
                # checkpoint. Once the prior lease expires, reclaim the same
                # Run and resume from that checkpoint instead of requiring an
                # operator to discover and copy an obsolete token.
                if run.lease_expires is None or run.lease_expires > time.time():
                    raise ExecutionStateError("lease_token is required for a running workflow Run")
                run = self.execution.claim_run(run.id, lease_seconds=lease_seconds)
                lease_token = run.lease_token
            elif not isinstance(lease_token, str) or not lease_token.strip():
                raise ExecutionStateError("lease_token must be a non-empty string")
            elif run.lease_token != lease_token:
                raise ExecutionStateError("workflow Run lease is stale or owned by another worker")
        assert lease_token is not None

        checkpoint = self.execution.get_checkpoint(run.id)
        checkpoint_version = 0
        resumed_outputs: dict[str, Any] = {}
        if checkpoint is not None:
            checkpoint_version = checkpoint.version
            checkpoint_state = checkpoint.state
            if checkpoint_state.get("workflow_fingerprint") != workflow.fingerprint:
                raise ExecutionStateError(
                    "workflow Run checkpoint belongs to a different workflow",
                )
            raw_outputs = checkpoint_state.get("outputs", {})
            completed_steps = checkpoint_state.get("completed_steps", [])
            if not isinstance(raw_outputs, Mapping) or not isinstance(completed_steps, list):
                raise ExecutionStateError("workflow Run checkpoint is malformed")
            if any(
                not isinstance(step_id, str) or step_id not in raw_outputs
                for step_id in completed_steps
            ):
                raise ExecutionStateError("workflow Run checkpoint has invalid completed steps")
            completed_set = set(completed_steps)
            known_steps = {step.step_id for step in workflow.steps}
            if completed_set - known_steps:
                raise ExecutionStateError("workflow Run checkpoint names an unknown step")
            if any(
                step.step_id in completed_set
                and not set(step.depends_on) <= completed_set
                for step in workflow.steps
            ):
                raise ExecutionStateError("workflow Run checkpoint violates step dependencies")
            resumed_outputs = {
                step_id: raw_outputs[step_id]
                for step_id in completed_steps
            }

        def checkpoint_value(value: Any) -> Any:
            try:
                return json.loads(
                    json.dumps(value, ensure_ascii=False, allow_nan=False),
                )
            except (TypeError, ValueError, RecursionError) as exc:
                raise ExecutionStateError(
                    "workflow step output must be strict JSON for durable checkpoint",
                ) from exc

        async def invoke(step: Any, step_context: Mapping[str, Any]) -> Any:
            nonlocal checkpoint_version
            if step.step_id in resumed_outputs:
                self.execution.append_agent_event(
                    run.id, "workflow_step_replayed",
                    identity=f"workflow:{workflow.fingerprint}:{step.step_id}:replayed",
                    data={
                        "plan_id": workflow.plan_id,
                        "fingerprint": workflow.fingerprint,
                        "step_id": step.step_id,
                    },
                )
                return resumed_outputs[step.step_id]
            identity = f"workflow:{workflow.fingerprint}:{step.step_id}:started"
            self.execution.append_agent_event(
                run.id, "workflow_step_started", identity=identity,
                data={"plan_id": workflow.plan_id, "fingerprint": workflow.fingerprint, "step_id": step.step_id, "mode": step.mode},
            )
            try:
                result = executor(step, step_context)
                if inspect.isawaitable(result):
                    result = await result
            except Exception as exc:
                self.execution.append_agent_event(
                    run.id, "workflow_step_failed",
                    identity=f"workflow:{workflow.fingerprint}:{step.step_id}:failed",
                    data={"step_id": step.step_id, "error_type": type(exc).__name__},
                )
                raise
            output = checkpoint_value(result)
            saved = self.execution.save_checkpoint(
                run.id,
                lease_token,
                expected_version=checkpoint_version,
                phase="workflow_step",
                state={
                    "workflow_fingerprint": workflow.fingerprint,
                    "plan_id": workflow.plan_id,
                    "completed_steps": [
                        *resumed_outputs.keys(), step.step_id,
                    ],
                    "outputs": {
                        **resumed_outputs,
                        step.step_id: output,
                    },
                },
            )
            checkpoint_version = saved.version
            resumed_outputs[step.step_id] = output
            self.execution.append_agent_event(
                run.id, "workflow_step_completed",
                identity=f"workflow:{workflow.fingerprint}:{step.step_id}:completed",
                data={"step_id": step.step_id, "mode": step.mode},
            )
            return output

        heartbeat_error: list[BaseException] = []
        durable_cancel_requested = asyncio.Event()
        heartbeat_interval = lease_seconds / 3

        async def heartbeat_loop() -> None:
            try:
                while True:
                    await asyncio.sleep(heartbeat_interval)
                    renewed = self.execution.renew_lease(
                        run.id, lease_token, lease_seconds=lease_seconds,
                    )
                    if renewed.cancel_requested:
                        durable_cancel_requested.set()
                        return
            except asyncio.CancelledError:
                raise
            except BaseException as exc:
                heartbeat_error.append(exc)

        heartbeat_task = asyncio.create_task(heartbeat_loop())
        try:
            result = await execute_compiled_workflow(
                workflow,
                invoke,
                context=context,
                cancel_check=(
                    lambda: durable_cancel_requested.is_set()
                    or (cancel_check is not None and bool(cancel_check()))
                ),
            )
        finally:
            heartbeat_task.cancel()
            await asyncio.gather(heartbeat_task, return_exceptions=True)
        if heartbeat_error:
            raise ExecutionLeaseError(
                f"workflow Run {run.id!r} lost its lease",
            ) from heartbeat_error[0]
        if result.cancelled:
            # A cancellation request is durable control state.  Settle it
            # through the lease-aware transition so operators see a terminal
            # Run and the cancellation event is mirrored to evidence.
            self.execution.request_cancel(run.id)
            try:
                self.execution.finish_cancelled(run.id, lease_token)
            except ExecutionStateError:
                # Another operator may have settled the request concurrently;
                # the logical outcome remains cancelled and is idempotent.
                pass
        elif result.succeeded:
            self.execution.complete_run(
                run.id, lease_token, result=result.as_dict(),
            )
        else:
            error = dict(result.error or {
                "type": "WorkflowExecutionError",
                "message": "compiled workflow failed",
            })
            self.execution.fail_run(run.id, lease_token, error=error)
        return result

    async def execute_effect(
        self,
        proposal: EffectProposal,
        *,
        harness: Any,
        target: EffectTarget,
        available_capabilities: Mapping[str, Any] | Iterable[str] = (),
        budget_remaining: Mapping[str, float] | None = None,
        approved: bool = False,
    ) -> EffectObservation:
        """Admit, execute, and project one effect through a real Harness.

        This is the narrow bridge between the product-facing Runtime and the
        existing LLM/Tool Harnesses. The Harness owns the Claim/Effect tape;
        this method only supplies the admission decision and returns its
        durable observation. It never executes a target directly and never
        treats an orphan intent as success.
        """
        self._ensure_open()
        if not isinstance(proposal, EffectProposal):
            raise TypeError("proposal must be an EffectProposal")
        if not isinstance(target, (LLMTarget, ToolTarget)):
            raise TypeError("target must be an LLMTarget or ToolTarget")
        decision = self.decide_effect(
            proposal,
            available_capabilities=available_capabilities,
            budget_remaining=budget_remaining,
            approved=approved,
        )
        if decision.allowed and isinstance(target, ToolTarget):
            # A proposal is a declaration, not authority. Never let an agent
            # label an external/destructive Tool as ``risk='none'`` and use
            # that mismatch to bypass the Tool's own side-effect contract.
            declared_rank = _risk_rank(proposal.risk)
            actual_rank = _tool_risk_rank(target.tool.side_effect.value)
            if declared_rank is None or declared_rank < actual_rank:
                decision = EffectDecision(
                    False,
                    "risk_mismatch",
                    detail={
                        "declared": proposal.risk,
                        "actual": target.tool.side_effect.value,
                    },
                )
        from .exceptions import OrphanedEffectError
        from .harness import LLMHarness
        from .tool_harness import ToolHarness

        if isinstance(target, LLMTarget) and isinstance(harness, LLMHarness):
            try:
                await harness.call(
                    target.request,
                    proposal=proposal,
                    admission=decision,
                )
            except OrphanedEffectError:
                pass
            return harness.observation(proposal.effect_id)
        if isinstance(target, ToolTarget) and isinstance(harness, ToolHarness):
            try:
                await harness.call(
                    tool_name=target.tool.name,
                    arguments=target.arguments,
                    proposal=proposal,
                    admission=decision,
                )
            except OrphanedEffectError:
                pass
            return harness.observation(proposal.effect_id)
        raise TypeError(
            "harness and target must be a matching LLMHarness/LLMTarget or "
            "ToolHarness/ToolTarget pair",
        )


    def conversation_events(
        self,
        conversation_id: str,
        *,
        after: int = 0,
        limit: int = 100,
    ) -> ConversationEventPage:
        """Return one stable cursor page, including linked execution events.

        AgentEvent remains authoritative in ExecutionStore.  The chat event
        table is only an idempotent projection, populated here so reconnecting
        Web/CLI clients see tool activity and terminal results on the same
        conversation cursor as messages and task proposals.
        """
        self._ensure_open()
        run_ids = self.sessions.linked_run_ids(conversation_id)
        for run_id in sorted(run_ids):
            run = self.execution.get_run(run_id)
            if run is None:
                continue
            # Project in bounded pages so a long-running Run cannot be
            # truncated by a fixed safety limit.  The execution event cursor
            # is immutable, therefore paging is deterministic and safe to
            # retry after a process interruption.
            cursor = 0
            while True:
                page = self.execution.agent_events(run_id, after=cursor, limit=1_000)
                if not page:
                    break
                for event in page:
                    self.sessions.append_event(
                        conversation_id,
                        event_id=f"execution:{event.event_id}",
                        kind="agent_event",
                        message_id=None,
                        task_id=run.task_id,
                        run_id=run_id,
                        payload={
                            "event_id": event.event_id,
                            "type": event.type,
                            "iteration": event.iteration,
                            "data": dict(event.data),
                        },
                    )
                cursor = page[-1].sequence
                if len(page) < 1_000:
                    break
            for interrupt in self.execution.list_interrupts(run_id=run_id):
                self.sessions.append_event(
                    conversation_id,
                    event_id=f"interrupt:{interrupt.id}:{interrupt.state.value}",
                    kind=(
                        "approval_card"
                        if interrupt.kind == "approval" else "input_card"
                    ),
                    task_id=run.task_id,
                    run_id=run_id,
                    payload={
                        "interrupt_id": interrupt.id,
                        "kind": interrupt.kind,
                        "state": interrupt.state.value,
                        "request": dict(interrupt.request),
                        "response": interrupt.response,
                    },
                )
        return self.sessions.events(conversation_id, after=after, limit=limit)

    def promote_message_to_task(
        self,
        conversation_id: str,
        message_id: str,
        *,
        goal: str | None = None,
        workspace: str | Path | None = None,
    ) -> tuple[Task, Run, Message]:
        """Promote one message exactly once into the canonical Task/Run chain."""
        self._ensure_open()
        message = self.sessions.get_message(message_id)
        if message is None or message.conversation_id != conversation_id:
            raise KeyError(message_id)
        if message.task_id is not None or message.run_id is not None:
            if message.task_id is None or message.run_id is None:
                raise RuntimeError("message has a partial task link")
            task = self.execution.get_task(message.task_id)
            run = self.execution.get_run(message.run_id)
            if task is None or run is None or run.task_id != task.id:
                raise RuntimeError("message task link points to missing execution state")
            return task, run, message

        conversation = self.sessions.get_conversation(conversation_id)
        if conversation is None:
            raise KeyError(conversation_id)
        selected_goal = goal.strip() if isinstance(goal, str) and goal.strip() else _message_goal(message)
        selected_workspace = (
            str(Path(workspace).expanduser().resolve())
            if workspace is not None else conversation.workspace
        )
        conversation_root = Path(conversation.workspace).expanduser().resolve()
        selected_root = Path(selected_workspace).expanduser().resolve()
        if selected_root != conversation_root and conversation_root not in selected_root.parents:
            raise ValueError(
                "promoted Task workspace must be the conversation workspace "
                "or one of its descendants",
            )
        digest = hashlib.sha256(
            f"{conversation_id}\0{message.id}".encode("utf-8"),
        ).hexdigest()
        task_id = f"task_chat_{digest[:32]}"
        run_id = f"run_chat_{digest[32:64]}"
        task = self.execution.get_task(task_id)
        if task is None:
            try:
                task = self.execution.create_task(
                    selected_goal, selected_workspace, task_id=task_id,
                )
            except ExecutionStateError:
                task = self.execution.get_task(task_id)
                if task is None:
                    raise
        if task.goal != selected_goal or task.workspace != selected_workspace:
            raise RuntimeError("deterministic chat Task identity has conflicting data")
        run = self.execution.get_run(run_id)
        if run is None:
            try:
                run = self.execution.create_run(task.id, run_id=run_id)
            except ExecutionStateError:
                run = self.execution.get_run(run_id)
                if run is None:
                    raise
        if run.task_id != task.id:
            raise RuntimeError("deterministic chat Run identity has conflicting data")
        linked = self.sessions.attach_message(
            message.id, task_id=task.id, run_id=run.id,
        )
        return task, run, linked

    def coordinator(self, **kwargs: Any):
        """Build a multi-Agent coordinator over this Runtime's authority.

        The returned object borrows ``execution`` and therefore never closes
        the Runtime's store. Member registration remains application-owned
        configuration; handoff Task/Run facts are persisted by the Runtime.
        """
        self._ensure_open()
        reserved = {"execution", "workspace", "_owns_execution"} & kwargs.keys()
        if reserved:
            names = ", ".join(sorted(reserved))
            raise ValueError(f"LIPASRuntime.coordinator owns: {names}")
        from .coordination import AgentCoordinator
        return AgentCoordinator(
            self.execution,
            workspace=self.home,
            policy_store=self.workspace_policies,
            **kwargs,
        )

    def operator(self, **kwargs: Any):
        """Build the dependency-free local Web operator over this Runtime.

        The returned operator borrows the Runtime's execution connection and
        never owns or closes it.  Pass ``coordinator=runtime.coordinator()``
        when aggregate coordination events should be exposed as well.
        """
        self._ensure_open()
        from .operator import LocalWebOperator
        reserved = {"execution", "workbench"} & kwargs.keys()
        if reserved:
            names = ", ".join(sorted(reserved))
            raise ValueError(f"LIPASRuntime.operator owns: {names}")
        return LocalWebOperator(
            self.execution,
            workbench=self.workbench,
            operations=self.operations,
            sessions=self.sessions,
            conversation_workspace=self.home,
            conversation_event_reader=self.conversation_events,
            promote_message=self.promote_message_to_task,
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
        # Each Run owns its evidence attachment. The Workbench's global
        # ExecutionStore remains stable, so concurrent durable calls cannot
        # close or redirect one another's audit sink.
        with self.workbench.execution_scope(rowset, run_id=run_id) as execution:
            if resume:
                return await agent.resume_durable(
                    execution_store=execution,
                    run_id=run_id,
                    **kwargs,
                )
            return await agent.run_durable(
                prompt,
                execution_store=execution,
                run_id=run_id,
                **kwargs,
            )

    async def resume_durable(
        self,
        agent: Any,
        *,
        run_id: str,
        **kwargs: Any,
    ):
        if "execution_store" in kwargs:
            raise ValueError("LIPASRuntime owns execution_store")
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

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("LIPASRuntime is closed")

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
            getattr(self, "sessions", None),
            getattr(self, "workspace_policies", None),
            getattr(self, "handoffs", None),
            getattr(self, "operations", None),
            getattr(self, "workbench", None),
            getattr(getattr(self, "claims", None), "store", None),
            getattr(self, "_workspace_lease", None),
        ))
        replay_cursors = getattr(self, "_scoped_replay_cursors", None)
        if replay_cursors is not None:
            replay_cursors.clear()
        self._closed = True
        if error is not None:
            raise error

    def __enter__(self) -> "LIPASRuntime":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()


class AgentRuntime(LIPASRuntime):
    """The product-facing name for the unified Agent Operating Runtime.

    It is deliberately a thin subclass: all authority, persistence, and
    lifecycle behavior remains in :class:`LIPASRuntime`.
    """


def _risk_rank(value: str) -> int | None:
    """Map product risk labels to the Tool side-effect ordering."""
    if not isinstance(value, str):
        return None
    return {
        "none": 0,
        "read_only": 1,
        "read": 1,
        "idempotent_write": 2,
        "external_write": 3,
        "destructive": 3,
        "high": 3,
    }.get(value.strip().lower())


def _tool_risk_rank(value: str) -> int:
    return {
        "pure": 0,
        "read_only": 1,
        "idempotent_write": 2,
        "external_write": 3,
    }.get(value, 3)


def _message_goal(message: Message) -> str:
    if isinstance(message.content, str) and message.content.strip():
        return message.content.strip()
    return str(message.content)
