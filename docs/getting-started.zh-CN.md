# 快速开始

> 语言：[English](getting-started.md) | [中文](getting-started.zh-CN.md)

本页特意保持简短。先复制并运行；想理解每一部分为什么存在时，再阅读
[循序上手 LIPAS](tutorial.zh-CN.md)。

## 1. 启动本地模型

```bash
pip install 'lipas[ollama]'
ollama pull gemma4:12b
```

确认 Ollama 服务已经运行。LIPAS 默认使用
`http://localhost:11434`，也可以通过 `OLLAMA_HOST` 环境变量指定地址。

## 2. 复制一个有用的 Agent

新建 `welcome.py`：

```python
from lipas import Agent, tool


@tool(side_effect="read_only")
def welcome_customer(customer_id: str) -> str:
    """Welcome a new customer without changing any customer data."""
    return f"Welcome, {customer_id}!"


with Agent.ollama(
    tools=[welcome_customer],
    instructions="Use welcome_customer for new customers; answer concisely.",
    session="runs/welcome.db",  # omit for an in-memory run
) as agent:
    result = agent.ask("Welcome the new customer Jason.")

    if result.is_error:
        print("agent error:", result.error)
    else:
        print(result.text)
```

用 `python welcome.py` 运行它。

`Agent.ollama()` 会提供本地 Ollama adapter，并默认使用 `gemma4:12b`，
因此第一个版本不需要填写模型名。模型会收到工具名、docstring 和从类型推导
出的输入 schema，然后自行决定是否调用工具。如果希望它大概率调用工具，请
使用“Welcome the new customer Jason”这种明确请求，不要只输入 `Jason`。

`session=` 会把可检查的运行记录写入 SQLite。`with` 会妥善关闭该文件；
不方便使用上下文管理器时，调用 `agent.close()` 的效果相同。

## 下一步

- 阅读[循序上手 LIPAS](tutorial.zh-CN.md)：一本覆盖工具、结果、session、
  budget、replay、写操作、Skill 和 Team 的小教程。
- 运行 [`examples/01_first_agent.py`](../examples/01_first_agent.py)，查看同一
  结构的完整模块。
- 只有在需要精确了解 replay、持久化或外部操作保证时，再阅读
  [执行模型](execution-model.zh-CN.md)。
