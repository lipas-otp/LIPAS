# OpenAI-compatible 模型端点

> 语言：[English](model-providers.md) | [中文](model-providers.zh-CN.md)

LIPAS 0.40 延续 0.32 的 provider 边界，可以使用实现 OpenAI-compatible Chat Completions 契约的 provider。接入时
有意要求显式提供 URL、model name 与 credential：LIPAS 不猜测 provider，不静默替换
模型，也不会 fallback 到另一个端点。

先安装 HTTP adapter extra：

```bash
pip install 'lipas[compatible]'
```

原有 `lipas[openai]` extra 继续作为等价兼容别名保留。

## Python

请从环境变量或 secret manager 读取 key，不要把它写进源码：

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

`base_url` 既可以是 provider API root，也可以是完整的 `.../chat/completions` URL；
LIPAS 只会追加一次路径。只接受绝对 HTTP(S) URL；URL 中嵌入 credential、query string
或 fragment 会立即失败。

## 常见 provider 形态

provider 产品、region、model name 和账户权限都会变化。provider 控制台与当前官方文档
始终是权威来源；下表只说明 compatible URL 形态。

| Provider | 常见 `base_url` | key 环境变量示例 | Model 值 |
| --- | --- | --- | --- |
| OpenAI Chat Completions | `https://api.openai.com/v1` | `OPENAI_API_KEY` | 账户已开通的 Chat Completions model |
| 火山引擎方舟 / Volcengine Ark | `https://ark.cn-beijing.volces.com/api/v3` | `ARK_API_KEY` | 方舟界面显示的 endpoint/model ID |
| 阿里云百炼 / Alibaba Bailian | `https://dashscope.aliyuncs.com/compatible-mode/v1` | `DASHSCOPE_API_KEY` | 例如账户已开通的 `qwen-plus` |
| 腾讯混元 / Tencent Hunyuan | `https://api.hunyuan.cloud.tencent.com/v1` | `HUNYUAN_API_KEY` | 账户已开通的混元 model |
| DeepSeek | `https://api.deepseek.com` | `DEEPSEEK_API_KEY` | 例如 `deepseek-chat` 或 `deepseek-reasoner` |

LIPAS 不内置容易过时的 provider preset。显式传入 URL，使 region domain、private gateway
与未来 provider 变更始终留在应用配置中。

## CLI 与 task worker

CLI 接受 key 环境变量的名字，不提供会进入 shell history 的明文 key flag：

```bash
export DEEPSEEK_API_KEY='...'

lipas chat \
  --base-url https://api.deepseek.com \
  --api-key-env DEEPSEEK_API_KEY \
  --model deepseek-chat \
  --once "Explain the current task"
```

compatible-only flag 必须同时提供 `--base-url`，也不能与 custom `--factory` 混用；
非法组合会在 Agent 启动前失败。

执行本地 Task 的命令也使用同一组 flag，包括 `task start`、`task worker`、
`task resume` 与 `task approve`：

```bash
lipas task start . "inspect the tests and report risks" \
  --base-url https://dashscope.aliyuncs.com/compatible-mode/v1 \
  --api-key-env DASHSCOPE_API_KEY \
  --model qwen-plus
```

远程模型端点不会获得 workspace 的直接 authority。它只能请求现有 Workbench 暴露的
受限工具；write 与 command 仍遵守同一套 staging、approval、Effect 与 recovery 规则。

## 配置与 live contract 检查

验证精确 endpoint、credential source、model、transport mode、token field 与默认
capability report，同时不发送任何 network request：

```bash
lipas model check \
  --base-url https://api.deepseek.com \
  --api-key-env DEEPSEEK_API_KEY \
  --model deepseek-chat \
  --json
```

JSON 会明确报告 `network_request_sent: false`，也绝不会打印 key value。只有 operator
确实想发出一次真实 external request 时才增加 `--live`：

```bash
lipas model check \
  --base-url https://api.deepseek.com \
  --api-key-env DEEPSEEK_API_KEY \
  --model deepseek-chat \
  --live \
  --json
```

live probe 可能产生费用。它发送一个很小的 text-only request，报告 provider model、
stop reason、规范化 usage 与 text；失败时以退出码 1 返回已分类、已脱敏的错误。
用 `--model-streaming` 探测 SSE；只有 route 支持 `stream_options` 时才加入
`--include-usage`。

如果可信本地 gateway 明确不使用 authentication，请用 `--no-api-key` 代替
`--api-key-env`。这是显式 opt-out，不会在预期环境变量缺失时自动 fallback；两种
credential mode 互斥。custom `--prompt` 只在同时提供 `--live` 时接纳，dry validation
会直接拒绝，而不是静默忽略。

这个命令有意保持为 direct transport diagnostic：它不授予 tool/workspace authority，
也不持久化 Agent Claim/Effect session。若 probe 本身必须成为 audit evidence，请使用
普通 Agent session。

## Streaming 与兼容控制

非 streaming Chat Completions 是默认路径，因为兼容范围最广，而且通常能返回权威 usage。
只有确认所选 provider/model route 实现 SSE 后才启用：

```python
agent = Agent.openai_compatible(
    model="deepseek-reasoner",
    base_url="https://api.deepseek.com",
    api_key=os.environ["DEEPSEEK_API_KEY"],
    streaming=True,
    include_usage=True,  # 若端点拒绝 stream_options，请省略
)
```

streaming parser 支持 text delta、tool-call argument delta、reasoning-compatible 端点
常用的 `reasoning_content`、末尾 usage chunk、SSE comment，以及 `data:JSON` 和
`data: JSON` 两种 framing。畸形 JSON、多 choice、非法 usage、未知 finish reason 与
不完整 tool call 会变成带审计的 terminal error Reply，不会被当成部分可信的成功。

部分新 route 使用 `max_completion_tokens` 而不是 `max_tokens`：

```python
agent = Agent.openai_compatible(
    model="provider-model",
    base_url="https://provider.example/v1",
    api_key=os.environ["PROVIDER_API_KEY"],
    max_tokens_field="max_completion_tokens",
)
```

`response_format`、`tool_choice` 等 provider extension 可以通过
`Agent(request_extras={...})` 传入。adapter-owned field（`model`、`messages`、token
limit、`stream`、tools、temperature 与 stop sequence）不能通过 extra 覆盖。
可用 `headers={...}` 提供非鉴权 custom header；authorization、transport framing 与
host header 仍由 adapter 持有。

## 能力与错误诚实性

adapter 只报告当前配置真正证明的能力：

- 非 streaming 与 streaming 配置使用不同 capability name；
- generic endpoint 的 tool calling、structured output、reasoning、context length 与
  locality 保持 `unknown`；vision 明确为 false，因为当前 adapter 只接收 text/tool
  message block；
- 应用可为测试过的精确 route 注册 `ModelCapabilities`，并通过 `ModelRequirements`
  强制要求；
- authentication、rate limit、timeout、network、4xx、5xx、content filter 与畸形
  provider response 都进入普通 terminal Reply/error classifier；
- API key 不会进入 request body、URL 或 CLI argument；若 provider 在 HTTP error body
  中回显精确 key，LIPAS 会将它 redacted。

OpenAI-compatible wire shape 并不证明每个 model 都实现了所有 OpenAI feature。部署前请
测试精确的 provider、region、model 与 account 组合。
