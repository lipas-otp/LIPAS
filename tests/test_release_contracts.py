"""Independent release-gate contract tests for remote, policy, connector,
orchestration, observability, and extension boundaries."""
from __future__ import annotations

import asyncio
import threading
from pathlib import Path

import pytest

from lipas import (
    AgentRuntime,
    AgentPlan,
    ApprovalDelegation,
    ExecutionStore,
    EffectProposal,
    ExtensionManifest,
    ExtensionRegistry,
    ExtensionRegistryService,
    ExtensionSigner,
    ExtensionTrustPolicy,
    PlanStep,
    RemoteWorkerRunner,
    SLOReport,
    WorkerCapabilities,
    WorkspaceIdentity,
    measure_execution,
    WorkspacePolicyStore,
    RemoteCheckpoint,
    RemoteEffectObservation,
    RemoteExecutionResult,
    RemoteWorkerEvent,
    RemoteWorkerHTTPClient,
    RemoteWorkerHTTPServer,
    ExternalRunEnvelope,
    ConnectorRegistry,
    ConnectorSpec,
    RateLimitExceeded,
    RateLimitPolicy,
    CostLedger,
    EvaluationCase,
    evaluate_execution,
    project_cost_ledger,
    project_incidents,
    ExecutionStateError,
    DesignPartnerCase,
    DesignPartnerSignoff,
    run_design_partner_validation,
    OperatorAuthenticator,
)


def test_remote_worker_lease_is_fenced_and_redacted(tmp_path: Path):
    database = tmp_path / "worker.db"
    with ExecutionStore(database) as execution:
        task = execution.create_task("remote", tmp_path)
        run = execution.create_run(task.id)

    class Worker:
        capabilities = WorkerCapabilities("worker-a", capabilities=frozenset({"code"}))

        async def execute(self, task, lease):
            assert task.id == lease.task_id
            return {"ok": True}

    result = asyncio.run(RemoteWorkerRunner(database, Worker()).run(run.id))
    assert result["result"] == {"ok": True}
    assert "lease_token" not in result["lease"]
    with ExecutionStore(database) as execution:
        current = execution.get_run(run.id)
        assert current is not None and current.state.value == "completed"
        task2 = execution.create_task("remote again", tmp_path)
        run2 = execution.create_run(task2.id)
    runner = RemoteWorkerRunner(database, Worker())
    lease = runner.claim(run2.id)
    renewed = runner.heartbeat(lease)
    assert renewed.attempt == lease.attempt
    runner.complete(renewed, {"ok": True})


def test_remote_worker_capability_attestation_is_fail_closed(tmp_path: Path):
    database = tmp_path / "capability-worker.db"
    with ExecutionStore(database) as execution:
        task = execution.create_task("remote", tmp_path)
        run = execution.create_run(task.id)

    class Worker:
        capabilities = WorkerCapabilities("limited", capabilities=frozenset({"read"}))

        async def execute(self, task, lease):
            return {"ok": True}

    runner = RemoteWorkerRunner(database, Worker(), required_capabilities=frozenset({"write"}))
    with pytest.raises(ExecutionStateError, match="missing required capabilities"):
        runner.claim(run.id)


def test_worker_attestation_rejects_tamper_and_capability_drift():
    capabilities = WorkerCapabilities("attested", capabilities=frozenset({"read"}))
    attestation = capabilities.attest("attestation-secret-0123")
    assert attestation.verify(capabilities, "attestation-secret-0123")
    assert not attestation.verify(
        WorkerCapabilities("attested", capabilities=frozenset({"write"})),
        "attestation-secret-0123",
    )
    assert not type(attestation).from_mapping({**attestation.as_dict(), "signature": "0" * 64}).verify(
        capabilities, "attestation-secret-0123",
    )


def test_remote_worker_http_transport_round_trip_with_attestation(tmp_path: Path):
    worker_capabilities = WorkerCapabilities("http-worker", capabilities=frozenset({"code"}))
    calls = 0

    class Remote:
        capabilities = worker_capabilities

        async def execute(self, task, lease):
            nonlocal calls
            calls += 1
            return RemoteExecutionResult(
                result={"ok": True, "task": task.id},
                events=(RemoteWorkerEvent("remote-1", "remote_done", {"fence": lease.fence}),),
            )

    try:
        server = RemoteWorkerHTTPServer(("127.0.0.1", 0), Remote(), attestation_secret="0123456789abcdef")
    except PermissionError:
        pytest.skip("loopback sockets are restricted in this environment")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        # Use the canonical dataclass shapes; no ExecutionStore is needed for
        # a transport round-trip because the runner remains the authority.
        from lipas.execution import Task, TaskState
        task = Task("task-http", "remote", str(tmp_path), TaskState.OPEN, 1.0, 1.0)
        from lipas.dispatcher import RemoteWorkerLease
        lease = RemoteWorkerLease("run-http", task.id, "http-worker", 1, "lease-token", 9_999_999_999.0)
        client = RemoteWorkerHTTPClient(
            f"http://127.0.0.1:{server.server_port}", worker_capabilities,
            attestation_secret="0123456789abcdef", allow_http=True,
        )
        result = asyncio.run(client.execute(task, lease))
        assert isinstance(result, RemoteExecutionResult)
        assert result.result["ok"] is True
        assert result.events[0].data["fence"] == "run-http:1"
        replay = asyncio.run(client.execute(task, lease))
        assert replay == result
        assert calls == 1
        from lipas.execution import TaskState
        changed_task = Task(task.id, "tampered", task.workspace, TaskState.OPEN, task.created_at, task.updated_at)
        with pytest.raises(RuntimeError, match="403"):
            asyncio.run(client.execute(changed_task, lease))
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_remote_worker_run_renews_long_execution_lease(tmp_path: Path):
    database = tmp_path / "long-worker.db"
    with ExecutionStore(database) as execution:
        task = execution.create_task("long remote", tmp_path)
        run = execution.create_run(task.id)

    class SlowWorker:
        capabilities = WorkerCapabilities("slow-worker")

        async def execute(self, task, lease):
            await asyncio.sleep(0.12)
            return {"task": task.id, "fence": lease.fence}

    result = asyncio.run(RemoteWorkerRunner(
        database,
        SlowWorker(),
        lease_seconds=0.04,
        heartbeat_interval_s=0.01,
    ).run(run.id))
    assert result["run"]["state"] == "completed"


def test_remote_worker_does_not_execute_after_cancel_request(tmp_path: Path):
    database = tmp_path / "cancelled-worker.db"
    with ExecutionStore(database) as execution:
        task = execution.create_task("cancel remote", tmp_path)
        run = execution.create_run(task.id)
        execution.claim_run(run.id, lease_seconds=1, now=100)
        execution.request_cancel(run.id, now=102)

    calls: list[str] = []

    class CancelledWorker:
        capabilities = WorkerCapabilities("cancelled-worker")

        async def execute(self, task, lease):
            calls.append(task.id)
            return {"unexpected": True}

    result = asyncio.run(RemoteWorkerRunner(database, CancelledWorker()).run(run.id))
    assert result["run"]["state"] == "cancelled"
    assert calls == []


def test_shared_workspace_identity_delegation_and_plan_handoff(tmp_path: Path):
    grantor = WorkspaceIdentity("owner", "Owner", scopes=frozenset({"approve:*"}))
    delegate = WorkspaceIdentity("reviewer", "Reviewer", scopes=frozenset({"approve:email"}))
    delegation = ApprovalDelegation(
        "grant-1", grantor, delegate, frozenset({"approve:email"}), expires_at=9999999999,
    )
    assert delegation.allows("approve:email", now=1)
    plan = AgentPlan(
        "plan-1", "chat-1",
        (PlanStep("step-1", "send", "mailer", required_capabilities=frozenset({"email"})),),
    )
    handoff = plan.handoff("step-1", sender="user", payload={"subject": "draft"})
    assert handoff.id.startswith("plan_handoff_")
    assert handoff.metadata["conversation_id"] == "chat-1"
    assert plan.as_dict()["steps"][0]["step_id"] == "step-1"
    assert plan.fingerprint == AgentPlan(
        "plan-1", "chat-1",
        (PlanStep("step-1", "send", "mailer", required_capabilities=frozenset({"email"})),),
    ).fingerprint


def test_execution_metrics_and_slo_projection(tmp_path: Path):
    with ExecutionStore(tmp_path / "metrics.db") as execution:
        task = execution.create_task("done", tmp_path)
        run = execution.create_run(task.id)
        claimed = execution.claim_run(run.id)
        execution.complete_run(run.id, claimed.lease_token or "", result={"ok": True})
        metrics, slo = measure_execution(execution, target_success_rate=1.0)
    assert metrics.completed == 1
    assert metrics.success_rate == 1.0
    assert isinstance(slo, SLOReport) and slo.healthy


def test_empty_execution_window_is_not_reported_healthy(tmp_path: Path):
    with ExecutionStore(tmp_path / "empty-metrics.db") as execution:
        metrics, slo = measure_execution(execution, target_success_rate=0.0)
    assert metrics.runs == 0
    assert slo.terminal_count == 0
    assert not slo.healthy


def test_extension_registry_requires_artifact_provenance():
    manifest = ExtensionManifest("certified", provenance="registry:test")
    registry = ExtensionRegistry()
    record = registry.register(manifest, artifact=b"package", scenario_names=set(), skill_names=set())
    assert record.certified
    assert registry.get("certified") == record


def test_extension_signature_binds_manifest_and_artifact():
    signer = ExtensionSigner("release", "signing-secret-012345")
    unsigned = ExtensionManifest("signed", provenance="registry:test")
    signed = signer.sign(unsigned, artifact=b"package")
    registry = ExtensionRegistry(
        trust_policy=ExtensionTrustPolicy(
            allowed_provenance=frozenset({"registry:test"}),
            trusted_signers=frozenset({"release"}),
            require_signature=True,
            signer_secrets={"release": "signing-secret-012345"},
        ),
    )
    assert registry.register(signed, artifact=b"package", scenario_names=set(), skill_names=set()).certified
    with pytest.raises(ValueError, match="artifact|signature"):
        registry.register(signed, artifact=b"tampered", scenario_names=set(), skill_names=set())
    tampered_manifest = ExtensionManifest.from_mapping(
        {**signed.as_dict(), "version": "0.1.1"},
    )
    with pytest.raises(ValueError, match="signature"):
        registry.register(tampered_manifest, artifact=b"package", scenario_names=set(), skill_names=set())


def test_extension_registry_service_requires_mutation_authentication():
    with pytest.raises(ValueError, match="at least 16"):
        ExtensionRegistryService(("127.0.0.1", 0), ExtensionRegistry(), auth_token="short")


def test_operator_authenticator_rejects_expired_and_tampered_tokens():
    authenticator = OperatorAuthenticator("operator-secret-012345", ttl_s=10)
    token = authenticator.issue("alice", now=100)
    assert authenticator.verify(token, now=109) == "alice"
    assert authenticator.verify(token, now=110) is None
    assert authenticator.verify("%%%.___") is None
    encoded, signature = token.rsplit(".", 1)
    assert authenticator.verify(encoded + "." + ("0" if signature[0] != "0" else "1") + signature[1:], now=100) is None


def test_design_partner_validation_is_explicitly_fixture_scoped():
    cases = [
        DesignPartnerCase("repo", "Repository maintenance", "apply a safe patch"),
        DesignPartnerCase("mail", "Email delivery", "draft, approve, send, reconcile"),
    ]
    report = run_design_partner_validation(
        "fixture-partner",
        cases,
        lambda case: {
            "run_id": "run-" + case.case_id,
            "success": True,
            "unsafe_delivery": False,
            "operator_accepted": True,
            "reconciliation_seconds": 0.1 if case.case_id == "mail" else None,
        },
    )
    assert report.passed
    assert report.evidence_scope == "local_fixture"
    assert report.external_partner_evidence_required


def test_design_partner_external_signoff_requires_digest_verified_artifact(tmp_path: Path):
    evidence_path = tmp_path / "partner-signoff.json"
    evidence_path.write_text('{"accepted":true}\n', encoding="utf-8")
    report = run_design_partner_validation(
        "external-partner",
        (DesignPartnerCase("case", "Case", "exercise"),),
        lambda _case: {
            "run_id": "run-case",
            "success": True,
            "unsafe_delivery": False,
            "operator_accepted": True,
        },
        evidence_scope="external_adapter",
    )
    signoff = DesignPartnerSignoff.from_file(
        "external-partner", "partner-reviewer", "statement-1", evidence_path,
    )
    accepted = report.with_signoff(signoff)
    assert accepted.externally_accepted
    evidence_path.write_text('{"accepted":false}\n', encoding="utf-8")
    assert not accepted.externally_accepted


def test_agent_runtime_admits_effects_without_executing_them(tmp_path: Path):
    with AgentRuntime.open(tmp_path / "runtime", sandbox="local") as runtime:
        proposal = EffectProposal(
            "effect-1", "email_send", "agent", frozenset({"email.send"}),
            estimate={"calls": 1}, risk="external_write",
        )
        denied = runtime.decide_effect(
            proposal, available_capabilities={"email.send"},
            budget_remaining={"calls": 1},
        )
        assert not denied.allowed and denied.reason == "approval_required"
        allowed = runtime.decide_effect(
            proposal, available_capabilities={"email.send"},
            budget_remaining={"calls": 1}, approved=True,
        )
        assert allowed.allowed


def test_remote_structured_result_persists_events_checkpoint_and_effect(tmp_path: Path):
    database = tmp_path / "structured-worker.db"
    with ExecutionStore(database) as execution:
        task = execution.create_task("remote", tmp_path)
        run = execution.create_run(task.id)

    class Worker:
        capabilities = WorkerCapabilities("structured-worker")

        async def execute(self, task, lease):
            return RemoteExecutionResult(
                result={"usage": {"tokens": 3}, "provider": "fake"},
                events=(RemoteWorkerEvent("phase-1", "phase", {"ok": True}),),
                checkpoint=RemoteCheckpoint(0, "remote_done", {"cursor": 1}),
                effects=(RemoteEffectObservation("effect-1", "succeeded", {"provider": "p"}),),
            )

    result = asyncio.run(RemoteWorkerRunner(database, Worker()).run(run.id))
    assert result["result"]["usage"]["tokens"] == 3
    with ExecutionStore(database) as execution:
        events = execution.agent_events(run.id)
        assert {event.type for event in events} >= {"phase", "effect_observed"}
        checkpoint = execution.get_checkpoint(run.id)
        assert checkpoint is not None and checkpoint.phase == "remote_done"


def test_workspace_policy_store_persists_delegation_and_revoke(tmp_path: Path):
    path = tmp_path / "policy.db"
    owner = WorkspaceIdentity("owner", "Owner", scopes=frozenset({"approve:*"}))
    reviewer = WorkspaceIdentity("reviewer", "Reviewer")
    grant = ApprovalDelegation("grant", owner, reviewer, frozenset({"approve:email"}), expires_at=999)
    with WorkspacePolicyStore(path) as store:
        store.put_delegation(grant)
        assert store.put_identity(owner) == owner
        current = store.get_delegation("grant")
        assert current is not None and current.allows("approve:email", now=1)
        store.revoke_delegation("grant", actor_id="owner")
        assert store.get_delegation("grant") is None
        assert any(item["kind"] == "delegation_revoked" for item in store.audit())


def test_connector_rate_limit_and_registry_are_explicit():
    policy = RateLimitPolicy(1, window_s=100)
    policy.acquire(now=1)
    with pytest.raises(RateLimitExceeded):
        policy.acquire(now=1)
    registry = ConnectorRegistry()
    spec = ConnectorSpec("email", capabilities=frozenset({"email.send"}), supports_reconciliation=True)
    registry.register(spec, object())
    assert registry.spec("email") == spec


def test_external_run_boundary_and_observability_projections(tmp_path: Path):
    async def adapter(envelope, context):
        return {"usage": {"tokens": 2}, "provider": envelope.provider}

    with AgentRuntime.open(tmp_path / "runtime", sandbox="local") as runtime:
        coordinator = runtime.coordinator()
        envelope = ExternalRunEnvelope("langgraph", "ext-1", "external", str(runtime.home), {"x": 1})
        result = asyncio.run(coordinator.execute_external(envelope, adapter))
        assert result.status == "completed"
        ledger = project_cost_ledger(runtime.execution, price_per_unit={"tokens": 0.5})
        assert isinstance(ledger, CostLedger)
        report = evaluate_execution(runtime.execution, [EvaluationCase("case", result.run_id)])
        assert report.passed == 1
        assert project_incidents(runtime.execution) == ()


def test_external_run_retry_reuses_terminal_event_and_result(tmp_path: Path):
    calls = 0

    async def adapter(envelope, context):
        nonlocal calls
        calls += 1
        return {"ok": True}

    with AgentRuntime.open(tmp_path / "runtime", sandbox="local") as runtime:
        coordinator = runtime.coordinator()
        envelope = ExternalRunEnvelope(
            "autogen", "ext-retry", "external", str(runtime.home),
        )
        first = asyncio.run(coordinator.execute_external(envelope, adapter))
        second = asyncio.run(coordinator.execute_external(envelope, adapter))
        assert first == second
        assert calls == 1
        started = [
            event for event in runtime.execution.agent_events(first.run_id)
            if event.type == "handoff_started"
        ]
        assert len(started) == 1


def test_policy_and_connector_numeric_boundaries_fail_closed(tmp_path: Path):
    from lipas.coordination_policy import SharedBudgetPolicy
    from lipas.http_client import HttpClient, RateLimitPolicy

    owner = WorkspaceIdentity("owner", "Owner", scopes=frozenset({"approve:*"}))
    delegate = WorkspaceIdentity("delegate", "Delegate")
    with pytest.raises(ValueError, match="finite"):
        ApprovalDelegation(
            "huge", owner, delegate, frozenset({"approve:x"}), expires_at=10**1000,
        )
    with pytest.raises(ValueError, match="finite"):
        SharedBudgetPolicy({"handoffs": 10**1000})
    with pytest.raises(ValueError, match="finite"):
        RateLimitPolicy(1, window_s=10**1000)
    with pytest.raises(ValueError, match="finite"):
        HttpClient(base_url="https://provider.test", timeout_s=10**1000)
    with ExecutionStore(tmp_path / "coordination-boundary.db") as execution:
        from lipas.coordination import AgentCoordinator

        with pytest.raises(ValueError, match="finite"):
            AgentCoordinator(
                execution,
                workspace=tmp_path,
                lease_seconds=10**1000,
            )


def test_workbench_close_is_idempotent_and_rejects_late_access(tmp_path: Path):
    from lipas import Workbench

    workbench = Workbench(tmp_path / "workbench")
    workbench.close()
    workbench.close()
    with pytest.raises(RuntimeError, match="closed"):
        workbench.list_tasks()
