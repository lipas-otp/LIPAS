"""Composable business scenarios above the LIPAS execution core.

A scenario is deliberately declarative.  It selects instruction-only Skills,
describes a useful lifecycle, and states the Tool contract needed for real
actions.  It never creates a Tool, grants authority, or introduces another
execution state machine.
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import Enum
from functools import lru_cache
from pathlib import Path

from .skills import SkillRegistry
from .tools import SideEffectClass, Tool, ToolRegistry

__all__ = [
    "BusinessScenario",
    "CapabilityMismatch",
    "CapabilitySchemaMismatch",
    "CapabilityRequirement",
    "ScenarioAssessment",
    "ScenarioMode",
    "ScenarioRegistry",
    "builtin_scenarios",
    "load_builtin_scenario",
]


_VALID_APPROVALS = frozenset({"none", "before-call", "delivery"})


class ScenarioMode(str, Enum):
    """Where a scenario produces its useful result."""

    DRAFT = "draft"
    WORKSPACE = "workspace"
    CONNECTOR = "connector"


@dataclass(frozen=True, slots=True)
class CapabilityRequirement:
    """One named Tool contract required by a business scenario.

    Names are intentionally conventional rather than magical.  Applications
    implement normal LIPAS Tools with these names, then use ``assess`` before
    starting work.  Runtime approval and Effect semantics remain authoritative.
    """

    name: str
    side_effect: SideEffectClass
    purpose: str
    approval: str = "none"
    idempotency_required: bool = False
    reconciliation_required: bool = False
    required_parameters: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("CapabilityRequirement.name must be a non-empty string")
        if not isinstance(self.side_effect, SideEffectClass):
            raise TypeError("CapabilityRequirement.side_effect must be SideEffectClass")
        if not isinstance(self.purpose, str) or not self.purpose.strip():
            raise ValueError("CapabilityRequirement.purpose must be non-empty")
        if self.approval not in _VALID_APPROVALS:
            raise ValueError(
                "CapabilityRequirement.approval must be none, before-call, or delivery",
            )
        if not isinstance(self.idempotency_required, bool):
            raise TypeError("idempotency_required must be bool")
        if not isinstance(self.reconciliation_required, bool):
            raise TypeError("reconciliation_required must be bool")
        parameters = self.required_parameters
        if isinstance(parameters, (str, bytes)):
            raise TypeError("required_parameters must be an iterable of names")
        try:
            parameters = tuple(parameters)
        except TypeError as exc:
            raise TypeError(
                "required_parameters must be an iterable of names",
            ) from exc
        if (
            any(not isinstance(value, str) or not value.strip() for value in parameters)
            or len(set(parameters)) != len(parameters)
        ):
            raise ValueError("required_parameters must contain unique non-empty names")
        if self.side_effect in {
            SideEffectClass.IDEMPOTENT_WRITE,
            SideEffectClass.EXTERNAL_WRITE,
        } and self.approval == "none":
            raise ValueError("write capability requirements must declare approval")
        if self.reconciliation_required and (
            self.side_effect is not SideEffectClass.EXTERNAL_WRITE
        ):
            raise ValueError("reconciliation applies only to external writes")
        object.__setattr__(self, "name", self.name.strip())
        object.__setattr__(self, "purpose", self.purpose.strip())
        object.__setattr__(self, "required_parameters", parameters)

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "side_effect": self.side_effect.value,
            "purpose": self.purpose,
            "approval": self.approval,
            "idempotency_required": self.idempotency_required,
            "reconciliation_required": self.reconciliation_required,
            "required_parameters": list(self.required_parameters),
        }


@dataclass(frozen=True, slots=True)
class CapabilityMismatch:
    """A supplied Tool has the right name but a dishonest effect class."""

    name: str
    expected: SideEffectClass
    actual: SideEffectClass

    def as_dict(self) -> dict[str, str]:
        return {
            "name": self.name,
            "expected": self.expected.value,
            "actual": self.actual.value,
        }


@dataclass(frozen=True, slots=True)
class CapabilitySchemaMismatch:
    """A named Tool omits parameters required by the Scenario contract."""

    name: str
    missing_parameters: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "missing_parameters": list(self.missing_parameters),
        }


@dataclass(frozen=True, slots=True)
class ScenarioAssessment:
    """Structural readiness report; host requirements still need human policy."""

    scenario: "BusinessScenario"
    supplied_tools: tuple[str, ...]
    missing_tools: tuple[str, ...]
    mismatches: tuple[CapabilityMismatch, ...]
    schema_mismatches: tuple[CapabilitySchemaMismatch, ...]

    @property
    def compatible(self) -> bool:
        return (
            not self.missing_tools
            and not self.mismatches
            and not self.schema_mismatches
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.scenario.name,
            "compatible": self.compatible,
            "supplied_tools": list(self.supplied_tools),
            "missing_tools": list(self.missing_tools),
            "mismatches": [value.as_dict() for value in self.mismatches],
            "schema_mismatches": [
                value.as_dict() for value in self.schema_mismatches
            ],
            "host_requirements": list(self.scenario.host_requirements),
            "note": (
                "Tool compatibility does not prove account scope, approval, "
                "idempotency, data-egress policy, or provider reconciliation."
                if self.scenario.mode is ScenarioMode.CONNECTOR else None
            ),
        }


@dataclass(frozen=True, slots=True)
class BusinessScenario:
    """An immutable Skill + capability + lifecycle recipe."""

    name: str
    title: str
    description: str
    category: str
    mode: ScenarioMode
    skill_names: tuple[str, ...]
    lifecycle: tuple[str, ...]
    capabilities: tuple[CapabilityRequirement, ...] = ()
    host_requirements: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for field_name in (
            "skill_names", "lifecycle", "capabilities", "host_requirements",
        ):
            values = getattr(self, field_name)
            if isinstance(values, (str, bytes)):
                raise TypeError(f"BusinessScenario.{field_name} must be an iterable")
            try:
                object.__setattr__(self, field_name, tuple(values))
            except TypeError as exc:
                raise TypeError(
                    f"BusinessScenario.{field_name} must be an iterable",
                ) from exc
        for field_name in ("name", "title", "description", "category"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"BusinessScenario.{field_name} must be non-empty")
            object.__setattr__(self, field_name, value.strip())
        if any(
            character not in "abcdefghijklmnopqrstuvwxyz0123456789-_"
            for character in self.name
        ):
            raise ValueError("BusinessScenario.name contains unsupported characters")
        if not isinstance(self.mode, ScenarioMode):
            raise TypeError("BusinessScenario.mode must be ScenarioMode")
        if (
            not self.skill_names
            or any(not isinstance(value, str) or not value.strip() for value in self.skill_names)
            or len(set(self.skill_names)) != len(self.skill_names)
        ):
            raise ValueError("BusinessScenario.skill_names must be non-empty and unique")
        if not self.lifecycle or any(
            not isinstance(value, str) or not value.strip()
            for value in self.lifecycle
        ):
            raise ValueError("BusinessScenario.lifecycle must contain non-empty steps")
        capability_names = [value.name for value in self.capabilities]
        if len(set(capability_names)) != len(capability_names):
            raise ValueError("BusinessScenario capability names must be unique")
        if any(not isinstance(value, CapabilityRequirement) for value in self.capabilities):
            raise TypeError("BusinessScenario.capabilities are CapabilityRequirement values")
        if any(not isinstance(value, str) or not value.strip() for value in self.host_requirements):
            raise ValueError("BusinessScenario.host_requirements must be non-empty strings")

    def skill_registry(
        self,
        *,
        builtin_names: Iterable[str] | str = (),
        paths: Iterable[str | Path] | str | Path = (),
    ) -> SkillRegistry:
        return ScenarioRegistry((self,)).skill_registry(
            builtin_names=builtin_names,
            paths=paths,
        )

    def assess(
        self,
        tools: ToolRegistry | Iterable[Tool] = (),
    ) -> ScenarioAssessment:
        registry = tools if isinstance(tools, ToolRegistry) else ToolRegistry(tools)
        supplied = tuple(sorted(value.name for value in registry))
        available = {value.name: value for value in registry}
        missing: list[str] = []
        mismatches: list[CapabilityMismatch] = []
        schema_mismatches: list[CapabilitySchemaMismatch] = []
        for requirement in self.capabilities:
            actual = available.get(requirement.name)
            if actual is None:
                missing.append(requirement.name)
            elif actual.side_effect is not requirement.side_effect:
                mismatches.append(CapabilityMismatch(
                    requirement.name,
                    requirement.side_effect,
                    actual.side_effect,
                ))
            else:
                schema_required = actual.parameters_schema.get("required", ())
                declared = (
                    set(schema_required)
                    if isinstance(schema_required, (list, tuple))
                    and all(isinstance(value, str) for value in schema_required)
                    else set()
                )
                omitted = tuple(
                    name for name in requirement.required_parameters
                    if name not in declared
                )
                if omitted:
                    schema_mismatches.append(CapabilitySchemaMismatch(
                        requirement.name, omitted,
                    ))
        return ScenarioAssessment(
            scenario=self,
            supplied_tools=supplied,
            missing_tools=tuple(missing),
            mismatches=tuple(mismatches),
            schema_mismatches=tuple(schema_mismatches),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "title": self.title,
            "description": self.description,
            "category": self.category,
            "mode": self.mode.value,
            "skills": list(self.skill_names),
            "lifecycle": list(self.lifecycle),
            "capabilities": [value.as_dict() for value in self.capabilities],
            "host_requirements": list(self.host_requirements),
        }


def _requirement(
    name: str,
    side_effect: SideEffectClass,
    purpose: str,
    *,
    approval: str = "none",
    idempotency: bool = False,
    reconciliation: bool = False,
    parameters: tuple[str, ...] = (),
) -> CapabilityRequirement:
    return CapabilityRequirement(
        name=name,
        side_effect=side_effect,
        purpose=purpose,
        approval=approval,
        idempotency_required=idempotency,
        reconciliation_required=reconciliation,
        required_parameters=parameters,
    )


_LIST_FILES = _requirement(
    "list_workspace_files", SideEffectClass.READ_ONLY,
    "discover bounded workspace files",
)
_READ_FILE = _requirement(
    "read_workspace_file", SideEffectClass.READ_ONLY,
    "read one bounded workspace text file", parameters=("relative_path",),
)
_READ_PDF = _requirement(
    "read_pdf", SideEffectClass.READ_ONLY,
    "extract bounded text from one unencrypted workspace PDF",
    parameters=("relative_path",),
)
_SEARCH_FILES = _requirement(
    "search_workspace", SideEffectClass.READ_ONLY,
    "search literal text in bounded UTF-8 workspace files",
    parameters=("query",),
)
_WRITE_FILE = _requirement(
    "write_workspace_file", SideEffectClass.IDEMPOTENT_WRITE,
    "stage one atomic workspace file replacement", approval="delivery",
    idempotency=True, parameters=("relative_path", "content"),
)
_CONVERT_FILE = _requirement(
    "convert_workspace_file", SideEffectClass.IDEMPOTENT_WRITE,
    "convert one bounded document into a new workspace file",
    approval="delivery", idempotency=True,
    parameters=("source_path", "destination_path"),
)
_CALCULATE = _requirement(
    "calculate", SideEffectClass.READ_ONLY,
    "evaluate a bounded arithmetic expression without code or I/O",
    parameters=("expression",),
)
_ANALYZE_CSV = _requirement(
    "analyze_csv", SideEffectClass.READ_ONLY,
    "profile a bounded CSV file and summarize numeric columns",
    parameters=("relative_path",),
)
_PYTHON_EXEC = _requirement(
    "python_exec", SideEffectClass.EXTERNAL_WRITE,
    "run bounded Python in a temporary sandbox",
    approval="before-call", parameters=("source",),
)
_RUN_COMMAND = _requirement(
    "run_workspace_command", SideEffectClass.EXTERNAL_WRITE,
    "run one isolated, allowlisted verification command", approval="before-call",
    parameters=("argv",),
)
_GIT_STATUS = _requirement(
    "git_status", SideEffectClass.READ_ONLY, "inspect workspace Git state",
)
_GIT_DIFF = _requirement(
    "git_diff", SideEffectClass.READ_ONLY, "review the staged workspace change",
)

_CONNECTOR_HOST_REQUIREMENTS = (
    "explicit account and object scope",
    "secret references resolved outside prompts and durable evidence",
    "data-egress policy before provider calls",
    "durable Effect and provider-reference evidence",
)
_EXTERNAL_WRITE_HOST_REQUIREMENTS = _CONNECTOR_HOST_REQUIREMENTS + (
    "human preview and approval before delivery",
    "stable provider idempotency key",
    "uncertain-result reconciliation without blind retry",
)


def _scenario_catalog() -> tuple[BusinessScenario, ...]:
    """Construct the built-in catalog in one reviewable place."""
    draft = ScenarioMode.DRAFT
    workspace = ScenarioMode.WORKSPACE
    connector = ScenarioMode.CONNECTOR
    return (
        BusinessScenario(
            "business-notice", "Business notice",
            "Prepare a concise internal or customer-facing announcement.",
            "office", draft, ("business-notice",),
            ("establish audience and decision", "draft", "fact-check", "human review"),
        ),
        BusinessScenario(
            "calendar-planning", "Calendar planning",
            "Turn goals and constraints into a reviewable schedule draft.",
            "office", draft, ("calendar-planning",),
            ("collect constraints", "detect conflicts", "draft agenda", "human review"),
        ),
        BusinessScenario(
            "calendar-update", "Calendar update",
            "Inspect a calendar and create or update an approved event.",
            "connectors", connector, ("calendar-planning",),
            ("inspect availability", "draft event", "preview", "approve", "write", "reconcile"),
            (
                _requirement(
                    "list_calendar_events", SideEffectClass.READ_ONLY,
                    "inspect scoped calendar availability",
                    parameters=("calendar_id", "start", "end"),
                ),
                _requirement(
                    "upsert_calendar_event", SideEffectClass.EXTERNAL_WRITE,
                    "create or update one provider calendar event",
                    approval="before-call", idempotency=True, reconciliation=True,
                    parameters=("calendar_id", "event", "idempotency_key"),
                ),
            ),
            _EXTERNAL_WRITE_HOST_REQUIREMENTS,
        ),
        BusinessScenario(
            "celebration-message", "Celebration message",
            "Write a specific, warm greeting for a milestone or occasion.",
            "personal", draft, ("celebration-message",),
            ("establish relationship", "choose warmth", "draft", "privacy review"),
        ),
        BusinessScenario(
            "cloud-drive-organization", "Cloud-drive organization",
            "Review and reorganize scoped cloud-drive items with a preview.",
            "connectors", connector, ("cloud-drive-operations",),
            ("list scoped items", "propose moves", "preview", "approve", "move", "reconcile"),
            (
                _requirement(
                    "list_cloud_files", SideEffectClass.READ_ONLY,
                    "list files in an explicitly scoped provider folder",
                    parameters=("root_id",),
                ),
                _requirement(
                    "move_cloud_file", SideEffectClass.EXTERNAL_WRITE,
                    "move or rename one provider file",
                    approval="before-call", idempotency=True, reconciliation=True,
                    parameters=("item_id", "destination_id", "idempotency_key"),
                ),
            ),
            _EXTERNAL_WRITE_HOST_REQUIREMENTS,
        ),
        BusinessScenario(
            "code-review", "Code review",
            "Inspect a bounded change and produce evidence-linked findings.",
            "engineering", workspace, ("workspace-files", "code-review"),
            ("inspect scope", "read diff", "trace risks", "verify findings", "report"),
            (_LIST_FILES, _READ_FILE, _SEARCH_FILES, _GIT_STATUS, _GIT_DIFF),
        ),
        BusinessScenario(
            "coding-change", "Coding change",
            "Diagnose, implement, verify, and stage a bounded repository change.",
            "engineering", workspace, ("workspace-files", "coding-task"),
            ("diagnose", "inspect", "edit stage", "verify", "review diff", "deliver"),
            (
                _LIST_FILES, _READ_FILE, _SEARCH_FILES, _CALCULATE,
                _ANALYZE_CSV, _PYTHON_EXEC, _WRITE_FILE, _RUN_COMMAND,
                _GIT_STATUS, _GIT_DIFF,
            ),
        ),
        BusinessScenario(
            "document-processing", "Document processing",
            "Extract, summarize, normalize, or convert bounded text documents.",
            "files", workspace, ("workspace-files", "document-processing"),
            ("inventory", "inspect format", "transform", "stage output", "verify"),
            (_LIST_FILES, _READ_FILE, _SEARCH_FILES, _READ_PDF, _WRITE_FILE, _CONVERT_FILE),
        ),
        BusinessScenario(
            "email-delivery", "Email delivery",
            "Draft, preview, approve, send, and reconcile one scoped email.",
            "connectors", connector, ("email-drafting", "email-operations"),
            ("draft", "validate recipients", "preview", "approve", "send", "reconcile"),
            (
                _requirement(
                    "send_email", SideEffectClass.EXTERNAL_WRITE,
                    "send one provider message and return its message id",
                    approval="before-call", idempotency=True, reconciliation=True,
                    parameters=(
                        "account", "recipients", "subject", "body",
                        "idempotency_key",
                    ),
                ),
            ),
            _EXTERNAL_WRITE_HOST_REQUIREMENTS + (
                "attachment type, size, and malware policy",
            ),
        ),
        BusinessScenario(
            "email-draft", "Email draft",
            "Prepare an audience-aware email for human review without sending.",
            "office", draft, ("email-drafting",),
            ("establish audience", "draft", "fact-check", "privacy review"),
        ),
        BusinessScenario(
            "file-management", "File management",
            "Inspect and safely organize files in a staged local workspace.",
            "files", workspace, ("workspace-files",),
            ("inventory", "propose organization", "stage changes", "review", "deliver"),
            (_LIST_FILES, _READ_FILE, _WRITE_FILE),
        ),
        BusinessScenario(
            "meeting-notes", "Meeting notes",
            "Convert supplied meeting material into decisions and accountable actions.",
            "office", draft, ("meeting-notes",),
            ("separate facts", "extract decisions", "assign actions", "review uncertainty"),
        ),
        BusinessScenario(
            "office-report", "Office report",
            "Create an evidence-aware status, analysis, or decision report.",
            "office", draft, ("business-report",),
            ("define audience", "organize evidence", "draft", "check claims", "review"),
        ),
        BusinessScenario(
            "personal-letter", "Personal letter",
            "Write a respectful romantic, affectionate, gratitude, or apology letter.",
            "personal", draft, ("personal-letter",),
            ("establish relationship", "capture authentic details", "draft", "boundary review"),
        ),
        BusinessScenario(
            "proposal-draft", "Proposal draft",
            "Develop a decision-oriented project or business proposal.",
            "office", draft, ("proposal-writing",),
            ("define decision", "state value", "plan delivery", "surface risks", "review"),
        ),
        BusinessScenario(
            "release-readiness", "Release readiness",
            "Review a repository release candidate without publishing it.",
            "engineering", workspace,
            ("workspace-files", "coding-task", "code-review", "release-readiness"),
            ("identify target", "inspect changes", "run checks", "assess compatibility", "report go/no-go"),
            (_LIST_FILES, _READ_FILE, _RUN_COMMAND, _GIT_STATUS, _GIT_DIFF),
        ),
        BusinessScenario(
            "speech-draft", "Speech draft",
            "Write a speakable, audience-aware speech with an intentional arc.",
            "personal", draft, ("speech-writing",),
            ("define audience", "choose message", "draft aloud", "timing review"),
        ),
        BusinessScenario(
            "ticket-triage", "Ticket triage",
            "Inspect scoped work items, classify urgency, and draft next actions.",
            "connectors", connector, ("ticket-triage",),
            ("list queue", "inspect evidence", "classify", "draft recommendation", "handoff"),
            (
                _requirement(
                    "list_tickets", SideEffectClass.READ_ONLY,
                    "list work items in an explicitly scoped queue",
                    parameters=("queue",),
                ),
                _requirement(
                    "read_ticket", SideEffectClass.READ_ONLY,
                    "read one scoped work item and its permitted history",
                    parameters=("ticket_id",),
                ),
            ),
            _CONNECTOR_HOST_REQUIREMENTS,
        ),
    )


@lru_cache(maxsize=1)
def builtin_scenarios() -> tuple[BusinessScenario, ...]:
    """Return the packaged scenario catalog in stable name order."""
    return tuple(sorted(_scenario_catalog(), key=lambda value: value.name))


def load_builtin_scenario(name: str) -> BusinessScenario:
    """Select one packaged scenario by exact stable name."""
    if not isinstance(name, str) or not name.strip():
        raise ValueError("scenario name must be a non-empty string")
    selected = name.strip()
    for scenario in builtin_scenarios():
        if scenario.name == selected:
            return scenario
    available = ", ".join(value.name for value in builtin_scenarios()) or "<none>"
    raise ValueError(f"unknown business scenario {selected!r}; available: {available}")


@dataclass(frozen=True, slots=True)
class ScenarioRegistry:
    """A deduplicated selection of declarative business scenarios."""

    scenarios: tuple[BusinessScenario, ...] = ()

    def __init__(self, scenarios: Iterable[BusinessScenario] = ()) -> None:
        selected = tuple(scenarios)
        if any(not isinstance(value, BusinessScenario) for value in selected):
            raise TypeError("ScenarioRegistry accepts only BusinessScenario values")
        names = [value.name for value in selected]
        if len(set(names)) != len(names):
            raise ValueError("ScenarioRegistry scenario names must be unique")
        object.__setattr__(self, "scenarios", selected)

    @classmethod
    def from_names(cls, names: Iterable[str] | str = ()) -> "ScenarioRegistry":
        selected = (names,) if isinstance(names, str) else tuple(names)
        return cls(load_builtin_scenario(name) for name in selected)

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(value.name for value in self.scenarios)

    def get(self, name: str) -> BusinessScenario:
        for scenario in self.scenarios:
            if scenario.name == name:
                return scenario
        raise ValueError(f"scenario {name!r} is not selected")

    def skill_registry(
        self,
        *,
        builtin_names: Iterable[str] | str = (),
        paths: Iterable[str | Path] | str | Path = (),
    ) -> SkillRegistry:
        explicit = (
            (builtin_names,) if isinstance(builtin_names, str)
            else tuple(builtin_names)
        )
        names = tuple(dict.fromkeys(
            name
            for scenario in self.scenarios
            for name in scenario.skill_names
        ))
        combined = tuple(dict.fromkeys((*names, *explicit)))
        return SkillRegistry.from_sources(builtin_names=combined, paths=paths)

    def assess(
        self,
        tools: ToolRegistry | Iterable[Tool] = (),
    ) -> tuple[ScenarioAssessment, ...]:
        registry = tools if isinstance(tools, ToolRegistry) else ToolRegistry(tools)
        return tuple(value.assess(registry) for value in self.scenarios)

    def require_compatible(
        self,
        tools: ToolRegistry | Iterable[Tool] = (),
    ) -> tuple[ScenarioAssessment, ...]:
        assessments = self.assess(tools)
        failures = [value for value in assessments if not value.compatible]
        if failures:
            detail = "; ".join(
                f"{value.scenario.name}: missing={list(value.missing_tools)!r}, "
                f"mismatched={[item.name for item in value.mismatches]!r}, "
                f"schema={[item.name for item in value.schema_mismatches]!r}"
                for value in failures
            )
            raise ValueError(f"business scenario capability check failed: {detail}")
        return assessments
