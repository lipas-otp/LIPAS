"""Regression contracts for the bounded 0.51 workflow compiler."""
from __future__ import annotations

from pathlib import Path
import asyncio

import pytest

from lipas import (
    AgentPlan,
    AutonomousWorkflowCompiler,
    LIPASRuntime,
    WorkflowConstraint,
    WorkflowGoal,
    WorkflowStep,
    compile_workflow,
    execute_compiled_workflow,
)


def test_goal_only_compilation_is_deterministic_and_mixed():
    first = compile_workflow(
        "prepare release notes",
        constraints={"audience": "internal", "format": "markdown"},
        workspace=".",
        fixed_steps=(
            {"step_id": "inspect", "goal": "Inspect the changes"},
        ),
        adaptive_steps=2,
        max_adaptive_steps=2,
    )
    second = compile_workflow(
        "prepare release notes",
        constraints={"format": "markdown", "audience": "internal"},
        workspace=".",
        fixed_steps=(
            {"step_id": "inspect", "goal": "Inspect the changes"},
        ),
        adaptive_steps=2,
        max_adaptive_steps=2,
    )
    assert first.plan_id == second.plan_id
    assert first.fingerprint == second.fingerprint
    assert [step.mode for step in first.steps] == ["fixed", "adaptive", "adaptive"]
    assert first.adaptive_steps[-1].depends_on == ("adaptive-1",)
    assert isinstance(first.plan, AgentPlan)
    assert first.as_dict()["fixed_steps"] == ["inspect"]


def test_compiler_preserves_hard_constraints_without_granting_authority(tmp_path: Path):
    goal = WorkflowGoal(
        "write a report",
        (WorkflowConstraint("format", "markdown"),),
        str(tmp_path),
        max_adaptive_steps=1,
    )
    step = WorkflowStep("write", "Write the report", "fixed", "writer")
    compiled = AutonomousWorkflowCompiler(default_recipient="planner").compile(
        goal,
        fixed_steps=(step,),
        adaptive_steps=0,
    )
    assert compiled.goal.workspace == str(tmp_path.resolve())
    assert compiled.steps[0].mode == "fixed"
    envelope = compiled.handoff("write", sender="user", payload={"ok": True})
    assert envelope.recipient == "writer"
    assert envelope.metadata["plan_id"] == compiled.plan_id
    assert envelope.metadata["step_metadata"]["workflow_constraints"] == [
        {"name": "format", "value": "markdown", "hard": True},
    ]


def test_compiler_rejects_cycles_and_unbounded_adaptation():
    compiler = AutonomousWorkflowCompiler()
    with pytest.raises(ValueError, match="exceeds max_adaptive_steps"):
        compiler.compile("goal", adaptive_steps=2, max_adaptive_steps=1)
    with pytest.raises(ValueError, match="cycle"):
        compiler.compile(
            "goal",
            adaptive_steps=0,
            fixed_steps=(
                {"step_id": "a", "goal": "a", "depends_on": ("b",)},
                {"step_id": "b", "goal": "b", "depends_on": ("a",)},
            ),
        )


def test_explicit_adaptive_steps_carry_the_plan_bound():
    compiled = compile_workflow(
        "goal",
        max_adaptive_steps=3,
        fixed_steps=({"step_id": "fixed", "goal": "fixed"},),
        adaptive_steps=(
            {"step_id": "adapt", "goal": "adapt", "mode": "adaptive"},
        ),
    )
    assert compiled.adaptive_steps[0].metadata["max_steps"] == 3
    with pytest.raises(ValueError, match="max_steps"):
        compile_workflow(
            "goal",
            max_adaptive_steps=1,
            fixed_steps=({"step_id": "fixed", "goal": "fixed"},),
            adaptive_steps=(
                {"step_id": "adapt", "goal": "adapt", "max_steps": 2},
            ),
        )


def test_goal_constraints_are_carried_to_every_compiled_step_and_dependencies_canonicalize():
    first = compile_workflow(
        "goal",
        constraints={"z": 1, "a": "hard"},
        fixed_steps=(
            {"step_id": "a", "goal": "a"},
            {"step_id": "b", "goal": "b", "depends_on": ("a",)},
        ),
        adaptive_steps=0,
    )
    second = compile_workflow(
        "goal",
        constraints={"a": "hard", "z": 1},
        fixed_steps=(
            {"step_id": "a", "goal": "a"},
            {"step_id": "b", "goal": "b", "depends_on": ("a",)},
        ),
        adaptive_steps=0,
    )
    assert first.plan_id == second.plan_id
    assert [item.name for item in first.steps[0].constraints] == ["a", "z"]
    assert first.plan.steps[0].metadata["workflow_constraints"] == [
        {"name": "a", "value": "hard", "hard": True},
        {"name": "z", "value": 1, "hard": True},
    ]


def test_runtime_compilation_defaults_to_runtime_workspace(tmp_path: Path):
    with LIPASRuntime.open(tmp_path / "state", sandbox="local") as runtime:
        compiled = runtime.compile_workflow(
            "inspect workspace",
            fixed_steps=({"step_id": "inspect", "goal": "inspect"},),
            adaptive_steps=0,
        )
        assert compiled.goal.workspace == str((tmp_path / "state").resolve())


def test_runtime_workflow_renews_short_lease_during_long_step(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    with LIPASRuntime.open(tmp_path / "state", sandbox="local") as runtime:
        workflow = runtime.compile_workflow(
            "slow inspection",
            workspace=project,
            fixed_steps=({"step_id": "slow", "goal": "slow"},),
            adaptive_steps=0,
        )

        async def executor(_step, _context):
            await asyncio.sleep(0.08)
            return {"ok": True}

        result = asyncio.run(
            runtime.execute_workflow(workflow, executor, lease_seconds=0.03),
        )
        assert result.succeeded


def test_runtime_workflow_resumes_from_step_checkpoint_after_expired_lease(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    state = tmp_path / "state"
    with LIPASRuntime.open(state, sandbox="local") as runtime:
        workflow = runtime.compile_workflow(
            "checkpointed workflow",
            workspace=project,
            fixed_steps=(
                {"step_id": "first", "goal": "first"},
                {"step_id": "second", "goal": "second", "depends_on": ("first",)},
            ),
            adaptive_steps=0,
        )
        calls: list[str] = []

        def crash_after_first(step, _context):
            calls.append(step.step_id)
            if step.step_id == "second":
                raise KeyboardInterrupt()
            return {"step": step.step_id}

        with pytest.raises(KeyboardInterrupt):
            asyncio.run(runtime.execute_workflow(
                workflow, crash_after_first, lease_seconds=0.01,
            ))
        run = runtime.execution.list_runs()[0]
        assert run.state.value == "running"
        assert runtime.execution.get_checkpoint(run.id) is not None

    import time

    time.sleep(0.03)
    with LIPASRuntime.open(state, sandbox="local") as runtime:
        result = asyncio.run(runtime.execute_workflow(
            workflow,
            lambda step, _context: {"step": step.step_id},
            lease_seconds=0.05,
        ))
        assert result.succeeded
        assert [event.type for event in runtime.execution.agent_events(run.id)] .count(
            "workflow_step_replayed",
        ) == 1


def test_compiled_workflow_reports_cooperative_cancellation():
    workflow = compile_workflow(
        "cancel me",
        fixed_steps=(
            {"step_id": "one", "goal": "one"},
            {"step_id": "two", "goal": "two", "depends_on": ("one",)},
        ),
        adaptive_steps=0,
    )
    calls: list[str] = []

    async def executor(step, _context):
        calls.append(step.step_id)
        return {"ok": True}

    result = asyncio.run(
        execute_compiled_workflow(
            workflow,
            executor,
            cancel_check=lambda: bool(calls),
        ),
    )
    assert result.cancelled
    assert calls == ["one"]
    assert [item.status for item in result.steps] == ["succeeded", "skipped"]


def test_runtime_workflow_cancellation_settles_durable_run(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    with LIPASRuntime.open(tmp_path / "state", sandbox="local") as runtime:
        workflow = runtime.compile_workflow(
            "cancel durable workflow",
            workspace=project,
            fixed_steps=({"step_id": "one", "goal": "one"},),
            adaptive_steps=0,
        )
        result = asyncio.run(
            runtime.execute_workflow(
                workflow,
                lambda _step, _context: {"ok": True},
                cancel_check=lambda: True,
            ),
        )
        assert result.cancelled
        assert runtime.execution.list_runs()[0].state.value == "cancelled"
