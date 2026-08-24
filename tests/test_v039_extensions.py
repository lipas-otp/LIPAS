"""0.39 extension SDK and framework boundary contracts."""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from lipas import (
    AgentCoordinator,
    ExtensionManifest,
    run_conformance,
    scaffold_extension,
)
from lipas.integrations import (
    AutoGenHandoffHandler,
    LangGraphHandoffNode,
)


def test_extension_scaffold_and_offline_conformance(tmp_path: Path):
    manifest = scaffold_extension(tmp_path / "extension", "Demo Connector")
    assert manifest.entrypoint == "demo_connector:build"
    assert (tmp_path / "extension" / "lipas-extension.json").exists()
    report = run_conformance(
        manifest,
        scenario_names=set(),
        skill_names=set(),
    )
    assert report.passed
    with pytest.raises(FileExistsError):
        scaffold_extension(tmp_path / "extension", "Demo Connector")


def test_extension_manifest_round_trip():
    manifest = ExtensionManifest(
        "demo",
        scenarios=("draft",),
        skills=("tone",),
    )
    restored = ExtensionManifest.from_mapping(manifest.as_dict())
    assert restored == manifest


def test_extension_conformance_checks_version_and_connector_safety():
    manifest = ExtensionManifest(
        "mail",
        lipas_min_version="0.40.0",
        lipas_max_version="0.40.9",
        provenance="registry:example",
        connector_scope=("email.send",),
        requires_approval=True,
        supports_reconciliation=True,
    )
    assert run_conformance(manifest, lipas_version="0.40.2").passed
    report = run_conformance(manifest, lipas_version="0.41.0")
    assert not report.passed
    assert any(check.name == "compatibility.lipas" for check in report.failures)
    unsafe = ExtensionManifest(
        "unsafe-mail",
        connector_scope=("email.send",),
    )
    unsafe_report = run_conformance(unsafe, lipas_version="0.40.0")
    assert not unsafe_report.passed
    assert any(check.name == "connector.contract" for check in unsafe_report.failures)


def test_langgraph_and_autogen_handoff_adapters_require_stable_ids(tmp_path: Path):
    async def scenario() -> None:
        async def member(payload: Any) -> dict[str, Any]:
            return {"echo": payload}

        with AgentCoordinator.open(tmp_path / "coord.db") as coordinator:
            coordinator.add("worker", member)
            graph = LangGraphHandoffNode(coordinator, "worker")
            result = await graph(
                {"input": "graph", "coordination_id": "thread-1"},
                {"configurable": {"checkpoint_id": "checkpoint-1"}},
            )
            assert result["output"] == {"echo": "graph"}
            replay = await graph(
                {"input": "graph", "coordination_id": "thread-1"},
                {"configurable": {"checkpoint_id": "checkpoint-1"}},
            )
            assert replay["_lipas_replayed"] is True

            autogen = AutoGenHandoffHandler(coordinator, "worker")
            handled = await autogen.handle(
                "autogen", conversation_id="conversation-1", request_id="message-1",
            )
            assert handled["content"] == {"echo": "autogen"}
            with pytest.raises(ValueError):
                await graph(
                    {"input": "missing", "coordination_id": "thread-2"},
                    {"configurable": {}},
                )

    asyncio.run(scenario())
