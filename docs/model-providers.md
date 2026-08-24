# OpenAI-compatible model endpoints

> Language: [English](model-providers.md) | [中文](model-providers.zh-CN.md)

LIPAS 0.40 retains the 0.32 provider boundary for services that implement the OpenAI-compatible Chat
Completions contract. The integration is intentionally configured with an
explicit URL, model name, and credential: LIPAS does not guess a provider,
silently change a model, or fall back to another endpoint.

Install the HTTP adapter extra:

```bash
pip install 'lipas[compatible]'
```

The existing `lipas[openai]` extra remains an equivalent compatibility alias.

## Python

Read the key from an environment variable or secret manager rather than
placing it in source code:

```python
import os

from lipas import Agent


with Agent.openai_compatible(
    model="deepseek-chat",
    base_url="https://api.deepseek.com",
    api_key=os.environ["DEEPSEEK_API_KEY"],
    instructions="Answer concisely and state uncertainty.",
    session="runs/deepseek.db",
) as agent:
    result = agent.ask("Summarize the release risk")
    print(result.text)
```

`base_url` accepts either the provider's API root or the complete
`.../chat/completions` URL. LIPAS appends the path exactly once. Only absolute
HTTP(S) URLs are accepted; credentials, query strings, and fragments in the
URL fail early.

## Common provider shapes

Provider products, regions, model names, and account permissions change. The
provider console and current provider documentation remain authoritative; the
table only shows the compatible URL shape.

| Provider | Typical `base_url` | Example key variable | Model value |
| --- | --- | --- | --- |
| OpenAI Chat Completions | `https://api.openai.com/v1` | `OPENAI_API_KEY` | a Chat Completions model enabled for the account |
| 火山引擎方舟 / Volcengine Ark | `https://ark.cn-beijing.volces.com/api/v3` | `ARK_API_KEY` | the endpoint/model ID shown by Ark |
| 阿里云百炼 / Alibaba Bailian | `https://dashscope.aliyuncs.com/compatible-mode/v1` | `DASHSCOPE_API_KEY` | for example `qwen-plus` when enabled |
| 腾讯混元 / Tencent Hunyuan | `https://api.hunyuan.cloud.tencent.com/v1` | `HUNYUAN_API_KEY` | a Hunyuan model enabled for the account |
| DeepSeek | `https://api.deepseek.com` | `DEEPSEEK_API_KEY` | for example `deepseek-chat` or `deepseek-reasoner` |

There are no hard-coded provider presets. Passing the URL explicitly keeps
regional domains, private gateways, and future provider changes visible in
application configuration.

## CLI and task worker

The CLI accepts the name of a key environment variable, never a plaintext key
flag that would be copied into shell history:

```bash
export DEEPSEEK_API_KEY='...'

lipas chat \
  --base-url https://api.deepseek.com \
  --api-key-env DEEPSEEK_API_KEY \
  --model deepseek-chat \
  --once "Explain the current task"
```

Compatible-only flags require `--base-url` and cannot be combined with a
custom `--factory`; invalid combinations fail before an Agent starts.

The same flags work on commands that execute local Tasks, including
`task start`, `task worker`, `task resume`, and `task approve`:

```bash
lipas task start . "inspect the tests and report risks" \
  --base-url https://dashscope.aliyuncs.com/compatible-mode/v1 \
  --api-key-env DASHSCOPE_API_KEY \
  --model qwen-plus
```

The remote model endpoint does not receive direct workspace authority. It can
only request the bounded tools exposed by the existing Workbench, and writes
and commands retain the same staging, approval, Effect, and recovery rules.

## Configuration and live contract checks

Validate the exact endpoint, credential source, model, transport mode, token
field, and default capability report without sending any network request:

```bash
lipas model check \
  --base-url https://api.deepseek.com \
  --api-key-env DEEPSEEK_API_KEY \
  --model deepseek-chat \
  --json
```

The JSON explicitly reports `network_request_sent: false` and never prints the
key value. When an operator intends one real external request, add `--live`:

```bash
lipas model check \
  --base-url https://api.deepseek.com \
  --api-key-env DEEPSEEK_API_KEY \
  --model deepseek-chat \
  --live \
  --json
```

The live probe may be billable. It sends a small text-only request, reports
the provider model, stop reason, normalized usage, and text, or returns a
classified and credential-redacted error with exit status 1. Use
`--model-streaming` to probe SSE and add `--include-usage` only when that route
supports `stream_options`.

For a trusted local gateway that deliberately has no authentication, use
`--no-api-key` instead of `--api-key-env`. This is an explicit opt-out, not a
fallback when an expected environment variable is absent. The two credential
modes are mutually exclusive. A custom `--prompt` is accepted only with
`--live`; dry validation rejects it instead of silently ignoring it.

This command is intentionally a direct transport diagnostic: it grants no
tool or workspace authority and does not persist an Agent Claim/Effect session.
Use an ordinary Agent session when the probe itself must become audit evidence.

## Streaming and compatibility controls

Non-streaming Chat Completions is the default because it is the most widely
compatible and normally includes authoritative usage. Enable SSE only after
confirming the selected provider/model route implements it:

```python
agent = Agent.openai_compatible(
    model="deepseek-reasoner",
    base_url="https://api.deepseek.com",
    api_key=os.environ["DEEPSEEK_API_KEY"],
    streaming=True,
    include_usage=True,  # omit if the endpoint rejects stream_options
)
```

The streaming parser supports text deltas, tool-call argument deltas, the
`reasoning_content` field used by reasoning-compatible endpoints, terminal
usage chunks, SSE comments, and both `data:JSON` and `data: JSON` framing.
Malformed JSON, multiple choices, invalid usage, unknown finish reasons, and
incomplete tool calls become terminal audited error Replies rather than
partially trusted successes.

Some newer routes use `max_completion_tokens` instead of `max_tokens`:

```python
agent = Agent.openai_compatible(
    model="provider-model",
    base_url="https://provider.example/v1",
    api_key=os.environ["PROVIDER_API_KEY"],
    max_tokens_field="max_completion_tokens",
)
```

Provider extensions such as `response_format` or `tool_choice` can be passed
through `Agent(request_extras={...})`. Adapter-owned fields (`model`,
`messages`, token limit, `stream`, tools, temperature, and stop sequences)
cannot be overridden through extras. Optional non-authentication headers may
be supplied with `headers={...}`; authorization, transport framing, and host
headers remain adapter-owned.

## Capability and error honesty

The adapter reports only what its configured transport proves:

- non-streaming and streaming configurations have distinct capability names;
- tool calling, structured output, reasoning, context length, and locality
  remain `unknown` for a generic endpoint; vision is explicitly false because
  this adapter currently accepts only text/tool message blocks;
- applications can register exact `ModelCapabilities` for a tested route and
  require them with `ModelRequirements`;
- authentication, rate limit, timeout, network, 4xx, 5xx, content filter, and
  malformed-provider failures use the normal terminal Reply/error classifier;
- API keys are never put in request bodies, URLs, or CLI arguments, and exact
  key values are redacted if a provider echoes one in an HTTP error body.

An OpenAI-compatible wire shape is not proof that every model implements every
OpenAI feature. Test the exact provider, region, model, and account combination
you deploy.
