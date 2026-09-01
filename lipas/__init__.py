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
from .effect import EffectDecision, EffectObservation, EffectProposal
from .context import (
    CancellationToken,
    RunCancelled,
    RunContext,
    RunDeadlineExceeded,
    current_run_context,
)
from .conversation import RunHandle, Session
from .conversation_store import (
    Attachment,
    Conversation,
    ConversationEvent,
    ConversationEventPage,
    Message,
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
    AgentPlan,
    PlanStep,
    Transfer,
    ExternalRunAdapter,
    ExternalRunEnvelope,
    ExternalRunResult,
)
from .coordination_policy import (
    ApprovalDelegation, CapabilityPolicy, SharedBudgetPolicy, WorkspaceIdentity,
    WorkspacePolicyStore,
)
from .workflow import (
    AutonomousWorkflowCompiler, CompiledPlan, CompiledWorkflow, MixedPlan,
    PlanCompiler, StepMode, WorkflowCompiler, WorkflowConstraint, WorkflowGoal,
    WorkflowStep, WorkflowStepResult, WorkflowExecutionResult,
    compile_goal, compile_workflow, execute_compiled_workflow, execute_workflow,
)
from .extensions import (
    ConformanceCheck,
    ConformanceReport,
    ExtensionCertification,
    ExtensionManifest,
    ExtensionRegistry,
    ExtensionRegistryService,
    ExtensionSigner,
    ExtensionTrustPolicy,
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
    AgentRuntime, ArtifactRepository, LIPASRuntime, RuntimeAuditReport, RuntimeClaimIssue,
)
from .workspace_storage import (
    WORKSPACE_DATABASE_NAME,
    WORKSPACE_SCHEMA_VERSION,
    RuntimeStorageIssue,
    WorkspaceMigrationPlan,
    WorkspaceMigrationRequired,
    WorkspaceMigrationResult,
    WorkspaceBackup,
    WorkspaceBundle,
    WorkspaceBackupBundle,
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
from .document_tools import (
    ConvertedDocument,
    DocumentToolError,
    MissingDocumentDependency,
    UnsupportedDocumentFormat,
    convert_document,
    read_pdf_text,
)
from .code_tools import (
    CodeExecutionResult,
    CodeToolError,
    CsvAnalysis,
    analyze_csv,
    calculate_expression,
    execute_python,
)
from .web_tools import (
    FetchedPage,
    WebToolError,
    extract_page_text,
    fetch_url,
    fetch_url_tool,
)
from .archive_tools import (
    ArchiveEntry,
    ArchiveSummary,
    ArchiveToolError,
    extract_archive,
    inspect_archive,
)
from .knowledge import (
    KnowledgeDocument,
    KnowledgeError,
    KnowledgeHit,
    KnowledgeStore,
    knowledge_search_tool,
)
from .gateway import ActionGateway, ActionResult, ActionSpec
from .http_client import (
    ConnectorRegistry, ConnectorSpec, EgressPolicy, HttpClient, HttpClientError,
    HttpOperationUncertain, HttpResponse, RateLimitExceeded, RateLimitPolicy,
)
from .email import (
    EmailApprovalRequired, EmailConnector, EmailDelivery, EmailMessage, EmailProvider,
)
from .operator import LocalWebOperator, OperatorAuthenticator, OperatorServer
from .faults import (
    FaultCampaign,
    FaultCampaignResult,
    FaultMatrixResult,
    FaultInjected,
    FaultInjector,
    FaultPlan,
    run_fault_matrix,
)
from .performance import (
    CostEntry, CostLedger, EvaluationCase, EvaluationReport, ExecutionBenchmark,
    ExecutionMetrics, ExecutionSoakReport, IncidentRecord, SLOReport,
    benchmark_execution_store,
    DesignPartnerCase, DesignPartnerReport, DesignPartnerRun, DesignPartnerSignoff,
    evaluate_execution, measure_execution, project_cost_ledger, project_incidents,
    run_design_partner_validation, run_execution_soak, run_soak,
)
from .provider_workflow import ProviderWorkflowEvidence, run_provider_workflow
from .dispatcher import DispatchOutcome, TaskDispatcher
from .dispatcher import (
    HybridWorker, RemoteCheckpoint, RemoteEffectObservation,
    RemoteExecutionResult, RemoteWorkerEvent, RemoteWorkerLease,
    RemoteWorkerRunner, RemoteWorkerHTTPClient, RemoteWorkerHTTPServer,
    WorkerAttestation, WorkerCapabilities,
)
from .security import (
    EnvironmentSecretResolver,
    FileSecretResolver,
    ManagedSecretResolver,
    SecretDetected,
    SecretPolicy,
    SecretResolutionError,
    TLSConfig,
)
from .deployment import (
    INSTALLATION_MANIFEST_NAME,
    INSTALLATION_MANIFEST_VERSION,
    DeploymentCheck,
    DeploymentReport,
    InstallationManifest,
    install_workspace,
    release_check,
    upgrade_workspace,
    verify_installation,
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
    "HandoffFailure", "HandoffOutcome", "MemberInfo", "AgentPlan", "PlanStep",
    "Transfer", "ExternalRunEnvelope", "ExternalRunResult", "ExternalRunAdapter",
    "SharedBudgetPolicy", "CapabilityPolicy", "WorkspaceIdentity",
    "ApprovalDelegation", "WorkspacePolicyStore",
    "WorkflowGoal", "WorkflowConstraint", "WorkflowStep", "CompiledWorkflow",
    "StepMode", "CompiledPlan", "MixedPlan", "AutonomousWorkflowCompiler", "WorkflowCompiler",
    "PlanCompiler",
    "compile_workflow", "compile_goal", "WorkflowStepResult",
    "WorkflowExecutionResult", "execute_compiled_workflow", "execute_workflow",
    "ExtensionManifest", "ExtensionRegistry", "ExtensionRegistryService",
    "ExtensionSigner", "ExtensionCertification", "ExtensionTrustPolicy",
    "ConformanceCheck", "ConformanceReport",
    "run_conformance", "scaffold_extension",
    "AgentEvent", "AgentEventType", "EventEmitter", "EventSink",
    "EffectProposal", "EffectDecision", "EffectObservation",
    "Session", "RunHandle", "SessionStore", "SessionSnapshot",
    "SessionConflictError", "SQLiteSessionStore",
    "Conversation", "Message", "Attachment", "ConversationEvent", "ConversationEventPage",
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
    "LIPASRuntime", "AgentRuntime", "RuntimeAuditReport", "RuntimeClaimIssue",
    "ArtifactRepository",
    "WorkspaceStorage", "WorkspaceStatus", "WorkspaceMigrationPlan",
    "WorkspaceMigrationResult", "RuntimeStorageIssue",
    "WorkspaceMigrationRequired", "WorkspaceSchemaMismatch", "WorkspaceBackup",
    "WorkspaceBundle",
    "WorkspaceBackupBundle",
    "WORKSPACE_SCHEMA_VERSION", "WORKSPACE_DATABASE_NAME",
    "workbench_approval_policy",
    "ConvertedDocument", "DocumentToolError", "MissingDocumentDependency",
    "UnsupportedDocumentFormat", "convert_document", "read_pdf_text",
    "CodeExecutionResult", "CodeToolError", "CsvAnalysis", "analyze_csv",
    "calculate_expression", "execute_python",
    "FetchedPage", "WebToolError", "extract_page_text", "fetch_url",
    "fetch_url_tool",
    "ArchiveEntry", "ArchiveSummary", "ArchiveToolError", "extract_archive",
    "inspect_archive",
    "KnowledgeDocument", "KnowledgeError", "KnowledgeHit", "KnowledgeStore",
    "knowledge_search_tool",
    "ActionGateway", "ActionResult", "ActionSpec",
    "HttpClient", "HttpResponse", "HttpClientError", "HttpOperationUncertain",
    "EgressPolicy", "RateLimitPolicy", "RateLimitExceeded", "ConnectorSpec",
    "ConnectorRegistry", "EmailApprovalRequired", "EmailConnector", "EmailMessage",
    "EmailDelivery", "EmailProvider",
    "LocalWebOperator", "OperatorAuthenticator", "OperatorServer",
    "FaultCampaign", "FaultCampaignResult", "FaultInjected", "FaultInjector",
    "FaultMatrixResult", "FaultPlan", "run_fault_matrix",
    "ExecutionBenchmark", "ExecutionMetrics", "ExecutionSoakReport", "SLOReport", "CostEntry", "CostLedger",
    "IncidentRecord", "EvaluationCase", "EvaluationReport",
    "benchmark_execution_store", "measure_execution", "project_cost_ledger",
    "project_incidents", "evaluate_execution",
    "DesignPartnerCase", "DesignPartnerRun", "DesignPartnerSignoff", "DesignPartnerReport",
    "run_design_partner_validation", "run_execution_soak", "run_soak",
    "ProviderWorkflowEvidence", "run_provider_workflow",
    "DispatchOutcome", "TaskDispatcher",
    "HybridWorker", "WorkerCapabilities", "RemoteWorkerLease", "RemoteWorkerRunner",
    "RemoteWorkerEvent", "RemoteCheckpoint", "RemoteEffectObservation",
    "RemoteExecutionResult", "WorkerAttestation", "RemoteWorkerHTTPClient",
    "RemoteWorkerHTTPServer",
    "SecretDetected", "SecretPolicy",
    "SecretResolutionError", "EnvironmentSecretResolver",
    "FileSecretResolver", "ManagedSecretResolver", "TLSConfig",
    "INSTALLATION_MANIFEST_NAME", "INSTALLATION_MANIFEST_VERSION",
    "DeploymentCheck", "DeploymentReport", "InstallationManifest",
    "install_workspace", "upgrade_workspace", "verify_installation", "release_check",
]
