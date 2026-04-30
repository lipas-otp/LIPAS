import asyncio
import pytest

from lipas.tools import tool, Tool, ToolRegistry
from lipas.runtime import ToolCall, arun_tools


def _run(coro):
    return asyncio.run(coro)


def test_arun_tools_async_handler_awaited():
    @tool
    async def add(a: int, b: int) -> int:
        """Add two ints."""
        await asyncio.sleep(0)
        return a + b

    reg = ToolRegistry().register(add)
    results = _run(arun_tools(reg, [ToolCall(id="1", name="add", arguments={"a": 1, "b": 2})]))
    assert len(results) == 1
    assert results[0].content == "3"
    assert results[0].error_kind is None


def test_arun_tools_sync_handler_called_directly():
    @tool
    def echo(x: str) -> str:
        """Echo."""
        return x

    reg = ToolRegistry().register(echo)
    results = _run(arun_tools(reg, [ToolCall(id="1", name="echo", arguments={"x": "hi"})]))
    assert results[0].content == "hi"


def test_arun_tools_preserves_input_order_not_completion_order():
    @tool
    async def slow(delay: float, label: str) -> str:
        """Sleep then return label."""
        await asyncio.sleep(delay)
        return label

    reg = ToolRegistry().register(slow)
    calls = [
        ToolCall(id="1", name="slow", arguments={"delay": 0.03, "label": "first"}),
        ToolCall(id="2", name="slow", arguments={"delay": 0.01, "label": "second"}),
        ToolCall(id="3", name="slow", arguments={"delay": 0.02, "label": "third"}),
    ]
    results = _run(arun_tools(reg, calls))
    # Completion order would be second → third → first; we assert input order.
    assert [r.content for r in results] == ["first", "second", "third"]
    assert [r.call_id for r in results] == ["1", "2", "3"]


def test_arun_tools_isolates_failures():
    @tool
    async def boom() -> None:
        """Always fails."""
        raise RuntimeError("kaboom")

    @tool
    async def ok() -> str:
        """Succeeds."""
        return "fine"

    reg = ToolRegistry().register(boom).register(ok)
    results = _run(arun_tools(reg, [
        ToolCall(id="1", name="boom", arguments={}),
        ToolCall(id="2", name="ok", arguments={}),
        ToolCall(id="3", name="missing", arguments={}),
        ToolCall(id="4", name="ok", arguments={"unexpected": 1}),
    ]))
    assert results[0].error_kind == "execution_error"
    assert results[0].content.startswith("ToolExecutionError: RuntimeError")
    assert results[1].error_kind is None
    assert results[2].error_kind == "unknown_tool"
    assert results[3].error_kind == "invalid_arguments"


def test_arun_tools_empty_calls():
    reg = ToolRegistry()
    results = _run(arun_tools(reg, []))
    assert results == []


def test_tool_call_rejects_async_handler():
    @tool
    async def a() -> int:
        """Async."""
        return 1

    t: Tool = a  # @tool returns the Tool
    with pytest.raises(TypeError, match="async handler"):
        t.call({})


def test_tool_acall_accepts_sync_handler():
    @tool
    def s(x: int) -> int:
        """Sync."""
        return x * 2

    t: Tool = s
    assert _run(t.acall({"x": 3})) == 6
