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


def test_skill_catalog_cli_lists_and_shows_instruction_only_business_knowledge(
    capsys,
):
    assert main(["skill", "list", "--json"]) == 0
    catalog = json.loads(capsys.readouterr().out)
    assert [value["name"] for value in catalog] == [
        "business-notice",
        "business-report",
        "calendar-planning",
        "celebration-message",
        "cloud-drive-operations",
        "code-review",
        "coding-task",
        "document-processing",
        "email-drafting",
        "email-operations",
        "meeting-notes",
        "personal-letter",
        "proposal-writing",
        "release-readiness",
        "speech-writing",
        "ticket-triage",
        "workspace-files",
    ]
    assert all(value["authority"] for value in catalog)

    assert main(["skill", "show", "email-drafting", "--json"]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["name"] == "email-drafting"
    assert "separate email Tool" in shown["instructions"]


def test_scenario_cli_lists_shows_and_checks_composable_recipes(capsys):
    assert main(["scenario", "list", "--category", "office", "--json"]) == 0
    catalog = json.loads(capsys.readouterr().out)
    assert {value["name"] for value in catalog} == {
        "business-notice", "calendar-planning", "email-draft",
        "meeting-notes", "office-report", "proposal-draft",
    }
    assert all(value["mode"] == "draft" for value in catalog)

    assert main(["scenario", "show", "email-delivery", "--json"]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["skills"] == ["email-drafting", "email-operations"]
    assert shown["capabilities"][0]["name"] == "send_email"
    assert shown["capabilities"][0]["reconciliation_required"] is True

    assert main(["scenario", "check", "email-draft", "--json"]) == 0
    checked = json.loads(capsys.readouterr().out)
    assert checked["compatible"] is True


def test_chat_scenario_selects_a_minimal_skill_bundle(monkeypatch):
    captured = {}

    class StubAgent:
        @classmethod
        def ollama(cls, *_args, **kwargs):
            captured.update(kwargs)
            return cls()

        def close(self):
            pass

    monkeypatch.setattr("lipas.cli.Agent", StubAgent)
    monkeypatch.setattr(
        "lipas.cli._chat",
        lambda *_args, **_kwargs: __import__("asyncio").sleep(0),
    )
    assert main([
        "chat", "--scenario", "office-report", "--once", "draft status",
    ]) == 0
    assert captured["skills"].names == ("business-report",)


def test_custom_chat_factory_receives_and_satisfies_a_connector_scenario(
    monkeypatch,
):
    captured = {}

    @tool(side_effect="external_write")
    def send_email(
        account: str,
        recipients: list[str],
        subject: str,
        body: str,
        idempotency_key: str,
    ) -> str:
        """Test-only provider delivery capability."""
        del account, recipients, subject, body, idempotency_key
        return "message-1"

    def factory(*, skills, scenarios):
        captured["skills"] = skills
        captured["scenarios"] = scenarios
        return Agent(
            adapter=FakeAdapter.echoing(), model="fake", tools=[send_email],
            skills=skills,
        )

    monkeypatch.setattr("lipas.cli._factory_callable", lambda _spec: factory)
    monkeypatch.setattr(
        "lipas.cli._chat",
        lambda *_args, **_kwargs: __import__("asyncio").sleep(0),
    )
    assert main([
        "chat", "--factory", "connectors:agent",
        "--scenario", "email-delivery", "--once", "send approved mail",
    ]) == 0
    assert captured["skills"].names == ("email-drafting", "email-operations")
    assert captured["scenarios"].names == ("email-delivery",)


def test_toolless_chat_rejects_a_scenario_that_requires_capabilities():
    with pytest.raises(SystemExit) as stopped:
        main([
            "chat", "--scenario", "coding-change", "--once", "change code",
        ])
    assert stopped.value.code == 2


def test_chat_composes_only_explicitly_selected_skills(monkeypatch, tmp_path):
    captured = {}

    class StubAgent:
        @classmethod
        def ollama(cls, *_args, **kwargs):
            captured.update(kwargs)
            return cls()

        def close(self):
            pass

    monkeypatch.setattr("lipas.cli.Agent", StubAgent)
    monkeypatch.setattr(
        "lipas.cli._chat",
        lambda *_args, **_kwargs: __import__("asyncio").sleep(0),
    )
    local = tmp_path / "local" / "SKILL.md"
    local.parent.mkdir()
    local.write_text(
        "---\nname: local-tone\ndescription: Use local tone.\n---\nBe direct.\n",
        encoding="utf-8",
    )
    assert main([
        "chat",
        "--skill", "email-drafting",
        "--skill-path", str(local.parent),
        "--once", "draft a note",
    ]) == 0
    assert captured["skills"].names == ("email-drafting", "local-tone")


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


def test_task_factory_receives_explicitly_composed_skills(monkeypatch, tmp_path):
    from argparse import Namespace

    from lipas.cli import _workbench_agent
    from lipas.workbench import Workbench

    captured = {}
    workspace = tmp_path / "project"
    workspace.mkdir()

    def factory(**kwargs):
        captured.update(kwargs)
        return Agent(
            adapter=FakeAdapter.echoing(),
            model="fake",
            tools=kwargs["tools"],
            session_path=kwargs["session_path"],
            skills=kwargs["skills"],
        )

    monkeypatch.setattr("lipas.cli._factory_callable", lambda _spec: factory)
    args = Namespace(
        factory="project:factory",
        base_url=None,
        model="fake",
        host=None,
        timeout=10.0,
        model_streaming=False,
        max_tokens_field="max_tokens",
        api_key_env="OPENAI_API_KEY",
        no_api_key=False,
        skill=["coding-task"],
        skill_path=[],
    )
    with Workbench(tmp_path / "state", sandbox="local") as workbench:
        task, run = workbench.create_task("repair code", workspace)
        agent = _workbench_agent(
            args, workbench, task_id=task.id, run_id=run.id,
        )
        try:
            assert captured["skills"].names == ("coding-task",)
            assert '<skill name="coding-task">' in (
                agent.behaviour.request_template.system
            )
        finally:
            agent.close()


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
