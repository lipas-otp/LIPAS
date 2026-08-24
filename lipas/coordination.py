"""ExecutionStore-backed coordination for groups of ordinary Agents.

The coordination layer deliberately owns no queue or workflow database. Every
handoff is one deterministic Task/Run in the existing ExecutionStore, while
policies only compose those runs. This gives multi-Agent work the same lease,
cancellation, deadline, terminal-result, and audit semantics as other durable
LIPAS execution without turning a mailbox or graph projection into authority.
"""
from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import math
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from .behaviour import AgentState, FinalResult
from .context import (
    RunCancelled,
    RunContext,
    RunDeadlineExceeded,
    bind_run_context,
)
from .durable import InputPolicy, ApprovalPolicy
from .events import AgentEvent, AgentEventType
from .exceptions import LipasError
from .coordination_policy import CapabilityPolicy, SharedBudgetPolicy
from .execution import (
    ExecutionLeaseError,
    ExecutionStateError,
    ExecutionStore,
    InterruptState,
    Run,
    RunState,
    RunSuspended,
)

__all__ = [
    "AgentCoordinator",
    "CoordinationBusy",
    "CoordinationBudgetExceeded",
    "CoordinationCapabilityDenied",
    "CoordinationError",
    "CoordinationFailed",
    "CoordinationIdentityConflict",
    "CoordinationRecoveryRequired",
    "CoordinationResult",
    "CoordinationResultError",
    "CoordinationEvent",
    "CoordinationEventHandle",
    "CoordinationEventPage",
    "HandoffEnvelope",
    "HandoffExecutionError",
    "HandoffFailure",
    "HandoffOutcome",
    "MemberInfo",
    "Transfer",
]


_COORDINATION_VERSION = 1
_TRANSFER_MARKER = "lipas.coordination.transfer/v1"
_AGENT_RESULT_MARKER = "lipas.coordination.agent-result/v1"
_DEFAULT_MAX_BYTES = 1_000_000
_MAX_JSON_DEPTH = 64
_MAX_JSON_NODES = 100_000


class CoordinationError(LipasError):
    """Base class for coordination contract failures."""


class CoordinationIdentityConflict(CoordinationError):
    """A stable handoff identity was reused for a different request."""


class CoordinationBusy(CoordinationError):
    """Another live worker owns the same durable handoff."""


class CoordinationBudgetExceeded(CoordinationError):
    """A shared coordination budget rejected a new handoff reservation."""


class CoordinationCapabilityDenied(CoordinationError):
    """A member requested capabilities outside the host delegation policy."""


class CoordinationRecoveryRequired(CoordinationError):
    """An expired handoff cannot be redelivered without explicit permission."""


class CoordinationResultError(CoordinationError):
    """A member returned a value that cannot be durably replayed."""


@dataclass(frozen=True, slots=True)
class CoordinationEvent:
    """One event projected from a coordination Run into an aggregate stream."""

    coordination_id: str
    cursor: str
    event: AgentEvent

    @property
    def run_id(self) -> str:
        return self.event.run_id

    @property
    def type(self) -> str:
        return self.event.type

    @property
    def data(self) -> Mapping[str, Any]:
        return self.event.data


@dataclass(frozen=True, slots=True)
class CoordinationEventPage:
    """Bounded aggregate event page with a reconnectable opaque cursor."""

    events: tuple[CoordinationEvent, ...]
    next_cursor: str | None
    has_more: bool


class CoordinationEventHandle:
    """Reconnectable read handle over all Runs in one coordination id.

    Per-run AgentEvent sequences remain authoritative. The aggregate cursor is
    a compact JSON map of ``run_id -> sequence`` so independent Runs can be
    merged without introducing a second global event sequence.
    """

    def __init__(self, coordinator: "AgentCoordinator", coordination_id: str) -> None:
        if not isinstance(coordination_id, str) or not coordination_id.strip():
            raise ValueError("coordination_id must be non-empty")
        self._coordinator = coordinator
        self.coordination_id = coordination_id

    def read(
        self,
        *,
        after: str | None = None,
        limit: int = 100,
    ) -> CoordinationEventPage:
        self._coordinator._ensure_open()
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("limit must be a positive int")
        positions = _decode_aggregate_cursor(after)
        candidates: list[tuple[float, str, int, AgentEvent]] = []
        for run in self._coordinator.execution.list_runs():
            # Most runs do not belong to this coordination.  Probe the first
            # event through the bounded/indexed path before materializing a
            # full per-run stream; this keeps an aggregate catch-up cheap in a
            # workspace containing unrelated Tasks.
            probe = self._coordinator.execution.agent_events(run.id, limit=1)
            if not probe or probe[0].data.get("coordination_id") != self.coordination_id:
                continue
            events = self._coordinator.execution.agent_events(run.id)
            floor = positions.get(run.id, 0)
            candidates.extend(
                (event.created_at, event.run_id, event.sequence, event)
                for event in events
                if event.sequence > floor
            )
        candidates.sort(key=lambda item: (item[0], item[1], item[2]))
        selected = candidates[:limit]
        advanced = dict(positions)
        projected: list[CoordinationEvent] = []
        for _created_at, run_id, sequence, event in selected:
            advanced[run_id] = max(advanced.get(run_id, 0), sequence)
            projected.append(CoordinationEvent(
                self.coordination_id,
                _encode_aggregate_cursor(advanced),
                event,
            ))
        next_cursor = _encode_aggregate_cursor(advanced) if selected else after
        has_more = len(candidates) > len(selected)
        return CoordinationEventPage(tuple(projected), next_cursor, has_more)

    def events(
        self,
        *,
        after: str | None = None,
        limit: int = 100,
    ) -> CoordinationEventPage:
        """Alias for ``read`` for stream-oriented callers."""
        return self.read(after=after, limit=limit)


class HandoffExecutionError(CoordinationError):
    """One member handoff reached a durable failed state."""

    def __init__(
        self,
        envelope: "HandoffEnvelope",
        run_id: str,
        error_type: str,
    ) -> None:
        self.envelope = envelope
        self.run_id = run_id
        self.error_type = error_type
        super().__init__(
            f"handoff {envelope.id!r} to {envelope.recipient!r} failed "
            f"with {error_type}",
        )


@dataclass(frozen=True, slots=True)
class HandoffEnvelope:
    """Stable, provider-neutral ownership transfer between two members."""

    id: str
    coordination_id: str
    sender: str
    recipient: str
    payload: Any
    sequence: int = 0
    parent_id: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        for name, value in (
            ("id", self.id),
            ("coordination_id", self.coordination_id),
            ("sender", self.sender),
            ("recipient", self.recipient),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"HandoffEnvelope.{name} must be non-empty")
        if (
            isinstance(self.sequence, bool)
            or not isinstance(self.sequence, int)
            or self.sequence < 0
        ):
            raise ValueError("HandoffEnvelope.sequence must be non-negative")
        if self.parent_id is not None and (
            not isinstance(self.parent_id, str) or not self.parent_id.strip()
        ):
            raise ValueError("HandoffEnvelope.parent_id must be non-empty or None")
        if not isinstance(self.metadata, Mapping):
            raise TypeError("HandoffEnvelope.metadata must be a mapping")
        if (
            isinstance(self.created_at, bool)
            or not isinstance(self.created_at, (int, float))
            or not math.isfinite(float(self.created_at))
        ):
            raise ValueError("HandoffEnvelope.created_at must be finite")

    @classmethod
    def create(
        cls,
        *,
        coordination_id: str,
        sender: str,
        recipient: str,
        payload: Any,
        sequence: int = 0,
        parent_id: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        handoff_id: str | None = None,
    ) -> "HandoffEnvelope":
        identity = handoff_id or _stable_id(
            "handoff",
            coordination_id,
            str(sequence),
            sender,
            recipient,
            parent_id or "",
        )
        return cls(
            id=identity,
            coordination_id=coordination_id,
            sender=sender,
            recipient=recipient,
            payload=_json_value(payload, path="payload"),
            sequence=sequence,
            parent_id=parent_id,
            metadata=cast(
                Mapping[str, Any],
                _json_value(dict(metadata or {}), path="metadata"),
            ),
        )


@dataclass(frozen=True, slots=True)
class Transfer:
    """A member result requesting one bounded Swarm-style transfer."""

    recipient: str
    payload: Any
    reason: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.recipient, str) or not self.recipient.strip():
            raise ValueError("Transfer.recipient must be non-empty")
        if not isinstance(self.reason, str):
            raise TypeError("Transfer.reason must be a string")


@dataclass(frozen=True, slots=True)
class MemberInfo:
    name: str
    description: str = ""
    redelivery_safe: bool = False
    version: str = "1"
    capabilities: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        if (
            not isinstance(self.name, str)
            or not self.name.strip()
            or self.name != self.name.strip()
        ):
            raise ValueError("MemberInfo.name must be a trimmed non-empty string")
        if not isinstance(self.description, str):
            raise TypeError("MemberInfo.description must be a string")
        if not isinstance(self.redelivery_safe, bool):
            raise TypeError("MemberInfo.redelivery_safe must be bool")
        if (
            not isinstance(self.version, str)
            or not self.version.strip()
            or self.version != self.version.strip()
        ):
            raise ValueError("MemberInfo.version must be a trimmed non-empty string")
        if not isinstance(self.capabilities, frozenset):
            raise TypeError("MemberInfo.capabilities must be a frozenset")
        if any(
            not isinstance(value, str)
            or not value.strip()
            or value != value.strip()
            for value in self.capabilities
        ):
            raise ValueError(
                "MemberInfo.capabilities must contain trimmed non-empty strings",
            )


@dataclass(frozen=True, slots=True)
class HandoffOutcome:
    envelope: HandoffEnvelope
    run_id: str
    attempt: int
    value: Any
    replayed: bool = False


@dataclass(frozen=True, slots=True)
class HandoffFailure:
    envelope: HandoffEnvelope
    run_id: str | None
    error_type: str
    message: str


@dataclass(frozen=True, slots=True)
class CoordinationResult:
    coordination_id: str
    strategy: str
    outcomes: tuple[HandoffOutcome, ...]
    failures: tuple[HandoffFailure, ...] = ()
    value: Any = None

    @property
    def successful(self) -> bool:
        return not self.failures


class CoordinationFailed(CoordinationError):
    """A policy could not satisfy its requested completion condition."""

    def __init__(self, result: CoordinationResult) -> None:
        self.result = result
        super().__init__(
            f"{result.strategy} coordination {result.coordination_id!r} "
            f"failed in {len(result.failures)} handoff(s)",
        )


MemberHandler = Callable[[Any], Awaitable[Any]]


@dataclass(frozen=True, slots=True)
class _Member:
    info: MemberInfo
    handler: Any = field(repr=False, compare=False)
    receives_envelope: bool = False
    approval_policy: ApprovalPolicy | None = field(
        default=None, repr=False, compare=False,
    )
    input_policy: InputPolicy | None = field(
        default=None, repr=False, compare=False,
    )
    phase_timeout_s: float | None = field(
        default=None, repr=False, compare=False,
    )


class AgentCoordinator:
    """Optional multi-Agent coordination above one ExecutionStore authority.

    Member registration is code-side configuration and must be reconstructed on
    restart. Handoff identity, ownership, attempt, terminal result, and failure
    remain durable Task/Run facts in ``execution``.
    """

    def __init__(
        self,
        execution: ExecutionStore,
        *,
        workspace: str | Path,
        max_concurrency: int = 4,
        max_steps: int = 64,
        lease_seconds: float = 60.0,
        heartbeat_interval_s: float | None = None,
        max_payload_bytes: int = _DEFAULT_MAX_BYTES,
        max_result_bytes: int = _DEFAULT_MAX_BYTES,
        budget_policy: SharedBudgetPolicy | None = None,
        capability_policy: CapabilityPolicy | None = None,
        _owns_execution: bool = False,
    ) -> None:
        if not isinstance(execution, ExecutionStore):
            raise TypeError("execution must be an ExecutionStore")
        self.execution = execution
        self.workspace = Path(workspace).expanduser().resolve()
        if not self.workspace.is_dir():
            raise ValueError(f"coordination workspace is not a directory: {self.workspace}")
        self.max_concurrency = _positive_int(max_concurrency, "max_concurrency")
        self.max_steps = _positive_int(max_steps, "max_steps")
        self.lease_seconds = _positive_seconds(lease_seconds, "lease_seconds")
        if heartbeat_interval_s is None:
            heartbeat_interval_s = self.lease_seconds / 3
        self.heartbeat_interval_s = _positive_seconds(
            heartbeat_interval_s, "heartbeat_interval_s",
        )
        if self.heartbeat_interval_s >= self.lease_seconds:
            raise ValueError("heartbeat_interval_s must be shorter than lease_seconds")
        self.max_payload_bytes = _positive_int(
            max_payload_bytes, "max_payload_bytes",
        )
        self.max_result_bytes = _positive_int(
            max_result_bytes, "max_result_bytes",
        )
        if budget_policy is not None and not isinstance(
            budget_policy, SharedBudgetPolicy,
        ):
            raise TypeError("budget_policy must be SharedBudgetPolicy or None")
        if capability_policy is not None and not isinstance(
            capability_policy, CapabilityPolicy,
        ):
            raise TypeError("capability_policy must be CapabilityPolicy or None")
        self.budget_policy = budget_policy
        self.capability_policy = capability_policy
        if budget_policy is not None:
            try:
                self.execution.configure_coordination_budget(
                    budget_policy.scope,
                    budget_policy.limits,
                )
            except ExecutionStateError as exc:
                raise CoordinationBudgetExceeded(str(exc)) from exc
        self._members: dict[str, _Member] = {}
        self._owns_execution = _owns_execution
        self._closed = False

    @classmethod
    def open(
        cls,
        path: str | Path = ":memory:",
        *,
        workspace: str | Path | None = None,
        **kwargs: Any,
    ) -> "AgentCoordinator":
        """Open a standalone coordinator; Runtime users should reuse its store."""
        if workspace is None:
            workspace = Path.cwd() if path == ":memory:" else Path(path).parent
        resolved_workspace = Path(workspace).expanduser().resolve()
        resolved_workspace.mkdir(parents=True, exist_ok=True)
        execution = ExecutionStore(path)
        try:
            return cls(
                execution,
                workspace=resolved_workspace,
                _owns_execution=True,
                **kwargs,
            )
        except BaseException:
            execution.close()
            raise

    @property
    def members(self) -> tuple[MemberInfo, ...]:
        return tuple(member.info for member in self._members.values())

    def event_handle(self, coordination_id: str) -> CoordinationEventHandle:
        """Return a reconnectable aggregate event handle for one coordination."""
        self._ensure_open()
        return CoordinationEventHandle(self, coordination_id)

    def budget_snapshot(self) -> Mapping[str, Any] | None:
        """Return the durable shared budget projection, if configured."""
        self._ensure_open()
        if self.budget_policy is None:
            return None
        return self.execution.coordination_budget_snapshot(
            self.budget_policy.scope,
        )

    def get_handoff_run(self, handoff: str | HandoffEnvelope) -> Run | None:
        """Return the authoritative Run for a stable handoff, if created."""
        self._ensure_open()
        handoff_id = _handoff_id(handoff)
        return self.execution.get_run(_stable_id("coord_run", handoff_id))

    def cancel_handoff(self, handoff: str | HandoffEnvelope) -> Run:
        """Persist a cancellation request for one pending or active handoff."""
        self._ensure_open()
        handoff_id = _handoff_id(handoff)
        run_id = _stable_id("coord_run", handoff_id)
        if self.execution.get_run(run_id) is None:
            raise KeyError(handoff_id)
        return self.execution.request_cancel(run_id)

    def add(
        self,
        name: str,
        handler: Any,
        *,
        description: str = "",
        redelivery_safe: bool = False,
        receives_envelope: bool = False,
        version: str = "1",
        approval_policy: ApprovalPolicy | None = None,
        input_policy: InputPolicy | None = None,
        phase_timeout_s: float | None = None,
        capabilities: Sequence[str] = (),
    ) -> "AgentCoordinator":
        """Register one Agent or async member under a stable host-owned name."""
        self._ensure_open()
        if not isinstance(name, str) or not name.strip() or name != name.strip():
            raise ValueError("member name must be a trimmed non-empty string")
        if name in self._members:
            raise ValueError(f"duplicate member name {name!r}")
        if not isinstance(description, str):
            raise TypeError("member description must be a string")
        if not isinstance(redelivery_safe, bool):
            raise TypeError("redelivery_safe must be bool")
        if not isinstance(receives_envelope, bool):
            raise TypeError("receives_envelope must be bool")
        try:
            declared_capabilities = frozenset(capabilities)
        except TypeError as exc:
            raise TypeError("capabilities must be an iterable of strings") from exc
        if any(
            not isinstance(value, str)
            or not value.strip()
            or value != value.strip()
            for value in declared_capabilities
        ):
            raise ValueError("capabilities must contain trimmed non-empty strings")
        if self.capability_policy is not None:
            missing = self.capability_policy.missing(name, declared_capabilities)
            if missing:
                raise CoordinationCapabilityDenied(
                    f"member {name!r} requires undelegated capabilities: "
                    f"{', '.join(sorted(missing))}",
                )
        if approval_policy is not None and not callable(approval_policy):
            raise TypeError("approval_policy must be callable or None")
        if input_policy is not None and not callable(input_policy):
            raise TypeError("input_policy must be callable or None")
        if phase_timeout_s is not None:
            phase_timeout_s = _positive_seconds(
                phase_timeout_s, "phase_timeout_s",
            )
        from .agent import Agent

        if isinstance(handler, Agent) and receives_envelope:
            raise ValueError(
                "receives_envelope=True is only supported for async members; "
                "wrap an Agent in an async envelope adapter",
            )

        is_async_callable = (
            inspect.iscoroutinefunction(handler)
            or (
                callable(handler)
                and inspect.iscoroutinefunction(type(handler).__call__)
            )
        )
        if not isinstance(handler, Agent) and not is_async_callable:
            raise TypeError("member handler must be an Agent or async callable")
        if not isinstance(handler, Agent) and (
            approval_policy is not None
            or input_policy is not None
            or phase_timeout_s is not None
        ):
            raise ValueError(
                "durable policies and phase_timeout_s require an Agent member",
            )
        self._members[name] = _Member(
            MemberInfo(
                name,
                description,
                redelivery_safe,
                version,
                declared_capabilities,
            ),
            handler,
            receives_envelope,
            approval_policy,
            input_policy,
            phase_timeout_s,
        )
        return self

    async def handoff(
        self,
        recipient: str,
        payload: Any,
        *,
        sender: str = "user",
        coordination_id: str | None = None,
        handoff_id: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        context: RunContext | None = None,
        timeout_s: float | None = None,
        deadline: float | None = None,
    ) -> HandoffOutcome:
        coordination_id = _coordination_id(coordination_id, context)
        context = _coordination_context(
            coordination_id, context=context, timeout_s=timeout_s,
            deadline=deadline,
        )
        envelope = HandoffEnvelope.create(
            coordination_id=coordination_id,
            sender=sender,
            recipient=recipient,
            payload=payload,
            metadata=metadata,
            handoff_id=handoff_id,
        )
        return await self.dispatch(envelope, context=context)

    async def dispatch(
        self,
        envelope: HandoffEnvelope,
        *,
        context: RunContext | None = None,
    ) -> HandoffOutcome:
        """Execute or replay one durable envelope."""
        self._ensure_open()
        if not isinstance(envelope, HandoffEnvelope):
            raise TypeError("envelope must be a HandoffEnvelope")
        # Member code must not be able to mutate the caller's object after its
        # durable fingerprint has been calculated.  The second snapshot in
        # ``_invoke_member`` likewise keeps the persisted envelope immutable
        # from the handler's point of view.
        envelope = _snapshot_envelope(envelope)
        member = self._member(envelope.recipient)
        parent = context or RunContext.create(run_id=envelope.coordination_id)
        parent.check()
        request = _envelope_request(envelope, member.info)
        request_json = _bounded_json(
            request, self.max_payload_bytes, "handoff request",
        )
        fingerprint = hashlib.sha256(request_json.encode("utf-8")).hexdigest()
        # Validate an already durable identity before reserving shared budget.
        # Otherwise a malformed redelivery could consume a reservation and
        # only then fail with CoordinationIdentityConflict.  This is a
        # read-only preflight; the claim transaction below remains the race
        # boundary and is still authoritative.
        self._preflight_handoff_identity(envelope, fingerprint)
        if self.budget_policy is not None:
            try:
                estimate = self.budget_policy.estimate(envelope, member.info)
                if estimate:
                    self.execution.reserve_coordination_budget(
                        self.budget_policy.scope,
                        envelope.id,
                        estimate,
                    )
            except (ExecutionStateError, TypeError, ValueError) as exc:
                raise CoordinationBudgetExceeded(str(exc)) from exc
        run, claimed = self._claim_handoff(
            envelope, member=member, fingerprint=fingerprint,
        )
        if not claimed:
            return self._replayed_outcome(
                envelope, run, fingerprint, member=member,
            )
        return await self._run_claimed(
            envelope,
            member=member,
            run=run,
            parent=parent,
            fingerprint=fingerprint,
        )

    async def sequential(
        self,
        recipients: Sequence[str],
        payload: Any,
        *,
        sender: str = "user",
        coordination_id: str | None = None,
        context: RunContext | None = None,
        timeout_s: float | None = None,
        deadline: float | None = None,
    ) -> CoordinationResult:
        names = self._member_sequence(recipients)
        coordination_id = _coordination_id(coordination_id, context)
        parent = _coordination_context(
            coordination_id, context=context, timeout_s=timeout_s,
            deadline=deadline,
        )
        outcomes: list[HandoffOutcome] = []
        current = payload
        previous: str | None = None
        current_sender = sender
        for sequence, recipient in enumerate(names):
            envelope = HandoffEnvelope.create(
                coordination_id=coordination_id,
                sender=current_sender,
                recipient=recipient,
                payload=current,
                sequence=sequence,
                parent_id=previous,
            )
            try:
                outcome = await self.dispatch(envelope, context=parent)
            except CoordinationError as exc:
                result = CoordinationResult(
                    coordination_id,
                    "sequential",
                    tuple(outcomes),
                    (_failure(envelope, exc),),
                    current,
                )
                raise CoordinationFailed(result) from exc
            outcomes.append(outcome)
            current = outcome.value
            previous = envelope.id
            current_sender = recipient
        return CoordinationResult(
            coordination_id, "sequential", tuple(outcomes), value=current,
        )

    async def round_robin(
        self,
        recipients: Sequence[str],
        payload: Any,
        *,
        rounds: int = 1,
        sender: str = "user",
        coordination_id: str | None = None,
        context: RunContext | None = None,
        timeout_s: float | None = None,
        deadline: float | None = None,
    ) -> CoordinationResult:
        names = self._member_sequence(recipients)
        rounds = _positive_int(rounds, "rounds")
        if rounds * len(names) > self.max_steps:
            raise ValueError("round-robin exceeds coordinator max_steps")
        expanded = tuple(name for _ in range(rounds) for name in names)
        try:
            result = await self.sequential(
                expanded,
                payload,
                sender=sender,
                coordination_id=coordination_id,
                context=context,
                timeout_s=timeout_s,
                deadline=deadline,
            )
        except CoordinationFailed as exc:
            failure = CoordinationResult(
                exc.result.coordination_id,
                "round_robin",
                exc.result.outcomes,
                exc.result.failures,
                exc.result.value,
            )
            raise CoordinationFailed(failure) from exc
        return CoordinationResult(
            result.coordination_id,
            "round_robin",
            result.outcomes,
            result.failures,
            result.value,
        )

    async def parallel(
        self,
        branches: Sequence[tuple[str, Any]],
        *,
        sender: str = "user",
        coordination_id: str | None = None,
        max_concurrency: int | None = None,
        require_all: bool = True,
        context: RunContext | None = None,
        timeout_s: float | None = None,
        deadline: float | None = None,
    ) -> CoordinationResult:
        if not isinstance(branches, Sequence) or not branches:
            raise ValueError("parallel branches must be a non-empty sequence")
        if len(branches) > self.max_steps:
            raise ValueError("parallel branches exceed coordinator max_steps")
        prepared: list[HandoffEnvelope] = []
        coordination_id = _coordination_id(coordination_id, context)
        parent = _coordination_context(
            coordination_id, context=context, timeout_s=timeout_s,
            deadline=deadline,
        )
        for sequence, branch in enumerate(branches):
            if not isinstance(branch, tuple) or len(branch) != 2:
                raise TypeError("each parallel branch must be (recipient, payload)")
            recipient, payload = branch
            self._member(recipient)
            prepared.append(HandoffEnvelope.create(
                coordination_id=coordination_id,
                sender=sender,
                recipient=recipient,
                payload=payload,
                sequence=sequence,
            ))
        limit = self.max_concurrency if max_concurrency is None else _positive_int(
            max_concurrency, "max_concurrency",
        )
        limit = min(limit, self.max_concurrency)
        semaphore = asyncio.Semaphore(limit)

        async def run(envelope: HandoffEnvelope) -> HandoffOutcome:
            async with semaphore:
                parent.check()
                return await self.dispatch(envelope, context=parent)

        values = await asyncio.gather(
            *(run(envelope) for envelope in prepared),
            return_exceptions=True,
        )
        outcomes: list[HandoffOutcome] = []
        failures: list[HandoffFailure] = []
        for envelope, value in zip(prepared, values, strict=True):
            if isinstance(value, RunDeadlineExceeded):
                raise value
            if isinstance(value, asyncio.CancelledError):
                # asyncio Tasks normalize RunCancelled (a cooperative
                # CancelledError subtype) to CancelledError when gathered.
                # Restore the parent contract when it caused the cancellation.
                parent.check()
                raise value
            if isinstance(value, BaseException) and not isinstance(value, Exception):
                raise value
            if isinstance(value, BaseException):
                failures.append(_failure(envelope, value))
            else:
                outcomes.append(value)
        result = CoordinationResult(
            coordination_id,
            "parallel",
            tuple(outcomes),
            tuple(failures),
            tuple(outcome.value for outcome in outcomes),
        )
        if failures and require_all:
            raise CoordinationFailed(result)
        return result

    async def select(
        self,
        selector: str,
        candidates: Sequence[str],
        payload: Any,
        *,
        sender: str = "user",
        coordination_id: str | None = None,
        context: RunContext | None = None,
        timeout_s: float | None = None,
        deadline: float | None = None,
    ) -> CoordinationResult:
        candidate_names = self._member_sequence(candidates)
        self._member(selector)
        coordination_id = _coordination_id(coordination_id, context)
        parent = _coordination_context(
            coordination_id, context=context, timeout_s=timeout_s,
            deadline=deadline,
        )
        selector_envelope = HandoffEnvelope.create(
            coordination_id=coordination_id,
            sender=sender,
            recipient=selector,
            payload={
                "input": _json_value(payload, path="selector input"),
                "candidates": [
                    {
                        "name": name,
                        "description": self._members[name].info.description,
                        "version": self._members[name].info.version,
                    }
                    for name in candidate_names
                ],
            },
            sequence=0,
        )
        try:
            selected = await self.dispatch(selector_envelope, context=parent)
        except CoordinationError as exc:
            result = CoordinationResult(
                coordination_id,
                "selector",
                (),
                (_failure(selector_envelope, exc),),
            )
            raise CoordinationFailed(result) from exc
        try:
            target = _selected_member(selected.value)
        except CoordinationResultError as exc:
            result = CoordinationResult(
                coordination_id,
                "selector",
                (selected,),
                (_failure(selector_envelope, exc),),
                selected.value,
            )
            raise CoordinationFailed(result) from exc
        if target not in candidate_names:
            result = CoordinationResult(
                coordination_id,
                "selector",
                (selected,),
                (HandoffFailure(
                    selector_envelope,
                    selected.run_id,
                    "InvalidSelection",
                    f"selector returned non-candidate {target!r}",
                ),),
                selected.value,
            )
            raise CoordinationFailed(result)
        target_envelope = HandoffEnvelope.create(
            coordination_id=coordination_id,
            sender=selector,
            recipient=target,
            payload=payload,
            sequence=1,
            parent_id=selector_envelope.id,
        )
        try:
            outcome = await self.dispatch(target_envelope, context=parent)
        except CoordinationError as exc:
            result = CoordinationResult(
                coordination_id,
                "selector",
                (selected,),
                (_failure(target_envelope, exc),),
                selected.value,
            )
            raise CoordinationFailed(result) from exc
        return CoordinationResult(
            coordination_id,
            "selector",
            (selected, outcome),
            value=outcome.value,
        )

    async def map_reduce(
        self,
        branches: Sequence[tuple[str, Any]],
        reducer: str,
        *,
        sender: str = "user",
        coordination_id: str | None = None,
        max_concurrency: int | None = None,
        context: RunContext | None = None,
        timeout_s: float | None = None,
        deadline: float | None = None,
    ) -> CoordinationResult:
        """Run bounded branches, then hand their ordered results to a reducer."""
        if not isinstance(branches, Sequence) or not branches:
            raise ValueError("map-reduce branches must be a non-empty sequence")
        if len(branches) + 1 > self.max_steps:
            raise ValueError("map-reduce exceeds coordinator max_steps")
        self._member(reducer)
        coordination_id = _coordination_id(coordination_id, context)
        parent = _coordination_context(
            coordination_id,
            context=context,
            timeout_s=timeout_s,
            deadline=deadline,
        )
        try:
            mapped = await self.parallel(
                branches,
                sender=sender,
                coordination_id=coordination_id,
                max_concurrency=max_concurrency,
                require_all=True,
                context=parent,
            )
        except CoordinationFailed as exc:
            result = CoordinationResult(
                coordination_id,
                "map_reduce",
                exc.result.outcomes,
                exc.result.failures,
                exc.result.value,
            )
            raise CoordinationFailed(result) from exc
        parent_ids = [outcome.envelope.id for outcome in mapped.outcomes]
        envelope = HandoffEnvelope.create(
            coordination_id=coordination_id,
            sender="coordinator",
            recipient=reducer,
            payload={
                "results": [
                    {
                        "recipient": outcome.envelope.recipient,
                        "handoff_id": outcome.envelope.id,
                        "value": outcome.value,
                    }
                    for outcome in mapped.outcomes
                ],
            },
            sequence=len(branches),
            metadata={
                "strategy": "map_reduce",
                "parent_ids": parent_ids,
            },
        )
        try:
            reduced = await self.dispatch(envelope, context=parent)
        except CoordinationError as exc:
            result = CoordinationResult(
                coordination_id,
                "map_reduce",
                mapped.outcomes,
                (_failure(envelope, exc),),
                mapped.value,
            )
            raise CoordinationFailed(result) from exc
        return CoordinationResult(
            coordination_id,
            "map_reduce",
            (*mapped.outcomes, reduced),
            value=reduced.value,
        )

    async def swarm(
        self,
        initial_recipient: str,
        payload: Any,
        *,
        sender: str = "user",
        coordination_id: str | None = None,
        max_hops: int = 8,
        context: RunContext | None = None,
        timeout_s: float | None = None,
        deadline: float | None = None,
    ) -> CoordinationResult:
        self._member(initial_recipient)
        max_hops = _positive_int(max_hops, "max_hops")
        if max_hops > self.max_steps:
            raise ValueError("max_hops exceeds coordinator max_steps")
        coordination_id = _coordination_id(coordination_id, context)
        parent = _coordination_context(
            coordination_id, context=context, timeout_s=timeout_s,
            deadline=deadline,
        )
        outcomes: list[HandoffOutcome] = []
        recipient = initial_recipient
        current_sender = sender
        current_payload = payload
        previous: str | None = None
        for sequence in range(max_hops):
            envelope = HandoffEnvelope.create(
                coordination_id=coordination_id,
                sender=current_sender,
                recipient=recipient,
                payload=current_payload,
                sequence=sequence,
                parent_id=previous,
            )
            try:
                outcome = await self.dispatch(envelope, context=parent)
            except CoordinationError as exc:
                result = CoordinationResult(
                    coordination_id,
                    "swarm",
                    tuple(outcomes),
                    (_failure(envelope, exc),),
                    outcomes[-1].value if outcomes else current_payload,
                )
                raise CoordinationFailed(result) from exc
            outcomes.append(outcome)
            transfer = _transfer_from_value(outcome.value)
            if transfer is None:
                return CoordinationResult(
                    coordination_id,
                    "swarm",
                    tuple(outcomes),
                    value=outcome.value,
                )
            try:
                self._member(transfer.recipient)
            except KeyError as exc:
                result = CoordinationResult(
                    coordination_id,
                    "swarm",
                    tuple(outcomes),
                    (HandoffFailure(
                        envelope,
                        outcome.run_id,
                        "InvalidTransferRecipient",
                        f"member transferred to unknown recipient "
                        f"{transfer.recipient!r}",
                    ),),
                    outcome.value,
                )
                raise CoordinationFailed(result) from exc
            current_sender = recipient
            recipient = transfer.recipient
            current_payload = transfer.payload
            previous = envelope.id
        result = CoordinationResult(
            coordination_id,
            "swarm",
            tuple(outcomes),
            (HandoffFailure(
                outcomes[-1].envelope,
                outcomes[-1].run_id,
                "MaxHopsExceeded",
                f"swarm exceeded {max_hops} hops",
            ),),
            outcomes[-1].value,
        )
        raise CoordinationFailed(result)

    def close(self) -> None:
        if self._closed:
            return
        if self._owns_execution:
            self.execution.close()
        self._closed = True

    def __enter__(self) -> "AgentCoordinator":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("AgentCoordinator is closed")

    def _member(self, name: str) -> _Member:
        if not isinstance(name, str) or name not in self._members:
            raise KeyError(f"unknown member {name!r}")
        return self._members[name]

    def _member_sequence(self, recipients: Sequence[str]) -> tuple[str, ...]:
        if isinstance(recipients, (str, bytes)) or not isinstance(
            recipients, Sequence,
        ):
            raise TypeError("recipients must be a sequence of member names")
        values = tuple(recipients)
        if not values:
            raise ValueError("recipients must not be empty")
        if len(values) > self.max_steps:
            raise ValueError("recipients exceed coordinator max_steps")
        for value in values:
            self._member(value)
        return values

    def _claim_handoff(
        self,
        envelope: HandoffEnvelope,
        *,
        member: _Member,
        fingerprint: str,
    ) -> tuple[Run, bool]:
        task_id = _stable_id("coord_task", envelope.id)
        run_id = _stable_id("coord_run", envelope.id)
        goal = _handoff_goal(envelope, fingerprint)
        task = self.execution.get_task(task_id)
        if task is None:
            try:
                task = self.execution.create_task(
                    goal, self.workspace, task_id=task_id,
                )
            except ExecutionStateError:
                task = self.execution.get_task(task_id)
                if task is None:
                    raise
        if task.goal != goal or Path(task.workspace).resolve() != self.workspace:
            raise CoordinationIdentityConflict(
                f"handoff identity {envelope.id!r} has different durable input",
            )
        run = self.execution.get_run(run_id)
        if run is None:
            try:
                run = self.execution.create_run(task_id, run_id=run_id)
            except ExecutionStateError:
                run = self.execution.get_run(run_id)
                if run is None:
                    raise
        if run.task_id != task_id:
            raise CoordinationIdentityConflict(
                f"handoff run {run_id!r} belongs to another task",
            )
        if run.state is RunState.COMPLETED:
            return run, False
        if run.state is RunState.FAILED:
            error_type = str((run.error or {}).get("type", "MemberFailed"))
            raise HandoffExecutionError(envelope, run.id, error_type)
        if run.state is RunState.CANCELLED:
            try:
                self.execution.append_agent_event(
                    run.id,
                    AgentEventType.HANDOFF_CANCELLED,
                    identity="coordination:cancelled",
                    data=_event_data(envelope),
                )
            except Exception as exc:
                raise CoordinationRecoveryRequired(
                    f"cancelled handoff run {run.id!r} requires event repair",
                ) from exc
            raise RunCancelled(f"handoff run {run.id!r} was cancelled")
        if run.state is RunState.WAITING:
            pending = self.execution.list_interrupts(
                run_id=run.id,
                state=InterruptState.PENDING,
            )
            if not pending:
                raise CoordinationRecoveryRequired(
                    f"handoff run {run.id!r} is waiting without a pending interrupt",
                )
            raise CoordinationRecoveryRequired(
                f"handoff run {run.id!r} is waiting for interrupt "
                f"{pending[0].id!r}; resolve it before dispatching",
            )
        if run.state is RunState.RUNNING:
            if run.lease_expires is None or run.lease_expires > time.time():
                raise CoordinationBusy(
                    f"handoff run {run.id!r} is owned by a live worker",
                )
            # A SQLite-backed Agent has its own stable Effect tape and phase
            # checkpoints.  Reclaiming its coordination lease is therefore
            # the durable recovery path (completed effects replay; uncertain
            # effects fail closed as OrphanedEffectError).  Ordinary members
            # still require an explicit whole-invocation redelivery contract.
            if (
                not run.cancel_requested
                and not member.info.redelivery_safe
                and not (
                    self._is_durable_agent(member)
                    and self._agent_supports_durable(member.handler)
                )
            ):
                raise CoordinationRecoveryRequired(
                    f"handoff run {run.id!r} has an expired lease; member "
                    f"{member.info.name!r} did not declare redelivery_safe",
                )
        try:
            claimed = self.execution.claim_run(
                run.id, lease_seconds=self.lease_seconds,
            )
        except ExecutionLeaseError as exc:
            raise CoordinationBusy(
                f"handoff run {run.id!r} was claimed concurrently",
            ) from exc
        if claimed.cancel_requested:
            self._finish_cancelled(
                claimed.id,
                claimed.lease_token or "",
                envelope,
            )
            raise RunCancelled(f"handoff run {claimed.id!r} was cancelled")
        return claimed, True

    def _preflight_handoff_identity(
        self,
        envelope: HandoffEnvelope,
        fingerprint: str,
    ) -> None:
        """Reject an identity conflict before any shared reservation write."""
        task_id = _stable_id("coord_task", envelope.id)
        run_id = _stable_id("coord_run", envelope.id)
        task = self.execution.get_task(task_id)
        if task is not None:
            goal = _handoff_goal(envelope, fingerprint)
            if task.goal != goal or Path(task.workspace).resolve() != self.workspace:
                raise CoordinationIdentityConflict(
                    f"handoff identity {envelope.id!r} has different durable input",
                )
        run = self.execution.get_run(run_id)
        if run is not None and run.task_id != task_id:
            raise CoordinationIdentityConflict(
                f"handoff run {run_id!r} belongs to another task",
            )

    async def _run_claimed(
        self,
        envelope: HandoffEnvelope,
        *,
        member: _Member,
        run: Run,
        parent: RunContext,
        fingerprint: str,
    ) -> HandoffOutcome:
        from .agent import Agent

        if isinstance(member.handler, Agent):
            if self._agent_supports_durable(member.handler):
                return await self._run_claimed_agent(
                    envelope,
                    member=member,
                    run=run,
                    parent=parent,
                )
            if (
                member.approval_policy is not None
                or member.input_policy is not None
                or member.phase_timeout_s is not None
            ):
                error = ValueError(
                    "coordination Agent policies require a SQLite-backed "
                    "durable Agent session",
                )
                self._fail_claimed(run.id, run.lease_token or "", error, envelope)
                raise HandoffExecutionError(
                    envelope, run.id, type(error).__name__,
                ) from error
        token = run.lease_token or ""
        lease_lost = asyncio.Event()
        durable_cancel_requested = asyncio.Event()
        heartbeat_error: list[BaseException] = []
        branch = RunContext(
            run_id=run.id,
            deadline=parent.deadline,
            cancellation=parent.cancellation,
            metadata={
                **dict(parent.metadata),
                "coordination_id": envelope.coordination_id,
                "handoff_id": envelope.id,
                "sender": envelope.sender,
                "recipient": envelope.recipient,
            },
            cancel_check=lambda: (
                parent.cancelled
                or lease_lost.is_set()
                or durable_cancel_requested.is_set()
            ),
        )
        self.execution.append_agent_event(
            run.id,
            AgentEventType.HANDOFF_STARTED,
            identity="coordination:started",
            data=_event_data(envelope),
        )

        async def heartbeat() -> None:
            try:
                while True:
                    await asyncio.sleep(self.heartbeat_interval_s)
                    renewed = self.execution.renew_lease(
                        run.id,
                        token,
                        lease_seconds=self.lease_seconds,
                    )
                    if renewed.cancel_requested:
                        durable_cancel_requested.set()
                        return
            except asyncio.CancelledError:
                raise
            except BaseException as exc:
                heartbeat_error.append(exc)
                lease_lost.set()

        heartbeat_task = asyncio.create_task(heartbeat())
        try:
            try:
                with bind_run_context(branch):
                    raw = await branch.wait(
                        self._invoke_member(member, envelope, branch),
                    )
                branch.check()
                if heartbeat_error:
                    raise CoordinationRecoveryRequired(
                        f"lost lease for handoff run {run.id!r}",
                    ) from heartbeat_error[0]
                value = _member_result(raw)
                encoded = {
                    "coordination_version": _COORDINATION_VERSION,
                    "envelope_id": envelope.id,
                    "request_fingerprint": fingerprint,
                    "value": value,
                }
                _bounded_json(encoded, self.max_result_bytes, "member result")
                try:
                    completed = self.execution.complete_run(
                        run.id, token, result=encoded,
                    )
                except ExecutionLeaseError as exc:
                    raise CoordinationRecoveryRequired(
                        f"lost lease while settling handoff run {run.id!r}",
                    ) from exc
                except ExecutionStateError as exc:
                    current = self.execution.get_run(run.id)
                    if current is not None and current.cancel_requested:
                        raise RunCancelled(
                            f"handoff run {run.id!r} was cancelled",
                        ) from exc
                    raise
                try:
                    self.execution.append_agent_event(
                        run.id,
                        AgentEventType.HANDOFF_COMPLETED,
                        identity="coordination:completed",
                        data={**_event_data(envelope), "replayed": False},
                    )
                except Exception as exc:
                    # The Run is already terminal. A repeated dispatch will
                    # replay it and idempotently restore the public event.
                    raise CoordinationRecoveryRequired(
                        f"handoff run {run.id!r} completed but its event "
                        "requires repair",
                    ) from exc
                return HandoffOutcome(
                    envelope,
                    completed.id,
                    completed.attempt,
                    value,
                    False,
                )
            except (RunCancelled, RunDeadlineExceeded, asyncio.CancelledError):
                # Lease loss is an ownership uncertainty, not authorization to
                # cancel the logical handoff.  A replacement worker may already
                # own it, so never set cancel_requested in this branch.
                if lease_lost.is_set():
                    raise CoordinationRecoveryRequired(
                        f"lost lease for handoff run {run.id!r}",
                    ) from (heartbeat_error[0] if heartbeat_error else None)
                self._finish_cancelled(run.id, token, envelope)
                raise
            except CoordinationRecoveryRequired:
                raise
            except Exception as exc:
                self._fail_claimed(run.id, token, exc, envelope)
                raise HandoffExecutionError(
                    envelope, run.id, type(exc).__name__,
                ) from exc
            except BaseException as exc:
                self._fail_claimed(run.id, token, exc, envelope)
                raise
        finally:
            heartbeat_task.cancel()
            await asyncio.gather(heartbeat_task, return_exceptions=True)

    @staticmethod
    def _agent_supports_durable(handler: Any) -> bool:
        from .serialization.store_sqlite import SqliteClaimStore

        rowset = getattr(handler, "rowset", None)
        store = getattr(rowset, "store", None)
        return isinstance(store, SqliteClaimStore)

    @staticmethod
    def _is_durable_agent(member: _Member) -> bool:
        from .agent import Agent

        return isinstance(member.handler, Agent)

    async def _run_claimed_agent(
        self,
        envelope: HandoffEnvelope,
        *,
        member: _Member,
        run: Run,
        parent: RunContext,
    ) -> HandoffOutcome:
        """Run an Agent on the lease already claimed for this handoff.

        ``Agent.run_durable`` accepts ``_claimed_run`` specifically for this
        bridge. The durable ReAct runner owns heartbeat, checkpoints,
        interrupts, Effect recovery, and terminal settlement; the coordinator
        only records the handoff boundary and translates the terminal value.
        """
        from .agent import Agent
        assert isinstance(member.handler, Agent)
        if member.receives_envelope:
            raise ValueError(
                "receives_envelope=True is not supported for Agent members; "
                "use an async envelope adapter around the Agent",
            )
        branch = RunContext(
            run_id=run.id,
            deadline=parent.deadline,
            cancellation=parent.cancellation,
            metadata={
                **dict(parent.metadata),
                "coordination_id": envelope.coordination_id,
                "handoff_id": envelope.id,
                "sender": envelope.sender,
                "recipient": envelope.recipient,
            },
            cancel_check=lambda: parent.cancelled,
        )
        self.execution.append_agent_event(
            run.id,
            AgentEventType.HANDOFF_STARTED,
            identity="coordination:started",
            data=_event_data(envelope),
        )
        checkpoint = self.execution.get_checkpoint(run.id)
        prompt: Any = None if checkpoint is not None else _json_value(
            envelope.payload,
            path="member prompt",
        )
        try:
            with bind_run_context(branch):
                result = await member.handler.run_durable(
                    prompt,
                    execution_store=self.execution,
                    run_id=run.id,
                    lease_seconds=self.lease_seconds,
                    heartbeat_interval_s=self.heartbeat_interval_s,
                    phase_timeout_s=member.phase_timeout_s,
                    approval_policy=member.approval_policy,
                    input_policy=member.input_policy,
                    context=branch,
                    _claimed_run=run,
                    _initial_metadata={
                        "caused_by": envelope.id,
                        "coordination_id": envelope.coordination_id,
                        "sender": envelope.sender,
                        "recipient": envelope.recipient,
                    },
                )
        except RunSuspended:
            # The durable runner atomically moved the Run to WAITING and
            # released its lease. The caller resolves the Interrupt and calls
            # dispatch with the same envelope to resume from its checkpoint.
            raise
        except ExecutionLeaseError as exc:
            raise CoordinationRecoveryRequired(
                f"lost lease for durable Agent handoff run {run.id!r}",
            ) from exc
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            current = self.execution.get_run(run.id)
            if current is not None and current.state is RunState.FAILED:
                error_type = str((current.error or {}).get("type", type(exc).__name__))
                self.execution.append_agent_event(
                    run.id,
                    AgentEventType.HANDOFF_FAILED,
                    identity="coordination:failed",
                    data={
                        **_event_data(envelope),
                        "error_type": error_type,
                    },
                )
                raise HandoffExecutionError(
                    envelope, run.id, error_type,
                ) from exc
            if current is not None and current.state is RunState.CANCELLED:
                raise RunCancelled(
                    f"handoff run {run.id!r} was cancelled",
                ) from exc
            self._fail_claimed(run.id, run.lease_token or "", exc, envelope)
            raise HandoffExecutionError(
                envelope, run.id, type(exc).__name__,
            ) from exc

        current = self.execution.get_run(run.id)
        if current is None:
            raise CoordinationRecoveryRequired(
                f"durable Agent handoff run {run.id!r} disappeared",
            )
        if current.state is RunState.CANCELLED:
            self.execution.append_agent_event(
                run.id,
                AgentEventType.HANDOFF_CANCELLED,
                identity="coordination:cancelled",
                data=_event_data(envelope),
            )
            raise RunCancelled(f"handoff run {run.id!r} was cancelled")
        if current.state is RunState.FAILED:
            error_type = str((current.error or {}).get("type", "AgentFailed"))
            self.execution.append_agent_event(
                run.id,
                AgentEventType.HANDOFF_FAILED,
                identity="coordination:failed",
                data={
                    **_event_data(envelope),
                    "error_type": error_type,
                },
            )
            raise HandoffExecutionError(envelope, run.id, error_type)
        if current.state is not RunState.COMPLETED:
            raise CoordinationRecoveryRequired(
                f"durable Agent handoff run {run.id!r} settled in "
                f"unexpected state {current.state.value!r}",
            )
        value = _member_result(result)
        self.execution.append_agent_event(
            run.id,
            AgentEventType.HANDOFF_COMPLETED,
            identity="coordination:completed",
            data={**_event_data(envelope), "replayed": False},
        )
        return HandoffOutcome(
            envelope,
            current.id,
            current.attempt,
            value,
            False,
        )

    async def _invoke_member(
        self,
        member: _Member,
        envelope: HandoffEnvelope,
        context: RunContext,
    ) -> Any:
        from .agent import Agent

        if isinstance(member.handler, Agent):
            state = AgentState(metadata={
                "caused_by": envelope.id,
                "coordination_id": envelope.coordination_id,
                "sender": envelope.sender,
                "recipient": envelope.recipient,
            })
            result = await member.handler.run(
                _json_value(envelope.payload, path="member payload"),
                state=state,
                context=context,
            )
            # Agent.run represents cooperative cancellation/deadline as a
            # FinalResult. At a coordination boundary those controls still
            # own the handoff settlement, so restore their exception contract.
            context.check()
            return result
        member_envelope = _snapshot_envelope(envelope)
        argument = (
            member_envelope
            if member.receives_envelope
            else member_envelope.payload
        )
        handler = cast(MemberHandler, member.handler)
        return await handler(argument)

    def _replayed_outcome(
        self,
        envelope: HandoffEnvelope,
        run: Run,
        fingerprint: str,
        *,
        member: _Member,
    ) -> HandoffOutcome:
        result = run.result
        if not isinstance(result, Mapping):
            raise CoordinationResultError(
                f"completed handoff run {run.id!r} has no result mapping",
            )
        from .agent import Agent

        if (
            isinstance(member.handler, Agent)
            and self._agent_supports_durable(member.handler)
        ):
            from .durable import settled_result_from_run

            checkpoint = self.execution.get_checkpoint(run.id)
            claim_store_id = getattr(member.handler.rowset.store, "store_id", None)
            if not isinstance(claim_store_id, str) or not claim_store_id:
                raise CoordinationResultError(
                    "completed durable Agent handoff has no stable claim store",
                )
            try:
                durable_result = settled_result_from_run(
                    run,
                    checkpoint,
                    claim_store_id=claim_store_id,
                )
            except Exception as exc:
                raise CoordinationResultError(
                    f"completed durable Agent handoff run {run.id!r} "
                    "has no restorable terminal result",
                ) from exc
            value = _member_result(durable_result)
        else:
            if (
                result.get("coordination_version") != _COORDINATION_VERSION
                or result.get("envelope_id") != envelope.id
                or result.get("request_fingerprint") != fingerprint
                or "value" not in result
            ):
                raise CoordinationIdentityConflict(
                    f"completed handoff run {run.id!r} does not match its envelope",
                )
            value = result["value"]
        self.execution.append_agent_event(
            run.id,
            AgentEventType.HANDOFF_COMPLETED,
            identity="coordination:completed",
            data={**_event_data(envelope), "replayed": False},
        )
        return HandoffOutcome(
            envelope,
            run.id,
            run.attempt,
            value,
            True,
        )

    def _finish_cancelled(
        self,
        run_id: str,
        token: str,
        envelope: HandoffEnvelope,
    ) -> None:
        try:
            current = self.execution.request_cancel(run_id)
            if current.state is RunState.RUNNING:
                current = self.execution.finish_cancelled(run_id, token)
            if current.state is RunState.CANCELLED:
                self.execution.append_agent_event(
                    run_id,
                    AgentEventType.HANDOFF_CANCELLED,
                    identity="coordination:cancelled",
                    data=_event_data(envelope),
                )
        except (ExecutionLeaseError, ExecutionStateError):
            pass
        except Exception as exc:
            raise CoordinationRecoveryRequired(
                f"cancelled handoff run {run_id!r} requires settlement repair",
            ) from exc

    def _fail_claimed(
        self,
        run_id: str,
        token: str,
        error: BaseException,
        envelope: HandoffEnvelope,
    ) -> None:
        try:
            self.execution.fail_run(
                run_id,
                token,
                error={
                    "type": type(error).__name__,
                    "message": "coordination member failed",
                },
            )
            self.execution.append_agent_event(
                run_id,
                AgentEventType.HANDOFF_FAILED,
                identity="coordination:failed",
                data={
                    **_event_data(envelope),
                    "error_type": type(error).__name__,
                },
            )
        except (ExecutionLeaseError, ExecutionStateError):
            pass


def _event_data(envelope: HandoffEnvelope) -> dict[str, Any]:
    return {
        "coordination_id": envelope.coordination_id,
        "handoff_id": envelope.id,
        "sender": envelope.sender,
        "recipient": envelope.recipient,
        "sequence": envelope.sequence,
        "parent_id": envelope.parent_id,
    }


def _handoff_goal(envelope: HandoffEnvelope, fingerprint: str) -> str:
    return (
        f"lipas coordination v{_COORDINATION_VERSION}; "
        f"recipient={envelope.recipient}; request={fingerprint}; "
        f"envelope={envelope.id}"
    )


def _snapshot_envelope(envelope: HandoffEnvelope) -> HandoffEnvelope:
    """Deep-copy JSON fields so one dispatch has a stable durable request."""
    return HandoffEnvelope(
        id=envelope.id,
        coordination_id=envelope.coordination_id,
        sender=envelope.sender,
        recipient=envelope.recipient,
        payload=_json_value(envelope.payload, path="payload"),
        sequence=envelope.sequence,
        parent_id=envelope.parent_id,
        metadata=cast(
            Mapping[str, Any],
            _json_value(dict(envelope.metadata), path="metadata"),
        ),
        created_at=envelope.created_at,
    )


def _coordination_id(value: str | None, context: RunContext | None) -> str:
    result = value or (context.run_id if context is not None else None)
    if result is None:
        return f"coord_{uuid.uuid4().hex}"
    if not isinstance(result, str) or not result.strip():
        raise ValueError("coordination_id must be non-empty or None")
    return result


def _handoff_id(value: str | HandoffEnvelope) -> str:
    result = value.id if isinstance(value, HandoffEnvelope) else value
    if not isinstance(result, str) or not result.strip():
        raise ValueError("handoff id must be a non-empty string")
    return result


def _coordination_context(
    coordination_id: str,
    *,
    context: RunContext | None,
    timeout_s: float | None,
    deadline: float | None,
) -> RunContext:
    if context is not None:
        if timeout_s is not None or deadline is not None:
            raise ValueError("a supplied RunContext already owns its deadline")
        return context
    return RunContext.create(
        run_id=coordination_id,
        timeout_s=timeout_s,
        deadline=deadline,
        metadata={"coordination_id": coordination_id},
    )


def _envelope_request(
    envelope: HandoffEnvelope,
    member: MemberInfo,
) -> dict[str, Any]:
    return {
        "version": _COORDINATION_VERSION,
        "id": envelope.id,
        "coordination_id": envelope.coordination_id,
        "sender": envelope.sender,
        "recipient": envelope.recipient,
        "payload": envelope.payload,
        "sequence": envelope.sequence,
        "parent_id": envelope.parent_id,
        "metadata": dict(envelope.metadata),
        "member": {
            "name": member.name,
            "version": member.version,
            "capabilities": sorted(member.capabilities),
        },
    }


def _member_result(value: Any) -> Any:
    if isinstance(value, Transfer):
        return {
            "__lipas_coordination__": _TRANSFER_MARKER,
            "recipient": value.recipient,
            "payload": _json_value(value.payload, path="transfer payload"),
            "reason": value.reason,
        }
    if isinstance(value, FinalResult):
        return {
            "__lipas_coordination__": _AGENT_RESULT_MARKER,
            "text": value.text,
            "stop_reason": value.stop_reason,
            "error": _json_value(
                None if value.error is None else dict(value.error),
                path="Agent error",
            ),
            "metadata": _json_value(dict(value.metadata), path="Agent metadata"),
        }
    if isinstance(value, Mapping) and "__lipas_coordination__" in value:
        raise CoordinationResultError(
            "member result uses reserved __lipas_coordination__ field",
        )
    return _json_value(value, path="member result")


def _transfer_from_value(value: Any) -> Transfer | None:
    if not isinstance(value, Mapping) or value.get(
        "__lipas_coordination__",
    ) != _TRANSFER_MARKER:
        return None
    recipient = value.get("recipient")
    reason = value.get("reason", "")
    if not isinstance(recipient, str) or not isinstance(reason, str):
        raise CoordinationResultError("persisted Transfer has invalid fields")
    return Transfer(recipient, value.get("payload"), reason)


def _selected_member(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        if value.get("__lipas_coordination__") == _AGENT_RESULT_MARKER:
            selected = value.get("text")
        else:
            selected = value.get("recipient", value.get("selected"))
        if isinstance(selected, str):
            return selected
    raise CoordinationResultError("selector must return a member name")


def _failure(envelope: HandoffEnvelope, error: BaseException) -> HandoffFailure:
    run_id = getattr(error, "run_id", None)
    return HandoffFailure(
        envelope,
        run_id if isinstance(run_id, str) else None,
        type(error).__name__,
        str(error),
    )


def _json_value(
    value: Any,
    *,
    path: str,
    _depth: int = 0,
    _active: set[int] | None = None,
    _nodes: list[int] | None = None,
) -> Any:
    if _depth > _MAX_JSON_DEPTH:
        raise CoordinationResultError(
            f"{path} exceeds maximum JSON depth {_MAX_JSON_DEPTH}",
        )
    if _active is None:
        _active = set()
    if _nodes is None:
        _nodes = [0]
    _nodes[0] += 1
    if _nodes[0] > _MAX_JSON_NODES:
        raise CoordinationResultError(
            f"{path} exceeds maximum JSON node count {_MAX_JSON_NODES}",
        )
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise CoordinationResultError(f"{path} contains a non-finite float")
        return value
    if isinstance(value, (list, tuple, Mapping)):
        identity = id(value)
        if identity in _active:
            raise CoordinationResultError(f"{path} contains a reference cycle")
        _active.add(identity)
        try:
            if isinstance(value, (list, tuple)):
                return [
                    _json_value(
                        item,
                        path=f"{path}[{index}]",
                        _depth=_depth + 1,
                        _active=_active,
                        _nodes=_nodes,
                    )
                    for index, item in enumerate(value)
                ]
            result: dict[str, Any] = {}
            for key, item in value.items():
                if not isinstance(key, str):
                    raise CoordinationResultError(
                        f"{path} contains a non-string key",
                    )
                result[key] = _json_value(
                    item,
                    path=f"{path}.{key}",
                    _depth=_depth + 1,
                    _active=_active,
                    _nodes=_nodes,
                )
            return result
        finally:
            _active.remove(identity)
    raise CoordinationResultError(
        f"{path} contains unsupported {type(value).__name__}; "
        "return JSON-compatible data",
    )


def _bounded_json(value: Any, limit: int, name: str) -> str:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError, RecursionError) as exc:
        raise CoordinationResultError(
            f"{name} cannot be encoded as canonical JSON",
        ) from exc
    size = len(encoded.encode("utf-8"))
    if size > limit:
        raise CoordinationResultError(
            f"{name} is {size} bytes; limit is {limit}",
        )
    return encoded


def _encode_aggregate_cursor(positions: Mapping[str, int]) -> str:
    return json.dumps(
        {"version": 1, "runs": dict(sorted(positions.items()))},
        separators=(",", ":"),
        sort_keys=True,
    )


def _decode_aggregate_cursor(value: str | None) -> dict[str, int]:
    if value is None:
        return {}
    if not isinstance(value, str) or len(value.encode("utf-8")) > 1_000_000:
        raise ValueError("aggregate event cursor must be a bounded string")
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("aggregate event cursor is not valid JSON") from exc
    if not isinstance(decoded, Mapping) or decoded.get("version") != 1:
        raise ValueError("unsupported aggregate event cursor version")
    runs = decoded.get("runs")
    if not isinstance(runs, Mapping):
        raise ValueError("aggregate event cursor runs must be a mapping")
    result: dict[str, int] = {}
    for run_id, sequence in runs.items():
        if (
            not isinstance(run_id, str)
            or not run_id
            or isinstance(sequence, bool)
            or not isinstance(sequence, int)
            or sequence < 0
        ):
            raise ValueError("aggregate event cursor contains invalid run position")
        result[run_id] = sequence
    return result


def _stable_id(prefix: str, *parts: str) -> str:
    encoded = json.dumps(parts, ensure_ascii=False, separators=(",", ":"))
    return f"{prefix}_{hashlib.sha256(encoded.encode('utf-8')).hexdigest()}"


def _positive_int(value: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _positive_seconds(value: float, name: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or value <= 0
    ):
        raise ValueError(f"{name} must be a positive finite number")
    return float(value)
