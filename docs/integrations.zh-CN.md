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
先获得显式 approval，再发送；之后可用 `connector.reconcile(key)`。异步 gateway timeout 会保留后台调用并允许 late completion
收敛 Effect；无法强杀的同步工具可在 operator/provider lookup 后显式调用
`ToolHarness.reconcile_orphan()`。

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
精确值进行脱敏。当前 LIPAS 尚未提供通用 secret vault 或任意 secret provider 插件。

路径检查、allowlist、审批和脱敏都是防误操作层，不是恶意模型的安全边界。本地
workbench 现在默认通过 Linux Bubblewrap 执行命令：`auto` 与 `bwrap` 无法建立文件系统和
网络 namespace 隔离时会失败关闭。只有对可信代码才应显式使用 `--sandbox local` 这一
不安全 fallback。Bubblewrap 目前覆盖第一方 workbench command；Action Gateway 中的任意
Python 工具仍需由各自 capability、容器或远程 sandbox 提供隔离。
