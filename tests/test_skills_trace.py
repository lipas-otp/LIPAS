from __future__ import annotations

from io import StringIO
import asyncio
from pathlib import Path

import pytest

from lipas.calculus import Claim
from lipas.agent import Agent
from tests.fake_adapter import FakeAdapter
from lipas.tools import SideEffectClass, ToolRegistry, tool
from lipas.skills import SkillError, SkillRegistry, discover_skills, load_skill
from lipas.trace import render_trace, write_jsonl


def test_load_and_render_standard_skill(tmp_path):
    skill_file = tmp_path / "research" / "SKILL.md"
    skill_file.parent.mkdir()
    skill_file.write_text(
        "---\nname: research\ndescription: Find sources\nmetadata: portable\n---\n"
        "# Research\nPrefer primary sources.\n",
        encoding="utf-8",
    )

    skill = load_skill(skill_file)
    assert skill.name == "research"
    assert skill.metadata["metadata"] == "portable"
    assert "<skill name=\"research\">" in SkillRegistry([skill]).system_prompt("Base")
    assert discover_skills(tmp_path) == (skill,)


def test_skill_keeps_claude_style_extended_metadata(tmp_path):
    path = tmp_path / "SKILL.md"
    path.write_text(
        "---\nname: deploy\ndescription: >\n  Deploy safely.\n"
        "allowed-tools: Bash(git status)\nmetadata:\n  short-description: Safe deploy\n"
        "---\nConfirm the target first.\n",
        encoding="utf-8",
    )
    skill = load_skill(path)
    assert skill.description == "Deploy safely."
    assert skill.metadata["allowed-tools"] == "Bash(git status)"
    assert "short-description: Safe deploy" in skill.front_matter


def test_skill_requires_portable_metadata(tmp_path):
    path = tmp_path / "SKILL.md"
    path.write_text("# instructions", encoding="utf-8")
    with pytest.raises(SkillError, match="front matter"):
        load_skill(path)


def test_ready_made_example_skills_are_portable_and_distinct():
    root = Path(__file__).resolve().parent.parent / "examples" / "skills"
    skills = discover_skills(root)
    assert [skill.name for skill in skills] == [
        "daily-brief", "research-brief", "safe-external-actions",
        "support-triage",
    ]
    assert all(skill.description and skill.instructions for skill in skills)


def test_agent_accepts_a_skill_directory_directly(tmp_path):
    skill_file = tmp_path / "triage" / "SKILL.md"
    skill_file.parent.mkdir()
    skill_file.write_text(
        "---\nname: triage\ndescription: Route work safely.\n---\n"
        "Look up facts before making a recommendation.\n",
        encoding="utf-8",
    )
    agent = Agent(adapter=FakeAdapter.echoing(), model="fake", skills=skill_file.parent)
    try:
        assert '<skill name="triage">' in agent.behaviour.request_template.system
    finally:
        agent.close()


def test_trace_is_markdown_and_jsonl_safe():
    claim = Claim(tag="effect_result", fields={"answer": "中文", "values": {1, 2}})
    markdown = render_trace([claim])
    assert "`effect_result`" in markdown
    assert "中文" in markdown

    stream = StringIO()
    write_jsonl([claim], stream)
    assert '"tag": "effect_result"' in stream.getvalue()


def test_agent_provisions_audited_default_rowset():
    agent = Agent(adapter=FakeAdapter.echoing(), model="fake")
    assert {type(row).__name__ for row in agent.rowset.rows} == {
        "HistoryRow", "CapabilityRow", "EffectRow",
    }
    result = asyncio.run(agent("hello"))
    assert result.text == "echo: hello"
    assert len(agent.rowset.store) >= 3


def test_agent_ask_is_the_sync_first_touch_api():
    with Agent(adapter=FakeAdapter.echoing(), model="fake") as agent:
        result = agent.ask("hello")
    assert result.text == "echo: hello"


def test_agent_ask_refuses_to_nest_an_event_loop():
    agent = Agent(adapter=FakeAdapter.echoing(), model="fake")

    async def inside_loop():
        with pytest.raises(RuntimeError, match="await agent.run"):
            agent.ask("hello")

    try:
        asyncio.run(inside_loop())
    finally:
        agent.close()


def test_agent_ollama_constructor_is_a_short_local_entrypoint():
    agent = Agent.ollama("demo-model", instructions="be concise")
    try:
        assert agent.model == "demo-model"
        assert agent.instructions == "be concise"
    finally:
        agent.close()


def test_agent_ollama_has_a_playable_documented_default():
    agent = Agent.ollama()
    try:
        assert agent.model == "gemma4:12b"
    finally:
        agent.close()


def test_durable_agent_keeps_its_declared_budget(tmp_path):
    adapter = FakeAdapter.echoing()
    agent = Agent(
        adapter=adapter,
        model="fake",
        session_path=tmp_path / "runs" / "agent.db",
        max_tokens=100,
        budgets={"tokens_out": 50},
    )
    try:
        result = agent.ask("write a long answer")
        assert result.is_error
        assert result.error and result.error["type"] == "preflight_rejection"
        assert adapter.calls_made == 0
    finally:
        agent.close()


def test_registry_supports_safe_decorator_registration():
    registry = ToolRegistry()

    @registry.register
    @tool(side_effect=SideEffectClass.PURE)
    def identity(value: str) -> str:
        """Return the input unchanged."""
        return value

    assert registry.get("identity") is identity
