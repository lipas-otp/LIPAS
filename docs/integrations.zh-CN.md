# Capability client 与实验性互操作 Adapter

本指南中的 HTTP/MCP client 是第一方 capability boundary。LangGraph、AutoGen、OpenClaw
与 MCP server bridge 仍是兼容样例，不属于核心产品界面，可能移动或变化，不提供兼容性承诺。

LIPAS 优先发展自己的本地任务 Agent 体验。只有用户已经存在明确互操作需求时，才使用
这些入口。所有入口仍进入同一个 `ActionGateway`，避免实验性 adapter 在产品内部创造
彼此不同的执行语义。

## 定义动作

```python
# actions.py
from lipas import tool

@tool(side_effect="idempotent_write")
def save_note(path: str, text: str) -> dict[str, str]:
    """Save one note after the host has approved the write."""
    # Real deployments should execute this inside an isolated capability.
    ...

def build_tools():
    return [save_note]
```

直接验证网关：

```bash
lipas action call \
  --factory actions:build_tools \
  --session ~/.lipas/actions.db \
  --tool save_note \
  --arguments '{"path":"notes/a.md","text":"hello"}' \
  --request-id task-42-save-1 \
  --approved
```

同一个 `request-id` 和相同参数再次投递会恢复已记录结果；不同参数复用该 id 会失败。
同步工具在线程中运行，不再阻塞 heartbeat。线程无法安全强杀，因此超时会返回
`uncertain` 并留下 orphan，而不是伪造一个 terminal failure。

## LangGraph

```python
from lipas import ActionGateway
from lipas.integrations import LangGraphActionNode, LangGraphToolAdapter

gateway = ActionGateway(build_tools(), session="actions.db")

# 把普通 LangGraph interrupt/审批节点放在它前面。
execute = LangGraphActionNode(gateway, approved=True)

# state["action"] 必须包含稳定 request_id。
# {"tool_name": "save_note", "arguments": {...}, "request_id": "..."}

# 需要 ToolNode/预构建 Agent 时：
tool = LangGraphToolAdapter(gateway, "save_note", approved=True)
langchain_tool = tool.as_langchain_tool()  # 仅此方法需要 langchain-core
```

需要把 LangGraph 节点委托给具名 LIPAS member 时，使用 `LangGraphHandoffNode`；需要
把 AutoGen message 委托给 durable member 时，使用 `AutoGenHandoffHandler`：

```python
from lipas.integrations import AutoGenHandoffHandler, LangGraphHandoffNode

graph_node = LangGraphHandoffNode(runtime.coordinator(), "reviewer")
autogen_handler = AutoGenHandoffHandler(runtime.coordinator(), "reviewer")
```

两个 handoff adapter 都把宿主的 thread/checkpoint/message id 作为 replay identity：不会
随机生成 handoff id，不会把 framework team/graph state model 导入核心，也不会绕过 LIPAS
的 approval、cancellation、budget 与 audit 规则。重启后应使用相同的 member contract
version；如果语义改变，必须使用新的 handoff identity。

## 第一方 HTTP/MCP client 与 Email connector

真实外部 capability 使用 `HttpClient`、`MCPClient`/`MCPHttpClient`，而不是在每个业务
场景里各写一套网络逻辑。HTTP write 在发送前写入 `OperationJournal`，需要稳定
`idempotency_key`；timeout/transport error 会进入 `uncertain`，必须通过同一个 journal 或
`/api/operations/<key>/reconcile` 完成 reconciliation 后才能使用新 key。
MCP `tools/call` notification 如果没有 JSON-RPC id，必须在 params 中提供
`_lipas_request_id`；否则 server 会拒绝，因为它没有可 replay 的 operation identity。

```python
from lipas import EgressPolicy, HttpClient, OperationJournal
from lipas.integrations import MCPClient, MCPHttpClient

http = HttpClient(
    base_url="https://api.example.test/v1",
    egress=EgressPolicy(frozenset({"api.example.test"})),
    journal=OperationJournal("operations.db"),
)
response = await http.request(
    "POST", "messages", json_body={"text": "hello"},
    idempotency_key="ticket-42-message-1",
)
mcp = MCPClient(MCPHttpClient("https://mcp.example.test/mcp"))
await mcp.initialize()
await mcp.call_tool("lookup_ticket", {"id": "42"}, request_id="ticket-42-lookup-1")
```

`EmailConnector` 是 connector boundary，不是新的 Agent 类型。它要求 provider 返回
provider reference 并实现按 idempotency key lookup；pending/uncertain key 会被拒绝，必须
先获得显式 approval，再发送；之后可用 `connector.reconcile(key)`，且 provider lookup 在
found 时必须给出 provider reference，否则仍保持 `uncertain`。异步 gateway timeout 会保留后台调用并允许 late completion
收敛 Effect；无法强杀的同步工具可在 operator/provider lookup 后显式调用
`ToolHarness.reconcile_orphan()`。

如果只需要受限的网页读取，可以在同一个 allowlist client 上包装
`fetch_url_tool`：

```python
from lipas import EgressPolicy, HttpClient, fetch_url_tool

http = HttpClient(
    base_url="https://docs.example.test",
    egress=EgressPolicy(frozenset({"docs.example.test"})),
)
fetch_url = fetch_url_tool(http)
```

该 Tool 沿用 client 的 HTTPS/host、timeout 与重定向策略，返回有大小上限的可见文本和
内容 digest。搜索 adapter 应只对自己 allowlist API 返回的 URL 调用它，并保留来源与引用
metadata。

本地 RAG 可以使用 `KnowledgeStore`，它提供持久化 lexical index，但不会成为对话记忆或
Claim authority：

```python
from lipas import KnowledgeStore

with KnowledgeStore("knowledge.db") as knowledge:
    knowledge.ingest("docs/guide.md", guide_text, scope="team-a")
    hits = knowledge.search("approval workflow", scope="team-a")
```

每个 hit 都包含 source、chunk、scope 和文档 digest。只应写入已经获授权的文本，并把引用
随生成结果一起保存。

## Remote worker、Web SSE 与附件

`RemoteWorkerHTTPClient`/`RemoteWorkerHTTPServer` 是 hybrid execution 的
provider-neutral reference transport。client 默认要求 HTTPS，将 fenced lease
发送到 worker，并用 HMAC-SHA256 签名 worker capability fingerprint；server 会先验证
worker id 与 attestation，再调用 worker，返回包含 event、checkpoint 和 Effect
observation 的结构化结果。`allow_http=True` 仅用于本地测试；生产部署仍需自己负责
TLS 证书、secret rotation、网络策略和 worker admission。

`TLSConfig` 会校验私钥权限，并提供证书 SHA-256 fingerprint 供轮换记录使用。
`OperatorServer.reload_tls()` 与 `RemoteWorkerHTTPServer.reload_tls()` 不重新绑定监听端口，
只替换后续连接使用的 context；现有连接继续使用已协商的会话。
`RemoteWorkerHTTPClient.reload_tls()` 也支持在 CA/client trust context 轮换后替换未来连接的
context。部署层应原子替换证书和私钥，构造新的 `TLSConfig`，记录轮换前后 fingerprint，
reload 后再做探针验证。

Local Web operator 提供可重连、基于 cursor 的 SSE 批次：

```text
GET /api/conversations/<id>/stream?after=0&limit=100
GET /api/runs/<id>/stream?after=0&limit=100
```

当 `require_authentication=True` 时使用 `Authorization: Bearer <token>`。无法设置 header 的
浏览器 `EventSource` 可以使用等价的短期 `access_token` query 参数；不要把长期 secret
放入 URL 或 access log。SSE 是 catch-up
传输，不是无限内存流；SQLite conversation/Run event log 仍是 replay authority。
Conversation attachment 通过有界 JSON base64 上传，保存在 workspace 下的生成路径中，带
SHA-256 digest 和幂等 id；filename 只作为 metadata，不能逃逸 workspace。

## Extension registry 的真实验签

`ExtensionSigner` 对 canonical manifest metadata 加 artifact digest 生成 HMAC-SHA256
签名。配置 signer 后，`ExtensionRegistry` 在 certification 前验证签名；artifact 或
manifest 被篡改会 fail closed。`ExtensionRegistryService` 在 `/v1/extensions` 提供受认证的
注册/撤销和只读 certification metadata，但不会 import、install 或执行 package。签名
secret 应放在部署 secret store；certification 是 admission metadata，不是 execution authority。

## Design-partner 验证

`run_design_partner_validation()` 可对本地 fixture 或外部 adapter（设置
`evidence_scope="external_adapter"`）执行同一组有界 case，生成
包含 run identity、unsafe-delivery、reconciliation time、operator acceptance 和 failure
categories 的报告。本地报告会标记为 `local_fixture`，并明确要求外部 partner evidence，
不能冒充客户验证。真实 partner 提供验收 artifact 后，可用
`DesignPartnerSignoff.from_file()` 记录 SHA-256，再调用 `report.with_signoff()`；只有
artifact 仍与 digest 一致时，报告才会暴露 `externally_accepted=True`。

真实 provider workflow 使用显式 opt-in helper：

```python
from lipas import run_provider_workflow

evidence = await run_provider_workflow(
    agent, runtime.execution, "summarize release notes",
    workspace=project_dir, live=True,
    request_id="release-notes-2026-08-30",
)
```

它只创建一个确定性的 Task/Run，继续走 durable Agent/Effect 路径，并返回有界的
provider/model/terminal evidence。`live=True` 是强制的，因为请求可能计费；本地 fixture
仍应使用普通 Agent API，并保持 fixture 标记。

## Hermes MCP

LIPAS 自带无第三方 MCP SDK 依赖的标准 stdio server：

```bash
lipas mcp serve \
  --factory actions:build_tools \
  --session ~/.lipas/hermes-actions.db
```

把这条命令注册为 Hermes 的 stdio MCP server。`tools/list` 会输出 MCP tool annotations；
write 默认失败关闭。只有当 Hermes 所在的可信 host 已经完成逐次审批时，才给 server 增加
`--allow-writes`。这个开关授予整个 server 写权限，不等同于 OS sandbox。

## OpenCrew / OpenClaw

OpenClaw shim 使用 JSON envelope：

```bash
lipas action openclaw \
  --factory actions:build_tools \
  --session ~/.lipas/opencrew-actions.db \
  --payload '{
    "task_id":"thread-42",
    "request_id":"thread-42-save-1",
    "tool_name":"save_note",
    "arguments":{"path":"notes/a.md","text":"hello"},
    "approved":true
  }' \
  --trust-caller-approval
```

仅当调用方经过身份认证、`approved` 无法由模型伪造时，才使用
`--trust-caller-approval`。输出包含 Effect id、状态和适合 OpenCrew Closeout 的
`safe_to_redeliver` / `requires_reconciliation`。

## Secret 与隔离边界

Action Gateway 会在写入任何 Claim 之前拒绝常见原始 secret。参数应传
opaque reference，并且仅在工具即将执行时解析。内置环境变量 resolver 必须明确列出
允许访问的变量名：

```python
from lipas import ActionGateway, EnvironmentSecretResolver

gateway = ActionGateway(
    build_tools(),
    session="actions.db",
    secret_resolver=EnvironmentSecretResolver(["CUSTOMER_API_KEY"]),
)
# 工具参数现在可以传 secret://env/CUSTOMER_API_KEY。
```

Effect intent 只保留引用，不保留解析后的值；工具返回值与异常会在持久化前，对已解析的
精确值进行脱敏。若使用 KMS/HSM/secret manager，可注入 `ManagedSecretResolver`，并按需提供
provider 专用 redactor；未提供时 LIPAS 使用有界的内存精确值脱敏。`vault://` 等自定义
namespace 会先经过 allowlist 和 resolver，不会作为普通字符串透传。部署层 callback 负责认证、
轮换、TTL 和外部审计；本地文件 resolver 本身不等价于密钥托管服务。

OpenAI-compatible adapter 也可以使用同一边界：传入
`api_key_reference="secret://..."` 与 `SecretResolver`，不要在代码中内联 `api_key`。
解析只发生在 adapter 构造时，key 仅留在 HTTP client 内存中；部署配置只应保存 opaque reference。
外部轮换后可调用 `adapter.reload_api_key()`（或重新构造 Agent）再发起后续请求；已经构造 header
的 in-flight 请求继续使用旧 key。

路径检查、allowlist、审批和脱敏都是防误操作层，不是恶意模型的安全边界。本地
workbench 现在默认通过 Linux Bubblewrap 执行命令：`auto` 与 `bwrap` 无法建立文件系统和
网络 namespace 隔离时会失败关闭。只有对可信代码才应显式使用 `--sandbox local` 这一
不安全 fallback。Bubblewrap 目前覆盖第一方 workbench command；Action Gateway 中的任意
Python 工具仍需由各自 capability、容器或远程 sandbox 提供隔离。
