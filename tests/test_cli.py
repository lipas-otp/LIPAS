"""Contract tests for the thin operational CLI."""
from __future__ import annotations

import asyncio
import json
import sys

import httpx
import pytest

from lipas import __version__
from lipas.agent import Agent
from lipas.adapter.errors import ErrorKind
from lipas.cli import main
from tests.fake_adapter import FakeAdapter
from lipas import tool
from lipas.tools import ToolRegistry


def test_version_flag_uses_package_version(capsys):
    with pytest.raises(SystemExit) as stopped:
        main(["--version"])
    assert stopped.value.code == 0
    assert capsys.readouterr().out.strip() == f"lipas {__version__}"


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


def test_factory_imports_from_cli_working_directory(monkeypatch, tmp_path):
    module_name = "release_local_factory"
    (tmp_path / f"{module_name}.py").write_text(
        "def build_agent():\n    return 'loaded from cwd'\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        sys,
        "path",
        [value for value in sys.path if value not in {"", str(tmp_path)}],
    )
    sys.modules.pop(module_name, None)
    try:
        from lipas.cli import _factory_callable

        factory = _factory_callable(f"{module_name}:build_agent")
        assert factory() == "loaded from cwd"
    finally:
        sys.modules.pop(module_name, None)


def test_chat_passes_a_new_session_to_the_agent(monkeypatch, tmp_path):
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
    assert captured["session"] == str(session)


def test_chat_builds_openai_compatible_agent_without_plaintext_key_flag(
    monkeypatch,
    tmp_path,
):
    captured = {}

    class StubAgent:
        @classmethod
        def openai_compatible(cls, *args, **kwargs):
            captured["args"] = args
            captured.update(kwargs)
            return cls()

        def close(self):
            pass

    monkeypatch.setattr("lipas.cli.Agent", StubAgent)
    monkeypatch.setattr(
        "lipas.cli._chat",
        lambda *_args, **_kwargs: __import__("asyncio").sleep(0),
    )
    session = tmp_path / "compatible.db"
    assert main([
        "chat",
        "--base-url", "https://api.deepseek.com",
        "--api-key-env", "DEEPSEEK_API_KEY",
        "--model", "deepseek-chat",
        "--session", str(session),
        "--once", "hello",
    ]) == 0

    assert captured["args"] == ("deepseek-chat",)
    assert captured["base_url"] == "https://api.deepseek.com"
    assert captured["api_key_env"] == "DEEPSEEK_API_KEY"
    assert captured["require_api_key"] is True
    assert captured["session"] == str(session)

    captured.clear()
    local_session = tmp_path / "local-compatible.db"
    assert main([
        "chat",
        "--base-url", "http://127.0.0.1:8000/v1",
        "--no-api-key",
        "--model", "local-compatible",
        "--session", str(local_session),
        "--once", "hello",
    ]) == 0
    assert captured["api_key_env"] is None
    assert captured["require_api_key"] is False
    assert captured["session"] == str(local_session)


def test_chat_rejects_compatible_options_that_would_be_ignored():
    invalid_argv = [
        [
            "chat",
            "--base-url", "https://provider.test/v1",
            "--host", "http://localhost:11434",
            "--once", "hello",
        ],
        [
            "chat",
            "--base-url", "https://provider.test/v1",
            "--factory", "agent:build_agent",
            "--once", "hello",
        ],
        ["chat", "--model-streaming", "--once", "hello"],
        [
            "chat",
            "--api-key-env", "DEEPSEEK_API_KEY",
            "--once", "hello",
        ],
        ["chat", "--no-api-key", "--once", "hello"],
        [
            "chat",
            "--base-url", "https://provider.test/v1",
            "--api-key-env", "PROVIDER_API_KEY",
            "--no-api-key",
            "--once", "hello",
        ],
        [
            "model", "check",
            "--base-url", "https://provider.test/v1",
            "--model", "provider-model",
            "--prompt", "ignored without live",
        ],
    ]
    for argv in invalid_argv:
        with pytest.raises(SystemExit) as stopped:
            main(argv)
        assert stopped.value.code == 2


def test_model_check_validates_configuration_without_network(
    monkeypatch,
    capsys,
):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "configuration-secret")
    assert main([
        "model", "check",
        "--base-url", "https://api.deepseek.com",
        "--model", "deepseek-chat",
        "--api-key-env", "DEEPSEEK_API_KEY",
        "--json",
    ]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["configured"] is True
    assert payload["network_request_sent"] is False
    assert payload["endpoint"] == "https://api.deepseek.com/chat/completions"
    assert payload["api_key"] == {
        "environment": "DEEPSEEK_API_KEY",
        "present": True,
        "required": True,
        "value_exposed": False,
    }
    assert "tool_calling" in payload["unknown_capabilities"]

    assert main([
        "model", "check",
        "--base-url", "http://127.0.0.1:8000/v1",
        "--model", "local-compatible",
        "--no-api-key",
        "--json",
    ]) == 0
    no_auth = json.loads(capsys.readouterr().out)
    assert no_auth["network_request_sent"] is False
    assert no_auth["api_key"] == {
        "environment": None,
        "present": False,
        "required": False,
        "value_exposed": False,
    }


def test_model_check_live_probe_reports_success_and_redacted_failure(
    monkeypatch,
    capsys,
):
    from lipas.adapter import OpenAICompatibleAdapter

    success_client = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, request=request, json={
            "id": "probe-1",
            "model": "provider-model",
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": "OK"},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 5, "completion_tokens": 1},
        }),
    ))
    success_adapter = OpenAICompatibleAdapter(
        base_url="https://provider.test/v1",
        api_key="probe-secret",
        client=success_client,
    )
    monkeypatch.setattr(
        "lipas.cli._model_check_adapter", lambda _args: success_adapter,
    )
    argv = [
        "model", "check",
        "--base-url", "https://provider.test/v1",
        "--model", "provider-model",
        "--live",
        "--json",
    ]
    assert main(argv) == 0
    success = json.loads(capsys.readouterr().out)
    assert success["network_request_sent"] is True
    assert success["result"]["ok"] is True
    assert success["result"]["text"] == "OK"
    assert success["result"]["usage"]["input"] == 5

    assert main(argv[:-1]) == 0
    human_success = capsys.readouterr().out
    assert "provider model: provider-model" in human_success
    assert '"input": 5' in human_success
    assert 'response text: "OK"' in human_success
    asyncio.run(success_client.aclose())

    failure_key = "failure-secret-must-not-leak"
    failure_client = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(
            401,
            request=request,
            json={"error": {"message": f"invalid {failure_key}"}},
        ),
    ))
    failure_adapter = OpenAICompatibleAdapter(
        base_url="https://provider.test/v1",
        api_key=failure_key,
        client=failure_client,
    )
    monkeypatch.setattr(
        "lipas.cli._model_check_adapter", lambda _args: failure_adapter,
    )
    assert main(argv) == 1
    raw_failure = capsys.readouterr().out
    failure = json.loads(raw_failure)
    assert failure["result"]["ok"] is False
    assert failure["result"]["error_kind"] == "auth"
    assert failure_key not in raw_failure
    assert "<redacted>" in raw_failure
    asyncio.run(failure_client.aclose())


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


def test_custom_adapter_error_does_not_require_the_ollama_extra():
    from lipas.cli import _friendly_error

    agent = Agent(adapter=FakeAdapter.echoing(), model="fake")
    try:
        message = _friendly_error(agent, {
            "type": "network_error", "exception_type": "CustomFailure",
        })
        assert message.startswith("error:")
        assert "localhost timeout" not in message
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


def test_action_cli_calls_the_shared_gateway(monkeypatch, tmp_path, capsys):
    calls = []

    @tool(side_effect="read_only")
    def lookup(value: str) -> str:
        """Return one CLI value."""
        calls.append(value)
        return value.upper()

    monkeypatch.setattr(
        "lipas.cli._tool_factory", lambda _spec: ToolRegistry([lookup]),
    )
    assert main([
        "action", "call",
        "--factory", "ignored:factory",
        "--session", str(tmp_path / "actions.db"),
        "--tool", "lookup",
        "--arguments", '{"value":"cli"}',
        "--request-id", "cli-action-1",
    ]) == 0
    output = capsys.readouterr().out
    assert '"status": "ok"' in output
    assert '"output": "CLI"' in output
    assert calls == ["cli"]
