"""0.46 LangGraph/AutoGen boundary and external-run cancellation contracts."""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from types import SimpleNamespace

from lipas import (
    ActionGateway,
    AgentCoordinator,
    ExternalRunEnvelope,
    RunCancelled,
    RunContext,
    tool,
)
from lipas.integrations.autogen import AutoGenHandoffHandler, AutoGenToolAdapter


def test_external_run_checks_parent_before_persisting_and_propagates_host_cancel(
    tmp_path: Path,
):
    async def scenario() -> None:
        async with _Coordinator(tmp_path) as coordinator:
            envelope = ExternalRunEnvelope(
                "langgraph", "cancel-before", "goal", str(tmp_path),
            )
            context = RunContext.create(run_id="parent")
            context.cancel()
            with pytest.raises(RunCancelled):
                await coordinator.execute_external(envelope, lambda *_: None, context=context)
            assert coordinator.execution.list_tasks() == ()

            async def slow(_envelope, _context):
                await asyncio.sleep(10)

            running = asyncio.create_task(
                coordinator.execute_external(
                    ExternalRunEnvelope(
                        "autogen", "cancel-during", "goal", str(tmp_path),
                    ),
                    slow,
                ),
            )
            await asyncio.sleep(0.05)
            running.cancel()
            with pytest.raises(asyncio.CancelledError):
                await running
            run = coordinator.execution.list_runs()[0]
            assert run.state.value == "cancelled"

    asyncio.run(scenario())


class _Coordinator:
    def __init__(self, path: Path):
        self.path = path
        self.value: AgentCoordinator | None = None

    async def __aenter__(self) -> AgentCoordinator:
        self.value = AgentCoordinator.open(self.path / "coord.db", workspace=self.path)
        return self.value

    async def __aexit__(self, *_):
        assert self.value is not None
        self.value.close()


def test_autogen_metadata_cannot_override_framework_and_blank_tool_ids_fail(
    tmp_path: Path,
):
    calls: list[str] = []

    @tool(side_effect="read_only")
    def lookup(value: str) -> str:
        """Return the supplied value."""
        calls.append(value)
        return value

    async def scenario() -> None:
        with ActionGateway([lookup], session=tmp_path / "gateway.db") as gateway:
            adapter = AutoGenToolAdapter(gateway, "lookup")
            with pytest.raises(ValueError, match="request_id"):
                await adapter.arun({"value": "x"}, request_id=" ")
            with pytest.raises(ValueError, match="request_id"):
                adapter.run({"value": "x"}, request_id=" ")

        captured = {}

        class StubCoordinator:
            async def handoff(self, *args, **kwargs):
                captured.update(kwargs)
                return SimpleNamespace(value="ok", run_id="run-1", replayed=False)

        handler = AutoGenHandoffHandler(StubCoordinator(), "member")
        result = await handler.handle(
            {"x": 1}, conversation_id="chat", request_id="handoff",
            metadata={"framework": "spoofed"},
        )
        assert result["run_id"] == "run-1"
        assert captured["metadata"]["framework"] == "autogen"

    asyncio.run(scenario())
