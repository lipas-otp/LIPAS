# 实验性互操作 Adapter

> **Experimental：**这些 adapter 是兼容样例，不是 LIPAS 核心产品界面；它们可能移动或
> 变化，不提供兼容性承诺。

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
