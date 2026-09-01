"""Action Gateway plus LangGraph, MCP, and OpenClaw/OpenCrew contracts."""
from __future__ import annotations

import asyncio
import time

import pytest

from lipas import (
    ActionGateway,
    EnvironmentSecretResolver,
    SecretDetected,
    SecretResolutionError,
    tool,
)
from lipas.rows.effect import EffectRow, F_ARGUMENTS
from lipas.exceptions import OrphanedEffectError
from lipas.integrations import (
    LangGraphActionNode,
    LangGraphToolAdapter,
    MCPActionServer,
    OpenClawActionBackend,
)


def _tools(calls: list[str]):
    @tool(side_effect="read_only")
    def lookup(value: str) -> dict[str, str]:
        """Return one deterministic value."""
        calls.append(f"read:{value}")
        return {"value": value}

    @tool(side_effect="idempotent_write")
    def save(value: str) -> dict[str, str]:
        """Persist one deterministic value."""
        calls.append(f"write:{value}")
        return {"saved": value}

    return lookup, save


def test_gateway_fails_closed_for_write_then_redelivers_recorded_result(tmp_path):
    calls: list[str] = []
    with ActionGateway(_tools(calls), session=tmp_path / "actions.db") as gateway:
        denied = asyncio.run(gateway.call(
            "save", {"value": "x"}, request_id="stable-write",
        ))
        assert denied.status == "approval_required"
        assert calls == []

        first = asyncio.run(gateway.call(
            "save", {"value": "x"}, request_id="stable-write", approved=True,
        ))
        second = asyncio.run(gateway.call(
            "save", {"value": "x"}, request_id="stable-write", approved=True,
        ))
        assert first.status == second.status == "ok"
        assert first.output == second.output == {"saved": "x"}
        assert first.effect_id == second.effect_id
        assert calls == ["write:x"]


def test_gateway_rejects_empty_identity_and_approval_payload_reuse(tmp_path):
    calls: list[str] = []
    with ActionGateway(_tools(calls), session=tmp_path / "identity.db") as gateway:
        with pytest.raises(ValueError, match="request_id"):
            asyncio.run(gateway.call(
                "save", {"value": "x"}, request_id="",
            ))
        denied = asyncio.run(gateway.call(
            "save", {"value": "x"}, request_id="approval-bound",
        ))
        assert denied.status == "approval_required"
        with pytest.raises(ValueError, match="different arguments"):
            asyncio.run(gateway.call(
                "save", {"value": "y"}, request_id="approval-bound", approved=True,
            ))
        assert calls == []

        done = asyncio.run(gateway.call(
            "save", {"value": "x"}, request_id="causal-bound",
            approved=True, caused_by="task-a",
        ))
        assert done.status == "ok"
        with pytest.raises(ValueError, match="different causation"):
            asyncio.run(gateway.call(
                "save", {"value": "x"}, request_id="causal-bound",
                approved=True, caused_by="task-b",
            ))


def test_gateway_rejects_raw_secret_before_any_persistent_claim(tmp_path):
    calls: list[str] = []
    with ActionGateway(_tools(calls), session=tmp_path / "actions.db") as gateway:
        with pytest.raises(SecretDetected) as raised:
            gateway.call_sync(
                "lookup", {"value": "API_KEY=sk-supersecretvalue"},
                request_id="secret-call",
            )
        assert "sk-supersecretvalue" not in str(raised.value)
        assert list(gateway.rowset.store.log) == []

        with pytest.raises(SecretResolutionError):
            gateway.call_sync(
                "lookup", {"value": "secret://env/CUSTOMER_KEY"},
                request_id="missing-resolver",
            )

    resolver = EnvironmentSecretResolver(
        ["CUSTOMER_KEY"], environ={"CUSTOMER_KEY": "actual-secret-value"},
    )
    with ActionGateway(
        _tools(calls), session=tmp_path / "resolved.db",
        secret_resolver=resolver,
    ) as gateway:
        safe = gateway.call_sync(
            "lookup", {"value": "secret://env/CUSTOMER_KEY"},
            request_id="secret-reference",
        )
        assert safe.status == "ok"
        assert safe.output == {"value": "[REDACTED SECRET]"}
        effect_row = next(
            value for value in gateway.rowset.rows if isinstance(value, EffectRow)
        )
        node = effect_row.project(gateway.rowset.store).nodes[safe.effect_id]
        assert node.intent.fields[F_ARGUMENTS] == {
            "value": "secret://env/CUSTOMER_KEY",
        }
        assert calls[-1] == "read:actual-secret-value"


def test_cancelled_sync_thread_is_uncertain_and_cannot_be_retried(tmp_path):
    completed: list[str] = []

    @tool(side_effect="idempotent_write")
    def slow_write(value: str) -> str:
        """Finish after the caller's deadline."""
        time.sleep(0.05)
        completed.append(value)
        return value

    with ActionGateway(
        [slow_write], session=tmp_path / "actions.db",
        default_timeout_s=0.005, allow_writes=True,
    ) as gateway:
        result = gateway.call_sync(
            "slow_write", {"value": "late"}, request_id="slow-write",
        )
        assert result.status == "uncertain"
        assert result.detail == {"timeout_s": 0.005, "orphan": True}
        # The bounded process executor lets the timeout return promptly. The
        # unkillable thread can still finish later, but no false terminal
        # Effect is written in the meantime.
        assert completed in ([], ["late"])
        time.sleep(0.06)
        assert completed == ["late"]
        with pytest.raises(OrphanedEffectError):
            gateway.call_sync(
                "slow_write", {"value": "late"}, request_id="slow-write",
            )
        assert completed == ["late"]


def test_langgraph_node_and_tool_adapter_use_gateway_idempotency(tmp_path):
    calls: list[str] = []
    with ActionGateway(_tools(calls), session=tmp_path / "actions.db") as gateway:
        node = LangGraphActionNode(gateway)
        state = {
            "action": {
                "tool_name": "lookup",
                "arguments": {"value": "node"},
                "request_id": "graph-node-1",
            },
        }
        first = asyncio.run(node(state, {"configurable": {"thread_id": "t-1"}}))
        second = asyncio.run(node(state, {"configurable": {"thread_id": "t-1"}}))
        assert first == second

        adapter = LangGraphToolAdapter(gateway, "lookup")
        result = asyncio.run(adapter.ainvoke({
            "value": "tool", "_lipas_request_id": "graph-tool-1",
        }))
        assert result["output"] == {"value": "tool"}
        assert calls == ["read:node", "read:tool"]
        with pytest.raises(ValueError, match="request id"):
            asyncio.run(adapter.ainvoke({
                "value": "blank", "_lipas_request_id": "",
            }))
        with pytest.raises(ValueError, match="require _lipas_request_id"):
            asyncio.run(adapter.ainvoke({"value": "missing"}))


def test_mcp_lists_annotations_and_calls_read_but_denies_write(tmp_path):
    calls: list[str] = []
    with ActionGateway(_tools(calls), session=tmp_path / "actions.db") as gateway:
        server = MCPActionServer(gateway)
        initialized = asyncio.run(server.handle({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18"},
        }))
        assert initialized["result"]["protocolVersion"] == "2025-06-18"  # type: ignore[index]

        listed = asyncio.run(server.handle({
            "jsonrpc": "2.0", "id": 2, "method": "tools/list",
        }))
        tools = {value["name"]: value for value in listed["result"]["tools"]}  # type: ignore[index]
        assert tools["lookup"]["annotations"]["readOnlyHint"] is True
        assert tools["save"]["annotations"]["readOnlyHint"] is False

        read = asyncio.run(server.handle({
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": "lookup", "arguments": {"value": "mcp"}},
        }))
        assert read["result"]["isError"] is False  # type: ignore[index]
        write = asyncio.run(server.handle({
            "jsonrpc": "2.0", "id": 4, "method": "tools/call",
            "params": {"name": "save", "arguments": {"value": "mcp"}},
        }))
        assert write["result"]["structuredContent"]["status"] == "approval_required"  # type: ignore[index]
        assert calls == ["read:mcp"]
        notification = asyncio.run(server.handle({
            "jsonrpc": "2.0", "method": "tools/call",
            "params": {"name": "lookup", "arguments": {"value": "mcp"}},
        }))
        assert notification["error"]["code"] == -32600  # type: ignore[index]


def test_openclaw_backend_requires_stable_id_and_trusted_approval(tmp_path):
    calls: list[str] = []
    with ActionGateway(_tools(calls), session=tmp_path / "actions.db") as gateway:
        untrusted = OpenClawActionBackend(gateway)
        payload = {
            "tool_name": "save", "arguments": {"value": "crew"},
            "request_id": "crew-task-1", "task_id": "thread-1",
            "approved": True,
        }
        denied = asyncio.run(untrusted.execute(payload))
        assert denied["status"] == "approval_required"

        trusted = OpenClawActionBackend(gateway, trust_caller_approval=True)
        done = asyncio.run(trusted.execute(payload))
        replayed = asyncio.run(trusted.execute(payload))
        assert done["status"] == replayed["status"] == "ok"
        assert done["closeout"]["safe_to_redeliver"] is True
        assert calls == ["write:crew"]
