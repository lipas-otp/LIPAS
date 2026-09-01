"""Keep the numbered beginner lessons importable and runnable offline."""
from __future__ import annotations

from importlib import import_module
import asyncio
import inspect
from pathlib import Path
import re
from urllib.parse import unquote


def _markdown_anchors(text: str) -> set[str]:
    """Return the GitHub-style anchors used by the documentation headings."""
    anchors: set[str] = set()
    occurrences: dict[str, int] = {}
    for heading in re.findall(r"^#{1,6} +(.+?) *#* *$", text, re.MULTILINE):
        heading = re.sub(r"`([^`]*)`", r"\1", heading)
        slug = re.sub(r"[^\w -]", "", heading.lower()).strip()
        slug = re.sub(r" +", "-", slug)
        duplicate = occurrences.get(slug, 0)
        occurrences[slug] = duplicate + 1
        anchors.add(slug if duplicate == 0 else f"{slug}-{duplicate}")
    return anchors


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
    """Lessons 05–12 must work without Ollama, a network, or pre-made runs/."""
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
        "examples.11_durable_execution",
        "examples.12_local_task_product",
        "examples.15_external_connectors",
        "examples.16_workspace_capabilities",
        ):
            module = import_module(module_name)
            result = module.main()
            if inspect.iscoroutine(result):
                asyncio.run(result)

    output = capsys.readouterr().out
    assert "live executions after replay: 1" in output
    assert "supervisor terminated: True" in output
    assert "mailbox status: acknowledged" in output
    assert "blind retry refused:" in output
    assert "decision:" in output
    assert "run state before approval: waiting" in output
    assert "saved notes: ['approved note']" in output
    assert "final run state: completed" in output
    assert "staged note before approval: after" in output
    assert "original before apply: before" in output
    assert "verified: True" in output
    assert "change set state: ready" in output
    assert "applied files: ['note.txt']" in output
    assert "original after apply: after" in output
    assert "mcp:" in output
    assert "csv rows: 2" in output
    assert "calculation: 21" in output
    assert "knowledge hits: 1" in output


def test_documentation_local_links_resolve():
    files = [
        Path("README.md"),
        Path("README.zh-CN.md"),
        Path("CHANGELOG.md"),
        *Path("docs").glob("*.md"),
        *Path("examples").glob("README*.md"),
    ]
    missing: list[str] = []
    for source in files:
        text = source.read_text(encoding="utf-8")
        for target in re.findall(r"\[[^]]+\]\(([^)]+)\)", text):
            if "://" in target or target.startswith("mailto:"):
                continue
            path_text, _, anchor = target.partition("#")
            destination = (source.parent / unquote(path_text or source.name)).resolve()
            if not destination.exists():
                missing.append(f"{source}:{target}")
                continue
            if anchor and destination.suffix == ".md":
                destination_text = destination.read_text(encoding="utf-8")
                if unquote(anchor).lower() not in _markdown_anchors(destination_text):
                    missing.append(f"{source}:{target}")
    assert missing == []


def test_lesson_catalogues_and_tutorial_cover_every_numbered_example():
    examples = sorted(Path("examples").glob("[0-9][0-9]_*.py"))
    english = Path("examples/README.md").read_text(encoding="utf-8")
    chinese = Path("examples/README.zh-CN.md").read_text(encoding="utf-8")
    tutorial = Path("docs/tutorial.md").read_text(encoding="utf-8")
    tutorial_zh = Path("docs/tutorial.zh-CN.md").read_text(encoding="utf-8")

    for example in examples:
        module = f"examples.{example.stem}"
        assert module in english
        assert module in chinese
    assert "examples/11_durable_execution.py" in tutorial
    assert "examples/11_durable_execution.py" in tutorial_zh
    assert "examples/12_local_task_product.py" in tutorial
    assert "examples/12_local_task_product.py" in tutorial_zh
    assert not Path("docs/getting-started.md").exists()
    assert not Path("docs/getting-started.zh-CN.md").exists()


def test_architecture_guides_keep_the_authority_map_visible():
    english = Path("docs/architecture.md").read_text(encoding="utf-8")
    chinese = Path("docs/architecture.zh-CN.md").read_text(encoding="utf-8")
    for text in (english, chinese):
        assert "Agent" in text
        assert "Workbench" in text
        assert "ExecutionStore" in text
        assert "OperationJournal" in text
        assert "Claim" in text
