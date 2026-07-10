import logging
import pytest

from lipas.tools import SideEffectClass, tool, ToolRegistry, InvalidArgumentsError
from lipas.runtime import ToolCall, ToolResult, run_tools


@pytest.fixture
def reg():
    r = ToolRegistry()

    @r.register
    @tool(side_effect=SideEffectClass.PURE)
    def echo(msg: str) -> str:
        """Return msg."""
        return msg

    @r.register
    @tool(side_effect=SideEffectClass.PURE)
    def boom(x: int) -> int:
        """Always raises."""
        raise RuntimeError("kaboom")

    return r


def test_success(reg):
    [res] = run_tools(reg, [ToolCall("c1", "echo", {"msg": "hi"})])
    assert res == ToolResult("c1", "hi", None)


def test_unknown_tool(reg):
    [res] = run_tools(reg, [ToolCall("c1", "nope", {})])
    assert res.is_error and res.error_kind == "unknown_tool"
    assert res.output.startswith("UnknownTool:")


def test_invalid_arguments_unexpected_kwarg(reg):
    [res] = run_tools(reg, [ToolCall("c1", "echo", {"wrong": "x"})])
    assert res.is_error and res.error_kind == "invalid_arguments"


def test_invalid_arguments_missing_required(reg):
    [res] = run_tools(reg, [ToolCall("c1", "echo", {})])
    assert res.is_error and res.error_kind == "invalid_arguments"


def test_execution_error(reg):
    [res] = run_tools(reg, [ToolCall("c1", "boom", {"x": 1})])
    assert res.is_error and res.error_kind == "execution_error"
    assert "RuntimeError" in res.output
    assert "kaboom" in res.output


def test_batch_isolation(reg):
    results = run_tools(reg, [
        ToolCall("a", "echo", {"msg": "ok"}),
        ToolCall("b", "boom", {"x": 1}),
        ToolCall("c", "echo", {"msg": "still ok"}),
    ])
    assert [r.is_error for r in results] == [False, True, False]
    assert [r.call_id for r in results] == ["a", "b", "c"]


def test_base_exception_propagates(reg):
    @reg.register
    @tool(side_effect=SideEffectClass.PURE)
    def interrupt_me() -> None:
        """Simulates Ctrl-C."""
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        run_tools(reg, [ToolCall("c1", "interrupt_me", {})])


def test_log_levels(reg, caplog):
    caplog.set_level(logging.DEBUG, logger="lipas.runtime")
    run_tools(reg, [
        ToolCall("a", "nope", {}),                # unknown_tool -> INFO
        ToolCall("b", "echo", {"wrong": "x"}),    # invalid_args -> INFO
        ToolCall("c", "boom", {"x": 1}),          # exec_error   -> WARNING
    ])
    levels = {
        r.getMessage().split()[0]: r.levelno
        for r in caplog.records
        if r.name == "lipas.runtime"
    }
    assert levels["UnknownTool"] == logging.INFO
    assert levels["InvalidArguments"] == logging.INFO
    assert levels["ToolExecutionError"] == logging.WARNING


def test_tool_call_direct_raises_invalid_arguments(reg):
    """Tool.call is a public API; direct callers see TypeError subtype."""
    tool_obj = reg.get("echo")
    with pytest.raises(InvalidArgumentsError):
        tool_obj.call({"wrong": "x"})
    with pytest.raises(TypeError):  # duck-typing compat
        tool_obj.call({"wrong": "x"})


def test_tool_call_preserves_body_exception(reg):
    """Body exceptions are NOT wrapped by Tool.call—runtime classifies."""
    tool_obj = reg.get("boom")
    with pytest.raises(RuntimeError, match="kaboom"):
        tool_obj.call({"x": 1})
