"""LIPAS — trustworthy AI execution with durable, auditable agent work.

The provider-neutral interchange surface is ``lipas.adapter``.
"""
from __future__ import annotations

from ._version import __version__
from .tools import SideEffectClass, tool
from .session import open_session, replay
from .trace import render_trace, write_jsonl
from .agent import Agent
from .operations import OperationJournal
from .skills import Skill, SkillRegistry, discover_skills, load_skill
from .team import Team
from .supervisor import project_supervisor
from .execution import (
    Checkpoint,
    CheckpointConflict,
    ExecutionLeaseError,
    ExecutionSchemaVersionMismatch,
    ExecutionStateError,
    ExecutionStore,
    Interrupt,
    InterruptState,
    Run,
    RunSuspended,
    RunState,
    Task,
    TaskState,
)
from .durable import ApprovalPolicy, DurablePhaseTimeout, writes_require_approval
from .workbench import (
    Approval,
    Artifact,
    ChangeSet,
    TaskReport,
    RunEvent,
    Verification,
    Workbench,
    Workspace,
    WorkspacePolicyError,
    workbench_approval_policy,
)
from .gateway import ActionGateway, ActionResult, ActionSpec
from .dispatcher import DispatchOutcome, TaskDispatcher
from .security import (
    EnvironmentSecretResolver,
    SecretDetected,
    SecretPolicy,
    SecretResolutionError,
)

__all__ = [
    "__version__",
    "Agent", "tool", "SideEffectClass", "Team", "OperationJournal",
    "Skill", "SkillRegistry", "discover_skills", "load_skill",
    "open_session", "replay", "render_trace", "write_jsonl",
    "project_supervisor",
    "ExecutionStore", "Task", "Run", "Checkpoint", "Interrupt",
    "TaskState", "RunState", "InterruptState",
    "ExecutionStateError", "ExecutionLeaseError",
    "ExecutionSchemaVersionMismatch", "CheckpointConflict",
    "ApprovalPolicy",
    "DurablePhaseTimeout",
    "RunSuspended", "writes_require_approval",
    "Workbench", "Workspace", "Approval", "Artifact", "ChangeSet", "Verification",
    "TaskReport", "WorkspacePolicyError",
    "RunEvent",
    "workbench_approval_policy",
    "ActionGateway", "ActionResult", "ActionSpec",
    "DispatchOutcome", "TaskDispatcher",
    "SecretDetected", "SecretPolicy",
    "SecretResolutionError", "EnvironmentSecretResolver",
]
