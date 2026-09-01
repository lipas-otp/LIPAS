"""Bounded autonomous-workflow planning contracts.

The compiler in this module is deliberately a plan compiler, not an Agent
or a scheduler.  It turns a host supplied goal and constraints into a stable
``AgentPlan`` whose steps are either fixed (declared by the host) or adaptive
(left for an Agent to choose at execution time).  The result is inspectable
and serialisable; executing a step still requires the normal Task/Run,
Effect, policy, and approval boundaries.
"""
from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, field
import asyncio
import hashlib
import inspect
import json
import math
import time
from pathlib import Path
from typing import Any, Literal
from types import MappingProxyType

from .coordination import AgentPlan, PlanStep
from .context import RunCancelled

__all__ = [
    "WorkflowGoal",
    "WorkflowConstraint",
    "WorkflowStep",
    "StepMode",
    "CompiledWorkflow",
    "CompiledPlan",
    "MixedPlan",
    "AutonomousWorkflowCompiler",
    "WorkflowCompiler",
    "PlanCompiler",
    "compile_workflow",
    "compile_goal",
    "WorkflowStepResult",
    "WorkflowExecutionResult",
    "execute_compiled_workflow",
    "execute_workflow",
]


StepMode = Literal["fixed", "adaptive"]


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


def _json_copy(value: Any, name: str) -> Any:
    """Copy and validate planner data without accepting NaN or live objects."""
    _validate_json_shape(value, name)
    try:
        payload = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be strict JSON") from exc
    return json.loads(payload)


def _validate_json_shape(value: Any, path: str, *, _active: set[int] | None = None) -> None:
    """Reject coercive JSON encodings such as integer object keys."""
    if value is None or isinstance(value, (bool, int, str)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{path} must contain finite numbers")
        return
    if _active is None:
        _active = set()
    if isinstance(value, (list, tuple, Mapping)):
        identity = id(value)
        if identity in _active:
            raise ValueError(f"{path} must not contain reference cycles")
        _active.add(identity)
        try:
            if isinstance(value, Mapping):
                for key, item in value.items():
                    if not isinstance(key, str):
                        raise ValueError(f"{path} must use string object keys")
                    _validate_json_shape(item, f"{path}.{key}", _active=_active)
            else:
                for index, item in enumerate(value):
                    _validate_json_shape(item, f"{path}[{index}]", _active=_active)
        finally:
            _active.remove(identity)
        return
    raise TypeError(f"{path} contains unsupported {type(value).__name__}")


def _names(value: Iterable[str], name: str) -> frozenset[str]:
    if isinstance(value, (str, bytes, bytearray)):
        raise TypeError(f"{name} must be an iterable of strings, not a string")
    try:
        items = tuple(value)
    except TypeError as exc:
        raise TypeError(f"{name} must be an iterable of strings") from exc
    normalized = tuple(_text(item, name) for item in items)
    if len(set(normalized)) != len(normalized):
        raise ValueError(f"{name} must contain unique strings")
    return frozenset(normalized)


@dataclass(frozen=True, slots=True)
class WorkflowConstraint:
    """One named constraint attached to a workflow goal.

    Constraints are data, not policy.  A hard constraint is a compiler input
    that must be preserved by every generated step; a soft constraint is a
    preference that adaptive execution may report as unsatisfied.  Neither
    form grants a capability or bypasses approval.
    """

    name: str
    value: Any
    hard: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _text(self.name, "constraint name"))
        if not isinstance(self.hard, bool):
            raise TypeError("constraint hard must be bool")
        object.__setattr__(self, "value", _json_copy(self.value, "constraint value"))

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "value": deepcopy(self.value), "hard": self.hard}


@dataclass(frozen=True, slots=True)
class WorkflowGoal:
    """A host-owned objective and its bounded planning envelope."""

    goal: str
    constraints: tuple[WorkflowConstraint, ...] = ()
    workspace: str = "."
    conversation_id: str | None = None
    max_adaptive_steps: int = 4

    def __post_init__(self) -> None:
        object.__setattr__(self, "goal", _text(self.goal, "goal"))
        if not isinstance(self.constraints, tuple):
            raise TypeError("constraints must be a tuple of WorkflowConstraint")
        if any(not isinstance(item, WorkflowConstraint) for item in self.constraints):
            raise TypeError("constraints must be a tuple of WorkflowConstraint")
        if len({item.name for item in self.constraints}) != len(self.constraints):
            raise ValueError("constraint names must be unique")
        # Constraint order is semantically irrelevant. Canonicalising it
        # keeps the generated plan identity stable when a caller constructs
        # an equivalent mapping in a different insertion order.
        object.__setattr__(
            self,
            "constraints",
            tuple(sorted(self.constraints, key=lambda item: item.name)),
        )
        root = Path(_text(self.workspace, "workspace")).expanduser().resolve()
        object.__setattr__(self, "workspace", str(root))
        if self.conversation_id is not None:
            object.__setattr__(self, "conversation_id", _text(self.conversation_id, "conversation_id"))
        if (
            isinstance(self.max_adaptive_steps, bool)
            or not isinstance(self.max_adaptive_steps, int)
            or self.max_adaptive_steps < 0
        ):
            raise ValueError("max_adaptive_steps must be a non-negative int")

    @classmethod
    def from_mapping(
        cls,
        goal: str,
        *,
        constraints: Mapping[str, Any] | Sequence[WorkflowConstraint] | None = None,
        workspace: str | Path = ".",
        conversation_id: str | None = None,
        max_adaptive_steps: int = 4,
    ) -> "WorkflowGoal":
        if constraints is None:
            normalized: tuple[WorkflowConstraint, ...] = ()
        elif isinstance(constraints, Mapping):
            normalized = tuple(
                WorkflowConstraint(name, value) for name, value in constraints.items()
            )
        else:
            if isinstance(constraints, (str, bytes, bytearray)):
                raise TypeError("constraints must be a mapping or sequence")
            raw_constraints = tuple(constraints)
            normalized_items: list[WorkflowConstraint] = []
            for item in raw_constraints:
                if isinstance(item, WorkflowConstraint):
                    normalized_items.append(item)
                elif isinstance(item, Mapping):
                    normalized_items.append(WorkflowConstraint(**dict(item)))
                else:
                    raise TypeError(
                        "constraints must contain WorkflowConstraint or mappings",
                    )
            normalized = tuple(normalized_items)
        return cls(
            goal,
            normalized,
            str(workspace),
            conversation_id,
            max_adaptive_steps,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "goal": self.goal,
            "constraints": [item.as_dict() for item in self.constraints],
            "workspace": self.workspace,
            "conversation_id": self.conversation_id,
            "max_adaptive_steps": self.max_adaptive_steps,
        }


@dataclass(frozen=True, slots=True)
class WorkflowStep:
    """A mixed-plan step with an explicit fixed/adaptive mode."""

    step_id: str
    goal: str
    mode: StepMode = "adaptive"
    recipient: str = "agent"
    depends_on: tuple[str, ...] = ()
    required_capabilities: frozenset[str] = frozenset()
    metadata: Mapping[str, Any] = field(default_factory=dict)
    constraints: tuple[WorkflowConstraint, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "step_id", _text(self.step_id, "step_id"))
        object.__setattr__(self, "goal", _text(self.goal, "step goal"))
        object.__setattr__(self, "recipient", _text(self.recipient, "recipient"))
        if not isinstance(self.mode, str) or self.mode not in {"fixed", "adaptive"}:
            raise ValueError("workflow step mode must be fixed or adaptive")
        if not isinstance(self.depends_on, tuple):
            raise TypeError("depends_on must be a tuple")
        normalized_deps = tuple(_text(item, "depends_on") for item in self.depends_on)
        if len(set(normalized_deps)) != len(normalized_deps):
            raise ValueError("depends_on must contain unique step ids")
        # Dependency order is not semantic; canonicalise it so equivalent
        # declarations produce the same plan fingerprint and durable handoff
        # identity regardless of caller insertion order.
        object.__setattr__(self, "depends_on", tuple(sorted(normalized_deps)))
        object.__setattr__(
            self,
            "required_capabilities",
            _names(self.required_capabilities, "required_capabilities"),
        )
        if not isinstance(self.metadata, Mapping):
            raise TypeError("workflow step metadata must be a mapping")
        object.__setattr__(
            self,
            "metadata",
            MappingProxyType(_json_copy(dict(self.metadata), "step metadata")),
        )
        if not isinstance(self.constraints, tuple) or any(
            not isinstance(item, WorkflowConstraint) for item in self.constraints
        ):
            raise TypeError("workflow step constraints must be a tuple of WorkflowConstraint")
        if len({item.name for item in self.constraints}) != len(self.constraints):
            raise ValueError("workflow step constraint names must be unique")
        object.__setattr__(
            self,
            "constraints",
            tuple(sorted(self.constraints, key=lambda item: item.name)),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "step_id": self.step_id,
            "goal": self.goal,
            "mode": self.mode,
            "recipient": self.recipient,
            "depends_on": list(self.depends_on),
            "required_capabilities": sorted(self.required_capabilities),
            "metadata": deepcopy(dict(self.metadata)),
            "constraints": [item.as_dict() for item in self.constraints],
        }

    def as_plan_step(self) -> PlanStep:
        metadata = {**dict(self.metadata), "workflow_mode": self.mode}
        if self.constraints:
            metadata["workflow_constraints"] = [
                item.as_dict() for item in self.constraints
            ]
        return PlanStep(
            self.step_id,
            self.goal,
            self.recipient,
            self.depends_on,
            self.required_capabilities,
            metadata,
        )


@dataclass(frozen=True, slots=True)
class CompiledWorkflow:
    """Deterministic, inspectable output of ``AutonomousWorkflowCompiler``."""

    plan_id: str
    goal: WorkflowGoal
    steps: tuple[WorkflowStep, ...]
    plan: AgentPlan
    fingerprint: str

    def __post_init__(self) -> None:
        if not isinstance(self.plan_id, str) or not self.plan_id.strip():
            raise ValueError("plan_id must be non-empty")
        if not isinstance(self.goal, WorkflowGoal):
            raise TypeError("goal must be WorkflowGoal")
        if not isinstance(self.steps, tuple) or not self.steps:
            raise ValueError("compiled workflow steps must be non-empty")
        if any(not isinstance(item, WorkflowStep) for item in self.steps):
            raise TypeError("steps must be a tuple of WorkflowStep")
        if not isinstance(self.plan, AgentPlan):
            raise TypeError("plan must be AgentPlan")
        if self.plan.plan_id != self.plan_id:
            raise ValueError("compiled workflow plan_id does not match AgentPlan")
        if tuple(self.plan.steps) != tuple(step.as_plan_step() for step in self.steps):
            raise ValueError("compiled workflow steps do not match AgentPlan")
        expected = _fingerprint(self.plan_id, self.goal, self.steps)
        if self.fingerprint != expected:
            raise ValueError("compiled workflow fingerprint does not match its contents")

    @property
    def fixed_steps(self) -> tuple[WorkflowStep, ...]:
        return tuple(item for item in self.steps if item.mode == "fixed")

    @property
    def adaptive_steps(self) -> tuple[WorkflowStep, ...]:
        return tuple(item for item in self.steps if item.mode == "adaptive")

    @property
    def max_adaptive_steps(self) -> int:
        return self.goal.max_adaptive_steps

    def as_dict(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "fingerprint": self.fingerprint,
            "goal": self.goal.as_dict(),
            "steps": [item.as_dict() for item in self.steps],
            "fixed_steps": [item.step_id for item in self.fixed_steps],
            "adaptive_steps": [item.step_id for item in self.adaptive_steps],
            "max_adaptive_steps": self.max_adaptive_steps,
        }

    def handoff(self, step_id: str, *, sender: str, payload: Any) -> Any:
        """Create the ordinary durable handoff envelope for one plan step."""
        if not any(item.step_id == step_id for item in self.steps):
            raise KeyError(step_id)
        return self.plan.handoff(step_id, sender=sender, payload=payload)


class AutonomousWorkflowCompiler:
    """Compile a mixed fixed/adaptive plan without invoking a model.

    ``fixed_steps`` may be ``WorkflowStep`` objects or mappings containing
    ``step_id`` and ``goal``.  ``adaptive_steps`` can be a count (the compiler
    creates one bounded decision step) or explicit ``WorkflowStep`` objects.
    Explicit plans are preferred in production because they make the fixed
    contract reviewable; the compact goal-only form is useful for chat hosts.
    """

    def __init__(self, *, default_recipient: str = "agent") -> None:
        self.default_recipient = _text(default_recipient, "default_recipient")

    def compile(
        self,
        goal: WorkflowGoal | str,
        *,
        constraints: Mapping[str, Any] | Sequence[WorkflowConstraint] | None = None,
        workspace: str | Path = ".",
        conversation_id: str | None = None,
        fixed_steps: Iterable[WorkflowStep | Mapping[str, Any]] = (),
        adaptive_steps: int | Iterable[WorkflowStep | Mapping[str, Any]] = 1,
        plan_id: str | None = None,
        max_adaptive_steps: int = 4,
    ) -> CompiledWorkflow:
        workflow_goal = (
            goal
            if isinstance(goal, WorkflowGoal)
            else WorkflowGoal.from_mapping(
                goal,
                constraints=constraints,
                workspace=workspace,
                conversation_id=conversation_id,
                max_adaptive_steps=max_adaptive_steps,
            )
        )
        if not isinstance(workflow_goal, WorkflowGoal):
            raise TypeError("goal must be a WorkflowGoal or string")
        if isinstance(goal, WorkflowGoal):
            # A WorkflowGoal is an immutable, host-owned planning snapshot.
            # Silently ignoring duplicate keyword inputs would make callers
            # believe they changed the plan while the compiler used the old
            # values, so reject meaningful conflicts explicitly.  The default
            # ``workspace='.'`` and ``max_adaptive_steps=4`` are treated as
            # omitted for ergonomic compatibility.
            if constraints is not None:
                supplied_goal = WorkflowGoal.from_mapping(
                    workflow_goal.goal,
                    constraints=constraints,
                    workspace=workflow_goal.workspace,
                    conversation_id=workflow_goal.conversation_id,
                    max_adaptive_steps=workflow_goal.max_adaptive_steps,
                )
                if supplied_goal.constraints != workflow_goal.constraints:
                    raise ValueError("constraints conflict with WorkflowGoal")
            if conversation_id is not None:
                normalized_conversation = _text(conversation_id, "conversation_id")
                if normalized_conversation != workflow_goal.conversation_id:
                    raise ValueError("conversation_id conflicts with WorkflowGoal")
            if workspace != "." and str(Path(workspace).expanduser().resolve()) != workflow_goal.workspace:
                raise ValueError("workspace conflicts with WorkflowGoal")
            if max_adaptive_steps != 4 and max_adaptive_steps != workflow_goal.max_adaptive_steps:
                raise ValueError("max_adaptive_steps conflicts with WorkflowGoal")
        fixed = self._coerce_steps(fixed_steps, "fixed", workflow_goal)
        if isinstance(adaptive_steps, bool) or isinstance(adaptive_steps, int):
            count = adaptive_steps
            if isinstance(count, bool) or not isinstance(count, int) or count < 0:
                raise ValueError("adaptive_steps count must be a non-negative int")
            if count > workflow_goal.max_adaptive_steps:
                raise ValueError("adaptive_steps exceeds max_adaptive_steps")
            adaptive = self._default_adaptive_steps(
                workflow_goal,
                count,
                recipient=self.default_recipient,
                depends_on=tuple(step.step_id for step in fixed),
            )
        else:
            adaptive = self._coerce_steps(adaptive_steps, "adaptive", workflow_goal)
        fixed = tuple(self._bind_constraints(step, workflow_goal) for step in fixed)
        adaptive = tuple(self._bind_constraints(step, workflow_goal) for step in adaptive)
        if len(adaptive) > workflow_goal.max_adaptive_steps:
            raise ValueError("adaptive_steps exceeds max_adaptive_steps")
        adaptive = tuple(
            self._bound_adaptive_step(step, workflow_goal.max_adaptive_steps)
            for step in adaptive
        )
        steps = self._validate_dependencies((*fixed, *adaptive))
        if not steps:
            raise ValueError("compiled workflow must contain at least one step")
        identifier = _text(plan_id, "plan_id") if plan_id is not None else _stable_plan_id(workflow_goal, steps)
        plan = AgentPlan(
            identifier,
            workflow_goal.conversation_id or f"workflow:{identifier}",
            tuple(item.as_plan_step() for item in steps),
        )
        fingerprint = _fingerprint(identifier, workflow_goal, steps)
        return CompiledWorkflow(identifier, workflow_goal, steps, plan, fingerprint)

    def compile_goal(self, goal: WorkflowGoal | str, **kwargs: Any) -> CompiledWorkflow:
        """Named alias for hosts that treat compilation as goal planning."""
        return self.compile(goal, **kwargs)

    def _coerce_steps(
        self,
        values: Iterable[WorkflowStep | Mapping[str, Any]],
        mode: StepMode,
        goal: WorkflowGoal,
    ) -> tuple[WorkflowStep, ...]:
        if isinstance(values, (str, bytes, bytearray)):
            raise TypeError("workflow steps must be an iterable, not a string")
        try:
            raw = tuple(values)
        except TypeError as exc:
            raise TypeError("workflow steps must be iterable") from exc
        result: list[WorkflowStep] = []
        for item in raw:
            if isinstance(item, WorkflowStep):
                if item.mode != mode:
                    raise ValueError(f"step {item.step_id!r} has mode {item.mode!r}, expected {mode!r}")
                result.append(item)
                continue
            if not isinstance(item, Mapping):
                raise TypeError("workflow steps must contain WorkflowStep or mappings")
            data = dict(item)
            data.setdefault("mode", mode)
            data.setdefault("recipient", self.default_recipient)
            metadata = dict(data.pop("metadata", {}) or {})
            # ``max_steps`` is a common planning spelling; accept it at the
            # mapping boundary while keeping the durable step contract's
            # extensible metadata namespace explicit.
            if "max_steps" in data:
                metadata.setdefault("max_steps", data.pop("max_steps"))
            data["metadata"] = metadata
            step = WorkflowStep(**data)
            if step.mode != mode:
                raise ValueError(
                    f"step {step.step_id!r} has mode {step.mode!r}, expected {mode!r}",
                )
            result.append(step)
        return tuple(result)

    @staticmethod
    def _bind_constraints(step: WorkflowStep, goal: WorkflowGoal) -> WorkflowStep:
        """Carry the immutable goal constraints onto every compiled step."""
        declared = tuple(goal.constraints)
        existing = tuple(step.constraints)
        if existing and existing != declared:
            raise ValueError(
                f"step {step.step_id!r} constraints conflict with WorkflowGoal",
            )
        if existing == declared:
            return step
        return WorkflowStep(
            step.step_id,
            step.goal,
            step.mode,
            step.recipient,
            step.depends_on,
            step.required_capabilities,
            step.metadata,
            declared,
        )

    @staticmethod
    def _bound_adaptive_step(step: WorkflowStep, maximum: int) -> WorkflowStep:
        """Attach and validate the per-plan bound on an adaptive step."""
        metadata = dict(step.metadata)
        declared = metadata.get("max_steps", maximum)
        if (
            isinstance(declared, bool)
            or not isinstance(declared, int)
            or declared < 0
            or declared > maximum
        ):
            raise ValueError("adaptive step max_steps exceeds max_adaptive_steps")
        metadata["max_steps"] = declared
        if metadata == dict(step.metadata):
            return step
        return WorkflowStep(
            step.step_id,
            step.goal,
            "adaptive",
            step.recipient,
            step.depends_on,
            step.required_capabilities,
            metadata,
            step.constraints,
        )

    @staticmethod
    def _default_adaptive_steps(
        goal: WorkflowGoal,
        count: int,
        *,
        recipient: str,
        depends_on: tuple[str, ...] = (),
    ) -> tuple[WorkflowStep, ...]:
        if count == 0:
            return ()
        steps: list[WorkflowStep] = []
        dependency = depends_on
        for index in range(1, count + 1):
            step_id = f"adaptive-{index}"
            steps.append(WorkflowStep(
                step_id,
                f"Choose bounded action {index} for: {goal.goal}",
                "adaptive",
                recipient,
                dependency,
                frozenset(),
                {"max_steps": goal.max_adaptive_steps, "ordinal": index},
            ))
            dependency = (step_id,)
        return tuple(steps)

    @staticmethod
    def _validate_dependencies(steps: tuple[WorkflowStep, ...]) -> tuple[WorkflowStep, ...]:
        ids = [item.step_id for item in steps]
        if len(set(ids)) != len(ids):
            raise ValueError("workflow step ids must be unique")
        known = set(ids)
        for item in steps:
            if item.step_id in item.depends_on:
                raise ValueError(f"workflow step {item.step_id!r} cannot depend on itself")
            if any(dep not in known for dep in item.depends_on):
                raise ValueError(f"workflow step {item.step_id!r} has an unknown dependency")
        # Kahn's algorithm catches cycles while preserving caller order.
        remaining = {item.step_id: set(item.depends_on) for item in steps}
        visited: list[str] = []
        while remaining:
            ready = [identifier for identifier, deps in remaining.items() if not deps]
            if not ready:
                raise ValueError("workflow step dependencies contain a cycle")
            for identifier in ready:
                visited.append(identifier)
                remaining.pop(identifier)
                for deps in remaining.values():
                    deps.discard(identifier)
        return steps


WorkflowCompiler = AutonomousWorkflowCompiler
PlanCompiler = AutonomousWorkflowCompiler
CompiledPlan = CompiledWorkflow
MixedPlan = CompiledWorkflow


def compile_workflow(
    goal: WorkflowGoal | str,
    **kwargs: Any,
) -> CompiledWorkflow:
    """Functional convenience wrapper around ``AutonomousWorkflowCompiler``."""
    return AutonomousWorkflowCompiler().compile(goal, **kwargs)


def compile_goal(goal: WorkflowGoal | str, **kwargs: Any) -> CompiledWorkflow:
    """Functional alias for :func:`compile_workflow`."""
    return compile_workflow(goal, **kwargs)


@dataclass(frozen=True, slots=True)
class WorkflowStepResult:
    """Durable-friendly result for one executed compiled step."""

    step_id: str
    mode: StepMode
    status: Literal["succeeded", "failed", "skipped"]
    output: Any = None
    error_type: str | None = None
    error_message: str | None = None
    started_at: float | None = None
    finished_at: float | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "step_id", _text(self.step_id, "step_id"))
        if self.mode not in {"fixed", "adaptive"}:
            raise ValueError("workflow step result mode is invalid")
        if self.status not in {"succeeded", "failed", "skipped"}:
            raise ValueError("workflow step result status is invalid")
        if self.error_type is not None:
            object.__setattr__(self, "error_type", _text(self.error_type, "error_type"))
        if self.error_message is not None:
            object.__setattr__(self, "error_message", str(self.error_message)[:512])
        if self.status == "succeeded":
            object.__setattr__(self, "output", _json_copy(self.output, "step output"))
        else:
            object.__setattr__(self, "output", None)
        for name in ("started_at", "finished_at"):
            value = getattr(self, name)
            if value is not None:
                try:
                    numeric = float(value)
                except (TypeError, ValueError, OverflowError) as exc:
                    raise ValueError(f"{name} must be a finite number") from exc
                if not math.isfinite(numeric):
                    raise ValueError(f"{name} must be a finite number")
                object.__setattr__(self, name, numeric)

    def as_dict(self) -> dict[str, Any]:
        return {
            "step_id": self.step_id,
            "mode": self.mode,
            "status": self.status,
            "output": deepcopy(self.output),
            "error_type": self.error_type,
            "error_message": self.error_message,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }


@dataclass(frozen=True, slots=True)
class WorkflowExecutionResult:
    """Outcome of executing a compiled plan through a host-owned callback."""

    plan_id: str
    fingerprint: str
    status: Literal["succeeded", "failed", "cancelled"]
    steps: tuple[WorkflowStepResult, ...]
    started_at: float
    finished_at: float
    error: Mapping[str, str] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "plan_id", _text(self.plan_id, "plan_id"))
        object.__setattr__(self, "fingerprint", _text(self.fingerprint, "fingerprint"))
        if self.status not in {"succeeded", "failed", "cancelled"}:
            raise ValueError("workflow execution status is invalid")
        if not isinstance(self.steps, tuple) or any(
            not isinstance(item, WorkflowStepResult) for item in self.steps
        ):
            raise TypeError("steps must be a tuple of WorkflowStepResult")
        if len({item.step_id for item in self.steps}) != len(self.steps):
            raise ValueError("workflow execution step ids must be unique")
        for name in ("started_at", "finished_at"):
            numeric = float(getattr(self, name))
            if not math.isfinite(numeric):
                raise ValueError(f"{name} must be finite")
            object.__setattr__(self, name, numeric)
        if self.finished_at < self.started_at:
            raise ValueError("finished_at cannot precede started_at")
        if self.error is not None:
            if not isinstance(self.error, Mapping):
                raise TypeError("workflow execution error must be a mapping")
            object.__setattr__(
                self, "error", _json_copy(dict(self.error), "workflow execution error"),
            )

    @property
    def succeeded(self) -> bool:
        return self.status == "succeeded"

    @property
    def cancelled(self) -> bool:
        """Whether cooperative cancellation stopped this workflow."""
        return self.status == "cancelled"

    def as_dict(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "fingerprint": self.fingerprint,
            "status": self.status,
            "steps": [item.as_dict() for item in self.steps],
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "error": None if self.error is None else deepcopy(dict(self.error)),
        }


def _workflow_order(workflow: CompiledWorkflow) -> tuple[WorkflowStep, ...]:
    """Return a stable topological order, preserving declaration order ties."""
    pending = list(workflow.steps)
    completed: set[str] = set()
    ordered: list[WorkflowStep] = []
    while pending:
        ready = [item for item in pending if set(item.depends_on) <= completed]
        if not ready:
            raise ValueError("compiled workflow dependencies contain a cycle")
        for item in ready:
            pending.remove(item)
            ordered.append(item)
            completed.add(item.step_id)
    return tuple(ordered)


async def execute_compiled_workflow(
    workflow: CompiledWorkflow,
    executor: Any,
    *,
    context: Mapping[str, Any] | None = None,
    cancel_check: Callable[[], bool] | None = None,
) -> WorkflowExecutionResult:
    """Execute a compiled workflow with one host-owned step callback.

    The callback receives ``(WorkflowStep, prior_outputs)`` and may return a
    strict-JSON value or an awaitable.  It is the callback's responsibility to
    use :meth:`LIPASRuntime.execute_effect_for_run` for world-changing work;
    this executor only provides bounded ordering and an inspectable result.
    """
    if not isinstance(workflow, CompiledWorkflow):
        raise TypeError("workflow must be a CompiledWorkflow")
    if not callable(executor):
        raise TypeError("executor must be callable")
    if cancel_check is not None and not callable(cancel_check):
        raise TypeError("cancel_check must be callable or None")
    if context is None:
        base_context: Mapping[str, Any] = {}
    else:
        if not isinstance(context, Mapping):
            raise TypeError("context must be a mapping")
        base_context = _json_copy(dict(context), "workflow context")
    started = time.time()
    results: list[WorkflowStepResult] = []
    outputs: dict[str, Any] = {}
    failure: Mapping[str, str] | None = None
    cancelled = False
    try:
        ordered = _workflow_order(workflow)
        for index, step in enumerate(ordered):
            if failure is not None:
                results.append(WorkflowStepResult(step.step_id, step.mode, "skipped"))
                continue
            if cancel_check is not None and cancel_check():
                failure = {
                    "type": "RunCancelled",
                    "message": "workflow cancellation was requested",
                }
                cancelled = True
                results.extend(
                    WorkflowStepResult(item.step_id, item.mode, "skipped")
                    for item in ordered[index:]
                )
                break
            dependencies = {key: outputs[key] for key in step.depends_on if key in outputs}
            step_context = {**dict(base_context), "outputs": dependencies}
            step_started = time.time()
            try:
                value = executor(step, step_context)
                if inspect.isawaitable(value):
                    value = await value
                output = _json_copy(value, f"output for step {step.step_id!r}")
            except RunCancelled as exc:
                failure = {
                    "step_id": step.step_id,
                    "type": type(exc).__name__,
                    "message": str(exc)[:512] or "workflow cancellation was requested",
                }
                cancelled = True
                results.append(WorkflowStepResult(
                    step.step_id, step.mode, "skipped",
                    error_type=type(exc).__name__,
                    error_message=str(exc) or "workflow cancellation was requested",
                    started_at=step_started, finished_at=time.time(),
                ))
                results.extend(
                    WorkflowStepResult(item.step_id, item.mode, "skipped")
                    for item in ordered[index + 1:]
                )
                break
            except Exception as exc:
                failure = {
                    "step_id": step.step_id,
                    "type": type(exc).__name__,
                    "message": str(exc)[:512],
                }
                results.append(WorkflowStepResult(
                    step.step_id, step.mode, "failed",
                    error_type=type(exc).__name__, error_message=str(exc),
                    started_at=step_started, finished_at=time.time(),
                ))
                continue
            outputs[step.step_id] = output
            results.append(WorkflowStepResult(
                step.step_id, step.mode, "succeeded", output=output,
                started_at=step_started, finished_at=time.time(),
            ))
    except Exception as exc:
        failure = {"type": type(exc).__name__, "message": str(exc)[:512]}
    return WorkflowExecutionResult(
        workflow.plan_id,
        workflow.fingerprint,
        "cancelled" if cancelled else ("failed" if failure is not None else "succeeded"),
        tuple(results),
        started,
        time.time(),
        failure,
    )


def execute_workflow(
    workflow: CompiledWorkflow,
    executor: Any,
    *,
    context: Mapping[str, Any] | None = None,
    cancel_check: Callable[[], bool] | None = None,
) -> WorkflowExecutionResult:
    """Synchronous convenience wrapper for :func:`execute_compiled_workflow`."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(
            execute_compiled_workflow(
                workflow,
                executor,
                context=context,
                cancel_check=cancel_check,
            ),
        )
    raise RuntimeError("execute_workflow cannot run inside an active event loop; await execute_compiled_workflow")


def _stable_plan_id(goal: WorkflowGoal, steps: tuple[WorkflowStep, ...]) -> str:
    digest = hashlib.sha256(
        json.dumps(
            {"goal": goal.as_dict(), "steps": [item.as_dict() for item in steps]},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8"),
    ).hexdigest()[:32]
    return f"plan_auto_{digest}"


def _fingerprint(
    plan_id: str,
    goal: WorkflowGoal,
    steps: tuple[WorkflowStep, ...],
) -> str:
    payload = {
        "plan_id": plan_id,
        "goal": goal.as_dict(),
        "steps": [item.as_dict() for item in steps],
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8"),
    ).hexdigest()
