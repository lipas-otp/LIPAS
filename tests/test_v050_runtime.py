"""Focused regression tests for the 0.50 Runtime Semantics façade."""
from __future__ import annotations

import asyncio
import math
from pathlib import Path

import pytest

from lipas import (
    AgentRuntime,
    EffectDecision,
    EffectObservation,
    EffectProposal,
    ExecutionLeaseError,
    ExecutionStateError,
    ExecutionStore,
    RemoteWorkerLease,
    RemoteWorkerRunner,
    WorkerCapabilities,
    WorkspaceStorage,
    WorkspaceSchemaMismatch,
    ExtensionManifest,
    ExtensionRegistry,
)
from lipas.adapter import Request
from lipas.effect import EffectKind, LLMTarget, ToolTarget
from lipas.harness import LLMHarness
from lipas.rows import RowSet
from lipas.rows.capability import CapabilityRow
from lipas.rows.effect import (
    EffectRow,
    F_CAUSED_BY,
    F_PROPOSAL_ID,
    F_PROPOSAL_METADATA,
    TAG_EFFECT_INTENT,
)
from lipas.rows.history import HistoryRow
from lipas.replay import ReplayCursor
from lipas.store import ClaimStore
from lipas.tool_harness import ToolHarness
from lipas.tools import ToolRegistry, tool
from tests.fake_adapter import FakeAdapter


def _proposal(**kwargs: object) -> EffectProposal:
    values: dict[str, object] = {
        "effect_id": "effect-1",
        "kind": "email_send",
        "actor": "agent",
        "capabilities": frozenset({"email.send"}),
        "estimate": {"calls": 1},
        "risk": "external_write",
    }
    values.update(kwargs)
    return EffectProposal(**values)  # type: ignore[arg-type]


def test_runtime_admission_checks_capability_budget_and_approval(tmp_path: Path):
    with AgentRuntime.open(tmp_path / "runtime", sandbox="local") as runtime:
        proposal = _proposal()

        denied_capability = runtime.decide_effect(
            proposal, available_capabilities={"code.exec"},
            budget_remaining={"calls": 1}, approved=True,
        )
        assert denied_capability.reason == "capability_denied"
        assert denied_capability.detail == {"missing": ["email.send"]}

        denied_budget = runtime.decide_effect(
            proposal, available_capabilities={"email.send"},
            budget_remaining={"calls": 0}, approved=True,
        )
        assert denied_budget.reason == "budget_exceeded"

        denied_approval = runtime.decide_effect(
            proposal, available_capabilities={"email.send"},
            budget_remaining={"calls": 1},
        )
        assert denied_approval.reason == "approval_required"

        admitted = runtime.decide_effect(
            proposal, available_capabilities={"email.send"},
            budget_remaining={"calls": 1}, approved=True,
        )
        assert admitted.allowed and admitted.reason == "allowed"
        assert proposal.as_dict()["capabilities"] == ["email.send"]
        assert admitted.as_dict()["allowed"] is True


def test_runtime_decision_rejects_unknown_risk_label(tmp_path: Path):
    with AgentRuntime.open(tmp_path / "runtime", sandbox="local") as runtime:
        decision = runtime.decide_effect(
            _proposal(risk="vendor_defined_risk"),
            available_capabilities={"email.send"},
            approved=True,
        )
    assert not decision.allowed and decision.reason == "risk_unknown"


def test_effect_contracts_snapshot_nested_policy_data():
    metadata = {"nested": {"value": 1}}
    detail = {"nested": {"value": 2}}
    proposal = _proposal(metadata=metadata)
    decision = EffectDecision(True, detail=detail)
    metadata["nested"]["value"] = 9
    detail["nested"]["value"] = 8
    assert proposal.metadata["nested"] == {"value": 1}
    assert decision.detail["nested"] == {"value": 2}


def test_runtime_admission_rejects_ambiguous_input_boundaries(tmp_path: Path):
    with AgentRuntime.open(tmp_path / "runtime", sandbox="local") as runtime:
        proposal = _proposal(risk="none")
        with pytest.raises(TypeError, match="must not be a string"):
            runtime.decide_effect(proposal, available_capabilities="email.send")
        with pytest.raises(ValueError, match="finite non-negative"):
            runtime.decide_effect(
                proposal, available_capabilities={"email.send"},
                budget_remaining={"calls": math.nan},
            )
        with pytest.raises(ValueError, match="finite non-negative"):
            runtime.decide_effect(
                proposal, available_capabilities={"email.send"},
                budget_remaining={"calls": -1},
            )
        with pytest.raises(TypeError, match="approved must be bool"):
            runtime.decide_effect(
                proposal, available_capabilities={"email.send"}, approved=1,
            )


def test_effect_observation_rejects_non_string_status():
    with pytest.raises(ValueError, match="status is invalid"):
        EffectObservation("effect-1", [])


def test_expired_worker_lease_cannot_commit_after_reclaim(tmp_path: Path):
    database = tmp_path / "execution.db"
    with ExecutionStore(database) as execution:
        task = execution.create_task("remote", tmp_path)
        run = execution.create_run(task.id)
        first = execution.claim_run(run.id, lease_seconds=1, now=100)
        second = execution.claim_run(run.id, lease_seconds=1, now=102)

    class Worker:
        capabilities = WorkerCapabilities("worker-a")

        async def execute(self, task, lease):  # pragma: no cover - protocol shape
            return task.id, lease.fence

    lease = RemoteWorkerLease(
        first.id,
        first.task_id,
        "worker-a",
        first.attempt,
        first.lease_token or "",
        first.lease_expires or 0,
    )
    runner = RemoteWorkerRunner(database, Worker())
    with pytest.raises(ExecutionLeaseError):
        runner.complete(lease, {"stale": True})

    with ExecutionStore(database) as execution:
        current = execution.get_run(second.id)
        assert current is not None and current.attempt == second.attempt
        assert current.state.value == "running"


def test_runtime_effect_bridge_persists_proposal_and_observation(tmp_path: Path):
    calls = 0

    @tool(side_effect="pure")
    def greet(name: str) -> str:
        """Return one deterministic greeting."""
        nonlocal calls
        calls += 1
        return f"hello {name}"

    rowset = RowSet(ClaimStore(), [EffectRow(), HistoryRow(), CapabilityRow()])
    harness = ToolHarness(tools=ToolRegistry([greet]), rowset=rowset)
    proposal = _proposal(
        effect_id="chat-greeting-1",
        kind="greeting",
        capabilities=frozenset(),
        estimate={"tool_calls": 1},
        risk="none",
        metadata={
            "proposal_id": "must-not-overwrite-identity",
            "custom": "namespaced",
        },
    )
    target = ToolTarget(greet, {"name": "Ada"})

    with AgentRuntime.open(tmp_path / "runtime", sandbox="local") as runtime:
        first = asyncio.run(
            runtime.execute_effect(
                proposal,
                harness=harness,
                target=target,
                budget_remaining={"tool_calls": 1},
            ),
        )
        second = asyncio.run(
            runtime.execute_effect(
                proposal,
                harness=harness,
                target=target,
                budget_remaining={"tool_calls": 1},
            ),
        )

    assert calls == 1
    assert first.status == second.status == "succeeded"
    assert first.effect_id == proposal.effect_id
    assert first.claim_id is not None and first.claim_id.startswith("tool_")
    intents = rowset.store.filter(tag=TAG_EFFECT_INTENT)
    assert len(intents) == 1
    assert intents[0].fields[F_PROPOSAL_ID] == proposal.effect_id
    assert intents[0].fields[F_PROPOSAL_METADATA] == {
        "proposal_id": "must-not-overwrite-identity",
        "custom": "namespaced",
    }
    assert "custom" not in intents[0].fields


def test_repeated_proposal_identity_cannot_change_provenance(tmp_path: Path):
    @tool(side_effect="pure")
    def greet(name: str) -> str:
        """Return one deterministic greeting."""
        return f"hello {name}"

    rowset = RowSet(ClaimStore(), [EffectRow(), HistoryRow(), CapabilityRow()])
    harness = ToolHarness(tools=ToolRegistry([greet]), rowset=rowset)
    original = _proposal(
        effect_id="immutable-proposal-1",
        kind="greeting",
        capabilities=frozenset(),
        risk="none",
    )
    changed = _proposal(
        effect_id=original.effect_id,
        kind=original.kind,
        actor="another-agent",
        capabilities=frozenset(),
        risk="none",
    )
    with AgentRuntime.open(tmp_path / "runtime", sandbox="local") as runtime:
        asyncio.run(
            runtime.execute_effect(
                original,
                harness=harness,
                target=ToolTarget(greet, {"name": "Ada"}),
            ),
        )
        with pytest.raises(ValueError, match="different proposal provenance"):
            asyncio.run(
                runtime.execute_effect(
                    changed,
                    harness=harness,
                    target=ToolTarget(greet, {"name": "Ada"}),
                ),
            )


def test_effect_identity_cannot_change_causation(tmp_path: Path):
    @tool(side_effect="pure")
    def greet(name: str) -> str:
        """Return one deterministic greeting."""
        return f"hello {name}"

    rowset = RowSet(ClaimStore(), [EffectRow(), HistoryRow(), CapabilityRow()])
    harness = ToolHarness(tools=ToolRegistry([greet]), rowset=rowset)
    with AgentRuntime.open(tmp_path / "runtime", sandbox="local") as runtime:
        first = asyncio.run(runtime.execute_effect(
            _proposal(effect_id="causal-effect", kind="greeting", capabilities=frozenset(), risk="none"),
            harness=harness,
            target=ToolTarget(greet, {"name": "Ada"}),
        ))
        assert first.status == "succeeded"
        with pytest.raises(ValueError, match="different causation"):
            asyncio.run(harness.call(
                tool_name="greet",
                arguments={"name": "Ada"},
                effect_id=first.claim_id,
                caused_by="different-parent",
            ))


def test_runtime_effect_bridge_persists_admission_rejection(tmp_path: Path):
    calls = 0

    @tool(side_effect="external_write")
    def send(value: str) -> str:
        """Represent a world-changing action."""
        nonlocal calls
        calls += 1
        return value

    rowset = RowSet(ClaimStore(), [EffectRow(), HistoryRow(), CapabilityRow()])
    harness = ToolHarness(tools=ToolRegistry([send]), rowset=rowset)
    proposal = _proposal(
        effect_id="send-1",
        kind="email_send",
        capabilities=frozenset(),
        estimate={"tool_calls": 1},
        risk="external_write",
    )
    with AgentRuntime.open(tmp_path / "runtime", sandbox="local") as runtime:
        observation = asyncio.run(
            runtime.execute_effect(
                proposal,
                harness=harness,
                target=ToolTarget(send, {"value": "hello"}),
                budget_remaining={"tool_calls": 1},
            ),
        )
    assert calls == 0
    assert observation.status == "rejected"


def test_runtime_effect_bridge_supports_llm_harness(tmp_path: Path):
    rowset = RowSet(ClaimStore(), [EffectRow(), HistoryRow(), CapabilityRow()])
    harness = LLMHarness(adapter=FakeAdapter.echoing(), rowset=rowset)
    proposal = _proposal(
        effect_id="answer-1",
        kind="answer",
        capabilities=frozenset(),
        estimate={"tokens": 1},
        risk="none",
    )
    request = Request("fake", [{"role": "user", "content": "hello"}], 16)
    with AgentRuntime.open(tmp_path / "runtime", sandbox="local") as runtime:
        observation = asyncio.run(
            runtime.execute_effect(
                proposal,
                harness=harness,
                target=LLMTarget(request),
                budget_remaining={"tokens": 1},
            ),
        )
    assert observation.status == "succeeded"
    assert observation.claim_id is not None and observation.claim_id.startswith("call_")


def test_runtime_effect_bridge_materialises_llm_replay_evidence(tmp_path: Path):
    source = RowSet(ClaimStore(), [EffectRow(), HistoryRow(), CapabilityRow()])
    source_harness = LLMHarness(adapter=FakeAdapter.echoing(), rowset=source)
    request = Request("fake", [{"role": "user", "content": "hello"}], 16)
    asyncio.run(source_harness.call(request))

    target = RowSet(ClaimStore(), [EffectRow(), HistoryRow(), CapabilityRow()])
    replay_harness = LLMHarness(
        adapter=FakeAdapter.echoing(),
        rowset=target,
        replay_cursor=ReplayCursor.from_store(source.store),
    )
    proposal = _proposal(
        effect_id="replayed-answer-1",
        kind="answer",
        capabilities=frozenset(),
        estimate={"tokens": 1},
        risk="none",
    )
    with AgentRuntime.open(tmp_path / "runtime", sandbox="local") as runtime:
        observation = asyncio.run(
            runtime.execute_effect(
                proposal,
                harness=replay_harness,
                target=LLMTarget(request),
                budget_remaining={"tokens": 1},
            ),
        )
    assert observation.status == "succeeded"
    assert target.project("effect").nodes[observation.claim_id or ""].is_terminal


def test_proposal_causation_and_product_identity_survive_orphan_reconcile():
    @tool(side_effect="idempotent_write")
    def deliver(value: str) -> str:
        """Represent a delivery whose provider outcome is initially unknown."""
        return value

    rowset = RowSet(ClaimStore(), [EffectRow(), HistoryRow(), CapabilityRow()])
    harness = ToolHarness(tools=ToolRegistry([deliver]), rowset=rowset)
    proposal = _proposal(
        effect_id="delivery-uncertain-1",
        kind="delivery",
        capabilities=frozenset(),
        risk="none",
        caused_by="handoff-1",
    )
    claim_id = proposal.claim_id(EffectKind.TOOL_CALL)
    harness._fold_intent(
        claim_id,
        deliver,
        {"value": "accepted"},
        None,
        None,
        proposal=proposal,
    )

    reconciled = harness.reconcile_orphan(
        proposal.effect_id,
        output="accepted",
    )
    observation = harness.observation(proposal.effect_id)
    assert "is_error" not in reconciled
    assert observation.status == "succeeded"
    assert observation.claim_id == claim_id
    node = rowset.project("effect").nodes[claim_id]
    assert node.intent.fields[F_CAUSED_BY] == "handoff-1"


def test_runtime_run_scoped_effect_helper_uses_durable_evidence(tmp_path: Path):
    @tool(side_effect="pure")
    def greet(name: str) -> str:
        """Return a greeting for the scoped-runtime test."""
        return f"hello {name}"

    with AgentRuntime.open(tmp_path / "runtime", sandbox="local") as runtime:
        task, run = runtime.workbench.create_task("greet", tmp_path)
        in_memory = RowSet(ClaimStore(), [EffectRow(), HistoryRow(), CapabilityRow()])
        harness = ToolHarness(tools=ToolRegistry([greet]), rowset=in_memory)
        proposal = _proposal(effect_id="scoped-effect", kind="greeting", capabilities=frozenset(), risk="none")
        observation = asyncio.run(runtime.execute_effect_for_run(
            run.id, proposal, harness=harness, target=ToolTarget(greet, {"name": "Ada"}),
        ))
        assert observation.status == "succeeded"
        evidence = runtime.claims_for_run(run.id)
        try:
            assert evidence.project("effect").nodes[observation.claim_id or ""].is_terminal
        finally:
            evidence.store.close()


def test_runtime_run_scoped_llm_replay_cursor_isolated_per_run(tmp_path: Path):
    source = RowSet(ClaimStore(), [EffectRow(), HistoryRow(), CapabilityRow()])
    source_harness = LLMHarness(adapter=FakeAdapter.echoing(), rowset=source)
    request = Request("fake", [{"role": "user", "content": "hello"}], 16)
    asyncio.run(source_harness.call(request))
    replay_harness = LLMHarness(
        adapter=FakeAdapter.echoing(),
        rowset=source,
        replay_cursor=ReplayCursor.from_store(source.store),
    )
    with AgentRuntime.open(tmp_path / "runtime", sandbox="local") as runtime:
        _task_a, run_a = runtime.workbench.create_task("a", tmp_path)
        _task_b, run_b = runtime.workbench.create_task("b", tmp_path)
        proposal_a = _proposal(effect_id="replay-a", kind="answer", capabilities=frozenset(), risk="none")
        proposal_b = _proposal(effect_id="replay-b", kind="answer", capabilities=frozenset(), risk="none")
        first = asyncio.run(runtime.execute_effect_for_run(
            run_a.id, proposal_a, harness=replay_harness, target=LLMTarget(request),
        ))
        second = asyncio.run(runtime.execute_effect_for_run(
            run_b.id, proposal_b, harness=replay_harness, target=LLMTarget(request),
        ))
    assert first.status == second.status == "succeeded"
    # The caller-owned cursor is not consumed by scoped execution.
    assert not replay_harness.replay_cursor.exhausted()  # type: ignore[union-attr]


def test_runtime_run_scoped_effect_requires_active_lease_and_projects_observation(
    tmp_path: Path,
):
    @tool(side_effect="pure")
    def greet(name: str) -> str:
        """Return a greeting for a claimed Run."""
        return f"hello {name}"

    with AgentRuntime.open(tmp_path / "runtime", sandbox="local") as runtime:
        task, run = runtime.workbench.create_task("greet", tmp_path)
        claimed = runtime.execution.claim_run(run.id)
        rowset = RowSet(ClaimStore(), [EffectRow(), HistoryRow(), CapabilityRow()])
        harness = ToolHarness(tools=ToolRegistry([greet]), rowset=rowset)
        proposal = _proposal(
            effect_id="leased-effect", kind="greeting",
            capabilities=frozenset(), risk="none",
        )
        observation = asyncio.run(runtime.execute_effect_for_run(
            run.id,
            proposal,
            harness=harness,
            target=ToolTarget(greet, {"name": "Ada"}),
            lease_token=claimed.lease_token,
        ))
        assert observation.status == "succeeded"
        events = runtime.execution.agent_events(run.id)
        assert [event.type for event in events] == ["effect_observed"]
        assert events[0].data["effect_id"] == "leased-effect"

        terminal = runtime.execution.complete_run(
            run.id, claimed.lease_token or "", result={"done": True},
        )
        assert terminal.state.value == "completed"
        with pytest.raises(ExecutionStateError, match="terminal Run"):
            asyncio.run(runtime.execute_effect_for_run(
                run.id,
                _proposal(effect_id="after-terminal", kind="greeting", capabilities=frozenset(), risk="none"),
                harness=harness,
                target=ToolTarget(greet, {"name": "Ada"}),
            ))


def test_extension_trust_revoke_and_rollback(tmp_path: Path):
    first = ExtensionManifest("demo", version="0.1.0", provenance="registry:test")
    second = ExtensionManifest("demo", version="0.2.0", provenance="registry:test")
    registry = ExtensionRegistry()
    registry.register(first, artifact=b"one", scenario_names=set(), skill_names=set())
    registry.register(second, artifact=b"two", scenario_names=set(), skill_names=set())
    assert registry.rollback("demo").manifest.version == "0.1.0"
    registry.revoke("demo")
    assert registry.get("demo") is None


def test_workspace_backup_restore_is_integrity_checked(tmp_path: Path):
    storage = WorkspaceStorage(tmp_path / "workspace")
    storage.require_current(create=True)
    backup = storage.backup(tmp_path / "backup.db")
    assert backup.backup_path.is_file()
    restored = storage.restore(backup.backup_path)
    assert restored.restored


def test_workspace_restore_without_existing_database_reports_no_pre_restore_copy(
    tmp_path: Path,
):
    source_storage = WorkspaceStorage(tmp_path / "source")
    source_storage.require_current(create=True)
    backup = source_storage.backup(tmp_path / "source-backup.db")
    assert backup.backup_path is not None

    target_storage = WorkspaceStorage(tmp_path / "fresh")
    restored = target_storage.restore(backup.backup_path, keep_backup=True)
    assert restored.restored
    assert restored.backup_path is None
    assert restored.database_path.is_file()


def test_workspace_restore_rejects_malformed_schema_version(tmp_path: Path):
    source_storage = WorkspaceStorage(tmp_path / "source")
    source_storage.require_current(create=True)
    backup = source_storage.backup(tmp_path / "malformed.db")
    assert backup.backup_path is not None
    import sqlite3

    with sqlite3.connect(backup.backup_path) as connection:
        connection.execute(
            "UPDATE runtime_meta SET value='not-an-int' WHERE key='schema_version'",
        )
        connection.commit()
    with pytest.raises(WorkspaceSchemaMismatch, match="schema version"):
        WorkspaceStorage(tmp_path / "target").restore(backup.backup_path)
