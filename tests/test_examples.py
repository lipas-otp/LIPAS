"""Keep the numbered beginner lessons importable and runnable offline."""
from __future__ import annotations

from importlib import import_module


def test_single_agent_lessons_build_durable_agents(tmp_path):
    lessons = [
        ("examples.01_first_agent", None),
        ("examples.02_research_brief", "research-brief"),
        ("examples.03_support_triage", "support-triage"),
        ("examples.04_daily_brief", "daily-brief"),
        ("examples.05_budget_limit", None),
    ]
    for index, (module_name, skill_name) in enumerate(lessons, start=1):
        module = import_module(module_name)
        path = tmp_path / f"lesson-{index}" / "agent.db"
        agent = module.build_agent(session=path)
        try:
            assert path.is_file()
            if skill_name:
                assert f'<skill name="{skill_name}">' in agent.behaviour.request_template.system
        finally:
            agent.close()


def test_offline_lessons_run_from_a_fresh_directory(tmp_path, monkeypatch, capsys):
    """Lessons 05–10 must work without Ollama, a network, or pre-made runs/."""
    monkeypatch.chdir(tmp_path)
    budget = import_module("examples.05_budget_limit").build_agent(
        session=tmp_path / "runs" / "budget.db",
    )
    try:
        result = budget.ask("write a detailed essay")
        assert result.is_error
        assert result.error and result.error["type"] == "preflight_rejection"
    finally:
        budget.close()

    for module_name in (
        "examples.06_strict_replay",
        "examples.07_supervision",
        "examples.08_team_handoff",
        "examples.09_external_operation",
        "examples.10_research_review_team",
    ):
        module = import_module(module_name)
        module.main()

    output = capsys.readouterr().out
    assert "live executions after replay: 1" in output
    assert "supervisor terminated: True" in output
    assert "mailbox status: acknowledged" in output
    assert "blind retry refused:" in output
    assert "decision:" in output
