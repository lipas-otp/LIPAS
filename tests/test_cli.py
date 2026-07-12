"""Contract tests for the thin operational CLI."""
from __future__ import annotations

import asyncio

from lipas.agent import Agent
from lipas.adapter.errors import ErrorKind
from lipas.cli import main
from tests.fake_adapter import FakeAdapter


def test_init_generates_editable_python_scaffold(tmp_path, capsys):
    target = tmp_path / "demo"
    assert main(["init", str(target), "--model", "demo-model"]) == 0
    source = (target / "agent.py").read_text(encoding="utf-8")
    assert "def build_agent() -> Agent:" in source
    assert "Agent.ollama('demo-model'" in source
    assert "OllamaAdapter" not in source
    compile(source, str(target / "agent.py"), "exec")
    assert "lipas chat --factory agent:build_agent" in (target / "README.md").read_text()
    assert "created" in capsys.readouterr().out


def test_trace_and_effects_read_a_normal_agent_session(tmp_path, capsys):
    session = tmp_path / "run.db"
    agent = Agent(adapter=FakeAdapter.echoing(), model="fake", session_path=str(session))
    try:
        asyncio.run(agent("hello"))
    finally:
        agent.close()

    assert main(["trace", str(session)]) == 0
    assert "call_intent" in capsys.readouterr().out
    assert main(["effects", str(session)]) == 0
    output = capsys.readouterr().out
    assert "effect_id\tkind\tstatus" in output
    assert "llm_call" in output


def test_chat_once_uses_same_agent_runtime(monkeypatch, capsys):
    agent = Agent(adapter=FakeAdapter.echoing(), model="fake")
    monkeypatch.setattr("lipas.cli._factory", lambda _: agent)
    assert main(["chat", "--factory", "ignored:factory", "--once", "hello"]) == 0
    assert "agent> echo: hello" in capsys.readouterr().out


def test_chat_creates_parent_for_new_session(monkeypatch, tmp_path):
    captured = {}

    class StubAgent:
        @classmethod
        def ollama(cls, *_args, **kwargs):
            captured.update(kwargs)
            return cls()
        def close(self): pass

    monkeypatch.setattr("lipas.cli.Agent", StubAgent)
    monkeypatch.setattr("lipas.cli._chat", lambda *_args, **_kwargs: __import__("asyncio").sleep(0))
    session = tmp_path / "new" / "chat.db"
    assert main(["chat", "--session", str(session), "--once", "hello"]) == 0
    assert session.parent.is_dir()
    assert captured["session"] == str(session)


def test_local_ollama_error_is_explained_without_claiming_internet():
    agent = Agent.ollama("demo-model", timeout_s=12)
    try:
        from lipas.cli import _friendly_error
        message = _friendly_error(agent, {
            "type": "network_error", "exception_type": "ReadTimeout",
        })
        assert "localhost timeout" in message
        assert "12s" in message
    finally:
        agent.close()


def test_chat_uses_prompt_local_retry_policy(monkeypatch, tmp_path):
    captured = {}

    class StubAgent:
        @classmethod
        def ollama(cls, *_args, **kwargs):
            captured.update(kwargs)
            return cls()
        def close(self): pass

    monkeypatch.setattr("lipas.cli.Agent", StubAgent)
    monkeypatch.setattr("lipas.cli._chat", lambda *_args, **_kwargs: __import__("asyncio").sleep(0))
    assert main(["chat", "--session", str(tmp_path / "chat.db"), "--once", "hello"]) == 0
    policy = captured["harness_kwargs"]["retry_policy"]
    assert policy[ErrorKind.TIMEOUT].max_attempts == 1
