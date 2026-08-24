"""LIPAS — trustworthy AI execution with durable, auditable agent work.

The provider-neutral interchange surface is ``lipas.adapter``.
"""
from __future__ import annotations

from ._version import __version__
from .tools import SideEffectClass, tool
from .session import open_session, replay
from .trace import render_trace, write_jsonl
from .agent import Agent
from .events import AgentEvent, AgentEventType, EventEmitter, EventSink
from .context import (
    CancellationToken,
    RunCancelled,
    RunContext,
    RunDeadlineExceeded,
    current_run_context,
)
from .conversation import RunHandle, Session
from .conversation_store import (
    SessionConflictError, SessionSnapshot, SessionStore, SQLiteSessionStore,
)
from .models import (
    CapabilityIssue,
    ModelCapabilities,
    ModelCapabilityError,
    ModelCapabilityReport,
    ModelRegistry,
    ModelRequirements,
)
from .observer import Recommendation, RunObserver, RunSnapshot
from .operations import OperationJournal
from .skills import (
    Skill,
    SkillRegistry,
    builtin_skills,
    discover_skills,
    load_builtin_skill,
    load_skill,
)
from .scenarios import (
    BusinessScenario,
    CapabilityMismatch,
    CapabilityRequirement,
    CapabilitySchemaMismatch,
    ScenarioAssessment,
    ScenarioMode,
    ScenarioRegistry,
    builtin_scenarios,
    load_builtin_scenario,
)
from .team import Team
from .coordination import (
    AgentCoordinator,
    CoordinationBusy,
    CoordinationBudgetExceeded,
    CoordinationCapabilityDenied,
    CoordinationError,
    CoordinationEvent,
    CoordinationEventHandle,
    CoordinationEventPage,
    CoordinationFailed,
    CoordinationIdentityConflict,
    CoordinationRecoveryRequired,
    CoordinationResult,
    CoordinationResultError,
    HandoffEnvelope,
    HandoffExecutionError,
    HandoffFailure,
    HandoffOutcome,
    MemberInfo,
    Transfer,
)
from .coordination_policy import CapabilityPolicy, SharedBudgetPolicy
from .extensions import (
    ConformanceCheck,
    ConformanceReport,
    ExtensionManifest,
    run_conformance,
    scaffold_extension,
)
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
from .durable import (
    ApprovalPolicy, CheckpointMigrationError, DurablePhaseTimeout,
    DurableRecoveryRequired, InputPolicy,
    final_result_from_checkpoint, migrate_checkpoint_payload,
    register_checkpoint_migration,
    writes_require_approval,
)
from .runtime import (
    ArtifactRepository, LIPASRuntime, RuntimeAuditReport, RuntimeClaimIssue,
)
from .workspace_storage import (
    WORKSPACE_DATABASE_NAME,
    WORKSPACE_SCHEMA_VERSION,
    RuntimeStorageIssue,
    WorkspaceMigrationPlan,
    WorkspaceMigrationRequired,
    WorkspaceMigrationResult,
    WorkspaceSchemaMismatch,
    WorkspaceStatus,
    WorkspaceStorage,
)
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
from .http_client import EgressPolicy, HttpClient, HttpClientError, HttpOperationUncertain, HttpResponse
from .email import (
    EmailApprovalRequired, EmailConnector, EmailDelivery, EmailMessage, EmailProvider,
)
from .operator import LocalWebOperator, OperatorServer
from .faults import (
    FaultCampaign,
    FaultCampaignResult,
    FaultMatrixResult,
    FaultInjected,
    FaultInjector,
    FaultPlan,
    run_fault_matrix,
)
from .performance import ExecutionBenchmark, benchmark_execution_store
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
    "AgentCoordinator", "CoordinationError", "CoordinationBusy",
    "CoordinationBudgetExceeded", "CoordinationCapabilityDenied",
    "CoordinationEvent", "CoordinationEventHandle", "CoordinationEventPage",
    "CoordinationFailed", "CoordinationIdentityConflict",
    "CoordinationRecoveryRequired", "CoordinationResultError",
    "CoordinationResult", "HandoffEnvelope", "HandoffExecutionError",
    "HandoffFailure", "HandoffOutcome", "MemberInfo", "Transfer",
    "SharedBudgetPolicy", "CapabilityPolicy",
    "ExtensionManifest", "ConformanceCheck", "ConformanceReport",
    "run_conformance", "scaffold_extension",
    "AgentEvent", "AgentEventType", "EventEmitter", "EventSink",
    "Session", "RunHandle", "SessionStore", "SessionSnapshot",
    "SessionConflictError", "SQLiteSessionStore",
    "RunContext", "CancellationToken", "RunCancelled",
    "RunDeadlineExceeded", "current_run_context",
    "ModelCapabilities", "ModelRequirements", "ModelRegistry",
    "ModelCapabilityReport", "ModelCapabilityError", "CapabilityIssue",
    "RunSnapshot", "RunObserver", "Recommendation",
    "Skill", "SkillRegistry", "discover_skills", "load_skill",
    "builtin_skills", "load_builtin_skill",
    "BusinessScenario", "CapabilityRequirement", "CapabilityMismatch",
    "CapabilitySchemaMismatch",
    "ScenarioAssessment", "ScenarioMode", "ScenarioRegistry",
    "builtin_scenarios", "load_builtin_scenario",
    "open_session", "replay", "render_trace", "write_jsonl",
    "project_supervisor",
    "ExecutionStore", "Task", "Run", "Checkpoint", "Interrupt",
    "TaskState", "RunState", "InterruptState",
    "ExecutionStateError", "ExecutionLeaseError",
    "ExecutionSchemaVersionMismatch", "CheckpointConflict",
    "ApprovalPolicy",
    "InputPolicy",
    "DurablePhaseTimeout",
    "DurableRecoveryRequired", "CheckpointMigrationError",
    "final_result_from_checkpoint", "migrate_checkpoint_payload",
    "register_checkpoint_migration",
    "RunSuspended", "writes_require_approval",
    "Workbench", "Workspace", "Approval", "Artifact", "ChangeSet", "Verification",
    "TaskReport", "WorkspacePolicyError",
    "RunEvent",
    "LIPASRuntime", "RuntimeAuditReport", "RuntimeClaimIssue",
    "ArtifactRepository",
    "WorkspaceStorage", "WorkspaceStatus", "WorkspaceMigrationPlan",
    "WorkspaceMigrationResult", "RuntimeStorageIssue",
    "WorkspaceMigrationRequired", "WorkspaceSchemaMismatch",
    "WORKSPACE_SCHEMA_VERSION", "WORKSPACE_DATABASE_NAME",
    "workbench_approval_policy",
    "ActionGateway", "ActionResult", "ActionSpec",
    "HttpClient", "HttpResponse", "HttpClientError", "HttpOperationUncertain",
    "EgressPolicy", "EmailApprovalRequired", "EmailConnector", "EmailMessage",
    "EmailDelivery", "EmailProvider",
    "LocalWebOperator", "OperatorServer",
    "FaultCampaign", "FaultCampaignResult", "FaultInjected", "FaultInjector",
    "FaultMatrixResult", "FaultPlan", "run_fault_matrix",
    "ExecutionBenchmark", "benchmark_execution_store",
    "DispatchOutcome", "TaskDispatcher",
    "SecretDetected", "SecretPolicy",
    "SecretResolutionError", "EnvironmentSecretResolver",
]
