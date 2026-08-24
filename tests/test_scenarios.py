"""Business Scenarios compose knowledge and declare authority honestly."""
from __future__ import annotations

import pytest

from lipas.scenarios import (
    CapabilityRequirement,
    ScenarioMode,
    ScenarioRegistry,
    builtin_scenarios,
    load_builtin_scenario,
)
from lipas.skills import builtin_skills
from lipas.tools import SideEffectClass, Tool


def _named_tool(
    name: str,
    side_effect: SideEffectClass,
    required_parameters: tuple[str, ...] = (),
) -> Tool:
    def handler() -> str:
        return "ok"

    return Tool(
        name=name,
        description=f"Test implementation for {name}.",
        parameters_schema={
            "type": "object",
            "properties": {value: {} for value in required_parameters},
            "required": list(required_parameters),
        },
        side_effect=side_effect,
        _handler=handler,
    )


def test_builtin_scenarios_cover_the_promised_business_surface():
    scenarios = builtin_scenarios()
    assert [value.name for value in scenarios] == [
        "business-notice",
        "calendar-planning",
        "calendar-update",
        "celebration-message",
        "cloud-drive-organization",
        "code-review",
        "coding-change",
        "document-processing",
        "email-delivery",
        "email-draft",
        "file-management",
        "meeting-notes",
        "office-report",
        "personal-letter",
        "proposal-draft",
        "release-readiness",
        "speech-draft",
        "ticket-triage",
    ]
    assert {value.mode for value in scenarios} == set(ScenarioMode)
    available_skills = {value.name for value in builtin_skills()}
    assert all(
        set(scenario.skill_names) <= available_skills
        for scenario in scenarios
    )


def test_scenario_registry_composes_and_deduplicates_skill_bundles():
    registry = ScenarioRegistry.from_names([
        "coding-change", "release-readiness",
    ])
    skills = registry.skill_registry(builtin_names=["code-review"])
    assert registry.names == ("coding-change", "release-readiness")
    assert skills.names == (
        "workspace-files",
        "coding-task",
        "code-review",
        "release-readiness",
    )


def test_draft_scenario_requires_no_executable_authority():
    scenario = load_builtin_scenario("email-draft")
    assessment = scenario.assess()
    assert scenario.mode is ScenarioMode.DRAFT
    assert assessment.compatible
    assert assessment.missing_tools == ()
    assert scenario.skill_registry().names == ("email-drafting",)


def test_workspace_scenario_checks_tool_presence_and_effect_identity():
    scenario = load_builtin_scenario("coding-change")
    correct = [
        _named_tool(
            value.name, value.side_effect, value.required_parameters,
        )
        for value in scenario.capabilities
    ]
    assessment = scenario.assess(correct)
    assert assessment.compatible
    assert assessment.missing_tools == ()

    dishonest = [
        _named_tool(
            value.name,
            SideEffectClass.PURE
            if value.name == "write_workspace_file" else value.side_effect,
            value.required_parameters,
        )
        for value in scenario.capabilities
    ]
    mismatch = scenario.assess(dishonest)
    assert not mismatch.compatible
    assert mismatch.mismatches[0].name == "write_workspace_file"
    assert mismatch.mismatches[0].expected is SideEffectClass.IDEMPOTENT_WRITE
    assert mismatch.mismatches[0].actual is SideEffectClass.PURE


def test_external_scenario_never_treats_tool_shape_as_complete_host_policy():
    scenario = load_builtin_scenario("email-delivery")
    requirement = scenario.capabilities[0]
    assert scenario.mode is ScenarioMode.CONNECTOR
    assert requirement.name == "send_email"
    assert requirement.side_effect is SideEffectClass.EXTERNAL_WRITE
    assert requirement.approval == "before-call"
    assert requirement.idempotency_required
    assert requirement.reconciliation_required

    assessment = scenario.assess([
        _named_tool(
            "send_email", SideEffectClass.EXTERNAL_WRITE,
            requirement.required_parameters,
        ),
    ])
    assert assessment.compatible
    assert assessment.as_dict()["note"] is not None
    assert any(
        "uncertain-result reconciliation" in value
        for value in scenario.host_requirements
    )

    incomplete = scenario.assess([
        _named_tool("send_email", SideEffectClass.EXTERNAL_WRITE),
    ])
    assert not incomplete.compatible
    assert incomplete.schema_mismatches[0].missing_parameters == (
        "account", "recipients", "subject", "body", "idempotency_key",
    )


def test_missing_scenario_tools_fail_before_execution():
    registry = ScenarioRegistry.from_names("file-management")
    with pytest.raises(ValueError, match="capability check failed"):
        registry.require_compatible(())


def test_write_requirement_cannot_hide_approval_or_reconciliation_class():
    with pytest.raises(ValueError, match="declare approval"):
        CapabilityRequirement(
            "unsafe_write", SideEffectClass.EXTERNAL_WRITE, "write somewhere",
        )
    with pytest.raises(ValueError, match="only to external writes"):
        CapabilityRequirement(
            "local_write", SideEffectClass.IDEMPOTENT_WRITE, "write locally",
            approval="delivery", reconciliation_required=True,
        )
