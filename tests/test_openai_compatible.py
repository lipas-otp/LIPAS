"""OpenAI-compatible Chat Completions contract and provider-shape tests."""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from lipas import Agent, ModelCapabilityError, ModelRequirements
from lipas.adapter import (
    Delta,
    OpenAICompatibleAdapter,
    Request,
    Thinking,
    ToolSpec,
    ToolUseDelta,
    Usage,
    complete,
)
from lipas.adapter.errors import ErrorKind, classify


def _request(**overrides) -> Request:
    values = {
        "model": "provider-model",
        "messages": [{"role": "user", "content": "hello"}],
        "max_tokens": 64,
    }
    values.update(overrides)
    return Request(**values)  # type: ignore[arg-type]


def _json_response(**overrides):
    payload = {
        "id": "chatcmpl-1",
        "model": "provider-model",
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": "world"},
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": 5, "completion_tokens": 2},
    }
    payload.update(overrides)
    return payload


def _run(adapter: OpenAICompatibleAdapter, request: Request | None = None):
    return asyncio.run(complete(adapter, request or _request()))


@pytest.mark.parametrize(
    ("base_url", "expected"),
    [
        (
            "https://ark.cn-beijing.volces.com/api/v3",
            "https://ark.cn-beijing.volces.com/api/v3/chat/completions",
        ),
        (
            "https://dashscope.aliyuncs.com/compatible-mode/v1/",
            "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
        ),
        (
            "https://api.hunyuan.cloud.tencent.com/v1",
            "https://api.hunyuan.cloud.tencent.com/v1/chat/completions",
        ),
        (
            "https://api.deepseek.com",
            "https://api.deepseek.com/chat/completions",
        ),
        (
            "https://example.test/v1/chat/completions/",
            "https://example.test/v1/chat/completions",
        ),
    ],
)
def test_provider_base_urls_resolve_to_one_chat_completions_path(
    base_url,
    expected,
):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, request=request, json=_json_response())

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = OpenAICompatibleAdapter(
        base_url=base_url,
        api_key="provider-secret",
        client=client,
    )
    reply = _run(adapter)
    asyncio.run(client.aclose())

    assert reply.stop_reason == "end_turn"
    assert str(seen[0].url) == expected
    assert seen[0].headers["authorization"] == "Bearer provider-secret"


def test_non_streaming_request_translates_system_tools_and_tool_history():
    captured = {}

    def handler(request):
        captured.update(json.loads(request.content))
        return httpx.Response(200, request=request, json=_json_response())

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = OpenAICompatibleAdapter(
        base_url="https://provider.test/v1",
        api_key="secret",
        client=client,
    )
    request = _request(
        system="Stay concise.",
        messages=[
            {"role": "user", "content": "look it up"},
            {"role": "assistant", "content": [{
                "type": "tool_use",
                "id": "call-1",
                "name": "lookup",
                "input": {"id": "42"},
            }]},
            {"role": "user", "content": [{
                "type": "tool_result",
                "tool_use_id": "call-1",
                "content": "Ada",
            }]},
        ],
        tools=[ToolSpec("lookup", "Look up a record", {"type": "object"})],
        temperature=0.2,
        stop_sequences=["END"],
    )
    _run(adapter, request)
    asyncio.run(client.aclose())

    assert captured["stream"] is False
    assert captured["messages"] == [
        {"role": "system", "content": "Stay concise."},
        {"role": "user", "content": "look it up"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": "call-1",
                "type": "function",
                "function": {
                    "name": "lookup",
                    "arguments": '{"id":"42"}',
                },
            }],
        },
        {"role": "tool", "tool_call_id": "call-1", "content": "Ada"},
    ]
    assert captured["tools"][0]["function"]["name"] == "lookup"
    assert captured["temperature"] == 0.2
    assert captured["stop"] == ["END"]


def test_non_streaming_reply_normalizes_tool_calls_and_disjoint_usage():
    response = _json_response(
        choices=[{
            "index": 0,
            "message": {
                "role": "assistant",
                "content": "checking",
                "tool_calls": [{
                    "id": "call-42",
                    "type": "function",
                    "function": {
                        "name": "lookup",
                        "arguments": '{"id":"42"}',
                    },
                }],
            },
            "finish_reason": "tool_calls",
        }],
        usage={
            "prompt_tokens": 10,
            "completion_tokens": 3,
            "prompt_tokens_details": {"cached_tokens": 4},
        },
    )
    client = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, request=request, json=response),
    ))
    adapter = OpenAICompatibleAdapter(
        base_url="https://provider.test/v1",
        api_key="secret",
        client=client,
    )
    reply = _run(adapter)
    asyncio.run(client.aclose())

    assert reply.stop_reason == "tool_use"
    assert reply.content == (
        {"type": "text", "text": "checking"},
        {
            "type": "tool_use",
            "id": "call-42",
            "name": "lookup",
            "input": {"id": "42"},
        },
    ) or list(reply.content) == [
        {"type": "text", "text": "checking"},
        {
            "type": "tool_use",
            "id": "call-42",
            "name": "lookup",
            "input": {"id": "42"},
        },
    ]
    assert reply.usage == Usage(input=6, output=3, cache_read=4)


def test_legacy_function_call_receives_a_deterministic_id():
    response = _json_response(choices=[{
        "message": {
            "role": "assistant",
            "content": None,
            "function_call": {"name": "lookup", "arguments": "{}"},
        },
        "finish_reason": "function_call",
    }])
    client = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, request=request, json=response),
    ))
    adapter = OpenAICompatibleAdapter(
        base_url="https://provider.test/v1", api_key="secret", client=client,
    )
    first = _run(adapter)
    second = _run(adapter)
    asyncio.run(client.aclose())

    assert first.content[0]["id"].startswith("call_lipas_")
    assert first.content[0]["id"] == second.content[0]["id"]


def test_streaming_normalizes_text_reasoning_and_usage():
    chunks = [
        {
            "id": "stream-1",
            "model": "deepseek-reasoner",
            "choices": [{
                "index": 0,
                "delta": {"reasoning_content": "think", "content": "hel"},
                "finish_reason": None,
            }],
        },
        {
            "id": "stream-1",
            "model": "deepseek-reasoner",
            "choices": [{
                "index": 0,
                "delta": {"content": "lo"},
                "finish_reason": "stop",
            }],
        },
        {
            "choices": [],
            "usage": {"prompt_tokens": 4, "completion_tokens": 2},
        },
    ]
    payload = ": heartbeat\n\n" + "".join(
        f"data:{json.dumps(chunk)}\n\n" for chunk in chunks
    ) + "data: [DONE]\n\n"
    client = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, request=request, text=payload),
    ))
    adapter = OpenAICompatibleAdapter(
        base_url="https://provider.test/v1",
        api_key="secret",
        streaming=True,
        include_usage=True,
        client=client,
    )

    async def collect():
        return [event async for event in adapter.stream(_request())]

    events = asyncio.run(collect())
    asyncio.run(client.aclose())

    assert [event.text for event in events if isinstance(event, Delta)] == [
        "hel",
        "lo",
    ]
    assert [event.text for event in events if isinstance(event, Thinking)] == [
        "think",
    ]
    assert events[-1].reply.content[0]["text"] == "hello"
    assert events[-1].reply.usage == Usage(input=4, output=2)


def test_streaming_reassembles_tool_call_fragments():
    chunks = [
        {"id": "stream-tool", "choices": [{
            "index": 0,
            "delta": {"tool_calls": [{
                "index": 0,
                "id": "call-1",
                "function": {"name": "lookup", "arguments": '{"id"'},
            }]},
            "finish_reason": None,
        }]},
        {"id": "stream-tool", "choices": [{
            "index": 0,
            "delta": {"tool_calls": [{
                "index": 0,
                "function": {"arguments": ':"42"}'},
            }]},
            "finish_reason": "tool_calls",
        }]},
    ]
    payload = "".join(
        f"data: {json.dumps(chunk)}\n\n" for chunk in chunks
    ) + "data: [DONE]\n\n"
    client = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, request=request, text=payload),
    ))
    adapter = OpenAICompatibleAdapter(
        base_url="https://provider.test/v1",
        api_key="secret",
        streaming=True,
        client=client,
    )

    async def collect():
        return [event async for event in adapter.stream(_request())]

    events = asyncio.run(collect())
    asyncio.run(client.aclose())

    assert [
        event.partial_json for event in events if isinstance(event, ToolUseDelta)
    ] == ['{"id"', ':"42"}']
    assert events[-1].reply.content == [{
        "type": "tool_use",
        "id": "call-1",
        "name": "lookup",
        "input": {"id": "42"},
    }]


def test_http_error_is_classifiable_and_redacts_api_key():
    key = "sk-never-persist-this"
    client = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(
            401,
            request=request,
            json={"error": {"message": f"invalid {key}"}},
        ),
    ))
    adapter = OpenAICompatibleAdapter(
        base_url="https://provider.test/v1", api_key=key, client=client,
    )
    reply = _run(adapter)
    asyncio.run(client.aclose())

    assert reply.stop_reason == "error"
    assert classify(reply) is ErrorKind.AUTH
    assert key not in repr(reply.error_detail)
    assert "<redacted>" in repr(reply.error_detail)


def test_transport_timeout_is_a_terminal_classifiable_reply():
    def fail(_request):
        raise httpx.ReadTimeout("slow provider")

    client = httpx.AsyncClient(transport=httpx.MockTransport(fail))
    adapter = OpenAICompatibleAdapter(
        base_url="https://provider.test/v1", api_key="secret", client=client,
    )
    reply = _run(adapter)
    asyncio.run(client.aclose())

    assert reply.stop_reason == "error"
    assert classify(reply) is ErrorKind.TIMEOUT


def test_200_error_payload_and_content_filter_fail_as_typed_replies():
    responses = iter([
        {"error": {"type": "rate_limit_error", "message": "slow down"}},
        _json_response(choices=[{
            "message": {"role": "assistant", "content": "partial"},
            "finish_reason": "content_filter",
        }]),
    ])
    client = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, request=request, json=next(responses)),
    ))
    adapter = OpenAICompatibleAdapter(
        base_url="https://provider.test/v1", api_key="secret", client=client,
    )
    rate_limit = _run(adapter)
    filtered = _run(adapter)
    asyncio.run(client.aclose())

    assert classify(rate_limit) is ErrorKind.RATE_LIMIT
    assert classify(filtered) is ErrorKind.CONTENT_FILTER
    assert filtered.content[0]["text"] == "partial"


@pytest.mark.parametrize(
    "response",
    [
        {"choices": []},
        {"choices": [{"message": {}, "finish_reason": "future_reason"}]},
        {"choices": [{
            "message": {
                "tool_calls": [{"function": {
                    "name": "lookup",
                    "arguments": "[1, 2]",
                }}],
            },
            "finish_reason": "tool_calls",
        }]},
        {
            "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": -1},
        },
    ],
)
def test_malformed_provider_responses_fail_closed(response):
    client = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, request=request, json=response),
    ))
    adapter = OpenAICompatibleAdapter(
        base_url="https://provider.test/v1", api_key="secret", client=client,
    )
    reply = _run(adapter)
    asyncio.run(client.aclose())

    assert reply.stop_reason == "error"
    assert reply.error_detail["type"] == "provider_error"


def test_adapter_owned_request_fields_cannot_be_overridden():
    client = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, request=request, json=_json_response()),
    ))
    adapter = OpenAICompatibleAdapter(
        base_url="https://provider.test/v1", api_key="secret", client=client,
    )
    reply = _run(adapter, _request(extra={"model": "silent-fallback"}))
    asyncio.run(client.aclose())

    assert reply.stop_reason == "error"
    assert "cannot override" in reply.error_detail["provider_error"]["message"]


def test_max_completion_tokens_and_custom_headers_are_explicit():
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        seen["header"] = request.headers["x-provider-project"]
        return httpx.Response(200, request=request, json=_json_response())

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = OpenAICompatibleAdapter(
        base_url="https://provider.test/v1",
        api_key="secret",
        max_tokens_field="max_completion_tokens",
        headers={"X-Provider-Project": "project-1"},
        client=client,
    )
    _run(adapter)
    asyncio.run(client.aclose())

    assert seen["body"]["max_completion_tokens"] == 64
    assert "max_tokens" not in seen["body"]
    assert seen["header"] == "project-1"


def test_agent_factory_reports_configured_streaming_honestly(tmp_path):
    client = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, request=request, json=_json_response()),
    ))
    agent = Agent.openai_compatible(
        "provider-model",
        base_url="https://provider.test/v1",
        api_key="secret",
        session=tmp_path / "agent.db",
        client=client,
    )
    assert agent.adapter.name == "openai-compatible"
    assert agent.capabilities.streaming is False
    assert agent.capabilities.vision is False
    agent.close()

    streaming_agent = Agent.openai_compatible(
        "provider-model",
        base_url="https://provider.test/v1",
        api_key="secret",
        streaming=True,
        client=client,
        model_requirements=ModelRequirements(streaming=True),
    )
    assert streaming_agent.capabilities.streaming is True
    assert streaming_agent.capabilities.vision is False
    streaming_agent.close()
    asyncio.run(client.aclose())


def test_non_streaming_factory_rejects_streaming_requirement():
    with pytest.raises(ModelCapabilityError):
        Agent.openai_compatible(
            "provider-model",
            base_url="https://provider.test/v1",
            api_key="secret",
            model_requirements=ModelRequirements(streaming=True),
        )


@pytest.mark.parametrize(
    "base_url",
    [
        "",
        "provider.test/v1",
        "ftp://provider.test/v1",
        "https://user:secret@provider.test/v1",
        "https://provider.test/v1?key=secret",
        "https://provider.test/v1#fragment",
    ],
)
def test_ambiguous_or_secret_bearing_urls_are_rejected(base_url):
    with pytest.raises(ValueError):
        OpenAICompatibleAdapter(base_url=base_url, api_key="secret")


def test_api_key_and_header_configuration_fail_early(monkeypatch):
    monkeypatch.delenv("MISSING_PROVIDER_KEY", raising=False)
    with pytest.raises(ValueError, match="requires an API key"):
        OpenAICompatibleAdapter(
            base_url="https://provider.test/v1",
            api_key_env="MISSING_PROVIDER_KEY",
        )
    with pytest.raises(ValueError, match="api_key must"):
        OpenAICompatibleAdapter(
            base_url="https://provider.test/v1",
            api_key="   ",
        )
    with pytest.raises(ValueError, match="adapter-owned"):
        OpenAICompatibleAdapter(
            base_url="https://provider.test/v1",
            api_key="secret",
            headers={"Authorization": "another secret"},
        )

    adapter = OpenAICompatibleAdapter(
        base_url="http://127.0.0.1:8000/v1",
        api_key_env=None,
        require_api_key=False,
    )
    assert adapter.api_key is None
