"""Focused tests for bounded computation tools."""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from lipas.code_tools import CodeToolError, analyze_csv, calculate_expression, execute_python
from lipas.sandbox import LocalCommandSandbox
from lipas.workbench import Workbench, WorkspacePolicyError


def test_calculator_accepts_arithmetic_and_rejects_code() -> None:
    assert calculate_expression("2 + 3 * 4") == 14
    assert calculate_expression("10 / 4") == 2.5
    assert calculate_expression("2 ** 8") == 256
    with pytest.raises(CodeToolError):
        calculate_expression("__import__('os').getcwd()")
    with pytest.raises(CodeToolError):
        calculate_expression("2 ** 101")


def test_csv_profile_is_bounded_and_does_not_return_rows(tmp_path: Path) -> None:
    source = tmp_path / "data.csv"
    source.write_text("name,value\na,1\nb,\nc,3\n", encoding="utf-8")
    result = analyze_csv(source).as_dict()
    assert result["columns"] == ["name", "value"]
    assert result["rows"] == 3
    assert result["numeric"]["value"]["mean"] == 2.0  # type: ignore[index]
    assert result["missing"]["value"] == 1  # type: ignore[index]
    with pytest.raises(CodeToolError, match="row limit"):
        analyze_csv(source, max_rows=2)


def test_python_worker_uses_temp_directory_and_blocks_network() -> None:
    async def run() -> object:
        return await execute_python(
            "import pathlib, socket\n"
            "print(pathlib.Path.cwd().name)\n"
            "try: socket.create_connection(('example.com', 80))\n"
            "except Exception as exc: print(type(exc).__name__)\n",
            sandbox=LocalCommandSandbox(),
        )

    result = asyncio.run(run())
    assert result.exit_code == 0  # type: ignore[attr-defined]
    assert "PermissionError" in result.stdout  # type: ignore[attr-defined]
    assert "lipas-python-" in result.stdout  # type: ignore[attr-defined]
    assert result.network_isolated is False  # type: ignore[attr-defined]


def test_python_worker_timeout_terminates_cpu_bound_code() -> None:
    async def run() -> object:
        return await execute_python(
            "while True: pass",
            sandbox=LocalCommandSandbox(),
            timeout_seconds=1,
        )

    result = asyncio.run(run())
    assert result.timed_out is True  # type: ignore[attr-defined]
    assert result.exit_code is None  # type: ignore[attr-defined]


def test_workbench_computation_tools_record_evidence(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "data.csv").write_text("x\n1\n2\n", encoding="utf-8")
    with Workbench(tmp_path / "home", sandbox="local") as workbench:
        task, run = workbench.create_task("profile data", workspace)
        tools = {value.name: value for value in workbench.workspace_tools(task.id, run.id)}
        info = tools["get_workspace_info"].invoke()
        assert info["available_capabilities"]["python_execution"] is True  # type: ignore[index]
        assert tools["calculate"].invoke(expression="6 * 7")["result"] == 42
        profile = tools["analyze_csv"].invoke(relative_path="data.csv")
        assert profile["rows"] == 2
        code = asyncio.run(tools["python_exec"].acall({"source": "print(40 + 2)"}))
        assert code["stdout"].strip() == "42"
        kinds = {artifact.kind for artifact in workbench.artifacts(task.id)}
        assert {"data_analysis", "code_execution"} <= kinds


def test_python_worker_rejects_invalid_limits(tmp_path: Path) -> None:
    with pytest.raises(WorkspacePolicyError):
        # The Workbench Tool wrapper translates computation errors into its
        # path/authority error type used by other local capabilities.
        with Workbench(tmp_path / "home", sandbox="local") as workbench:
            workspace = tmp_path / "workspace"
            workspace.mkdir()
            task, run = workbench.create_task("compute", workspace)
            tools = {value.name: value for value in workbench.workspace_tools(task.id, run.id)}
            asyncio.run(tools["python_exec"].acall({"source": "print(1)", "timeout_seconds": 0}))
