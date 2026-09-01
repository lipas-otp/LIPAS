"""Final local-first 1.0 productionization contracts."""
from __future__ import annotations

import asyncio
import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from lipas import (
    FileSecretResolver,
    ManagedSecretResolver,
    Agent,
    TLSConfig,
    LIPASRuntime,
    RunState,
    WorkflowStep,
    compile_workflow,
    execute_compiled_workflow,
    install_workspace,
    release_check,
    run_execution_soak,
    run_provider_workflow,
    upgrade_workspace,
)
from lipas.execution import ExecutionStore
from lipas.operator import LocalWebOperator
from lipas.dispatcher import RemoteWorkerHTTPServer, WorkerCapabilities
from tests.fake_adapter import FakeAdapter


def test_installation_is_idempotent_and_hardens_layout(tmp_path: Path):
    home = tmp_path / "state"
    first = install_workspace(home, sandbox="local")
    second = install_workspace(home, sandbox="local")
    assert first == second
    assert first.path.is_file()
    assert (home / "workspace.db").is_file()
    assert (home / "runs").is_dir()
    report = release_check(home)
    assert report.ready
    if os.name == "posix":
        assert (home.stat().st_mode & 0o077) == 0


def test_upgrade_refreshes_manifest_without_deleting_database(tmp_path: Path):
    home = tmp_path / "state"
    first = install_workspace(home)
    database = home / "workspace.db"
    before = database.read_bytes()
    upgraded = upgrade_workspace(home)
    assert upgraded.home == first.home
    assert database.read_bytes() == before
    assert release_check(home).ready


def test_upgrade_refreshes_manifest_after_package_version_change(tmp_path: Path):
    home = tmp_path / "state"
    first = install_workspace(home, sandbox="local")
    manifest_path = home / ".installation.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["package_version"] = "0.39.0"
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    assert not release_check(home).ready
    upgraded = upgrade_workspace(home, sandbox="local")
    assert upgraded.package_version == first.package_version
    assert release_check(home).ready


def test_release_check_fails_on_tampered_manifest_paths(tmp_path: Path):
    home = tmp_path / "state"
    install_workspace(home)
    manifest_path = home / ".installation.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["database"] = str(home / "other.db")
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    assert not release_check(home).ready


def test_file_secret_resolver_rotates_atomically_and_redacts(tmp_path: Path):
    resolver = FileSecretResolver(tmp_path / "secrets.json", allowed_names=("API_KEY",))
    reference = resolver.rotate("API_KEY", "secret-value")
    assert reference == "secret://file/API_KEY"
    assert resolver.resolve(reference) == "secret-value"
    assert resolver.redact({"value": "secret-value"}) == {"value": "[REDACTED SECRET]"}
    if os.name == "posix":
        assert (tmp_path / "secrets.json").stat().st_mode & 0o077 == 0
    with pytest.raises(Exception):
        resolver.rotate("OTHER", "value")


def test_file_secret_resolver_serializes_concurrent_rotations(tmp_path: Path):
    resolver = FileSecretResolver(
        tmp_path / "secrets.json",
        allowed_names=("A", "B", "C", "D"),
    )
    with ThreadPoolExecutor(max_workers=4) as workers:
        tuple(
            workers.map(
                lambda item: resolver.rotate(*item),
                ((name, f"value-{name}") for name in ("A", "B", "C", "D")),
            ),
        )
    assert all(
        resolver.resolve(f"secret://file/{name}") == f"value-{name}"
        for name in ("A", "B", "C", "D")
    )


def test_managed_secret_resolver_delegates_without_persisting_values():
    seen: list[str] = []

    def lookup(reference: str) -> str:
        seen.append(reference)
        return "managed-value"

    resolver = ManagedSecretResolver(
        lookup,
        redactor=lambda value: {
            key: str(item).replace("managed-value", "[REDACTED]")
            for key, item in value.items()
        } if isinstance(value, dict) else value,
        allowed_prefixes=("secret://vault/",),
    )
    assert resolver.resolve("secret://vault/API_KEY") == "managed-value"
    assert resolver.resolve_arguments(None, {"token": "secret://vault/API_KEY"}) == {
        "token": "managed-value",
    }
    assert resolver.redact({"token": "managed-value"}) == {"token": "[REDACTED]"}
    assert seen == ["secret://vault/API_KEY", "secret://vault/API_KEY"]
    with pytest.raises(Exception, match="managed secret"):
        resolver.resolve("secret://file/API_KEY")


def test_managed_secret_resolver_custom_namespace_and_default_redaction():
    resolver = ManagedSecretResolver(
        lambda reference: "managed-secret" if reference == "vault://prod/key" else "",
        allowed_prefixes=("vault://",),
    )
    assert resolver.resolve_arguments(None, {"token": "vault://prod/key"}) == {
        "token": "managed-secret",
    }
    assert resolver.redact({"token": "managed-secret"}) == {
        "token": "[REDACTED SECRET]",
    }
    cyclic: dict[str, object] = {}
    cyclic["self"] = cyclic
    with pytest.raises(Exception, match="cycle"):
        resolver.resolve_value(cyclic)


def test_openai_adapter_accepts_managed_api_key_reference():
    from lipas.adapter.openai_compatible import OpenAICompatibleAdapter
    resolver = ManagedSecretResolver(lambda reference: "provider-key")
    adapter = OpenAICompatibleAdapter(
        base_url="https://provider.example/v1",
        api_key_reference="secret://provider/key",
        secret_resolver=resolver,
    )
    assert adapter.api_key == "provider-key"
    with pytest.raises(Exception, match="either api_key"):
        OpenAICompatibleAdapter(
            base_url="https://provider.example/v1",
            api_key="inline",
            api_key_reference="secret://provider/key",
            secret_resolver=resolver,
        )

    current = {"value": "key-v1"}
    rotating = ManagedSecretResolver(lambda _reference: current["value"])
    rotated = OpenAICompatibleAdapter(
        base_url="https://provider.example/v1",
        api_key_reference="secret://provider/key",
        secret_resolver=rotating,
    )
    current["value"] = "key-v2"
    rotated.reload_api_key()
    assert rotated.api_key == "key-v2"


def test_local_execution_soak_reports_terminal_invariants(tmp_path: Path):
    database = tmp_path / "soak.db"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    with ExecutionStore(database) as execution:
        report = run_execution_soak(
            execution,
            iterations=5,
            workspace=workspace,
        )
        assert report.healthy
        assert report.executed_iterations == 5
        assert report.failed == 0
        assert report.p95_latency_ms >= 0


def test_tls_certificate_fingerprint_is_rotation_metadata(tmp_path: Path):
    cert = tmp_path / "cert.pem"
    key = tmp_path / "key.pem"
    cert.write_bytes(b"certificate-v1")
    key.write_bytes(b"private-key")
    if os.name == "posix":
        key.chmod(0o600)
    config = TLSConfig(cert, key)
    first = config.certificate_fingerprint()
    cert.write_bytes(b"certificate-v2")
    assert config.certificate_fingerprint() != first


def test_design_partner_signoff_rejects_symlink_evidence(tmp_path: Path):
    target = tmp_path / "evidence.json"
    target.write_text('{"accepted":true}\n', encoding="utf-8")
    link = tmp_path / "evidence-link.json"
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("symbolic links are unavailable")
    from lipas import DesignPartnerSignoff
    with pytest.raises(ValueError, match="symbolic link"):
        DesignPartnerSignoff.from_file("partner", "reviewer", "statement", link)


def test_provider_workflow_requires_explicit_live_and_records_terminal_run(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    with LIPASRuntime.open(tmp_path / "state", sandbox="local") as runtime:
        agent = Agent(
            adapter=FakeAdapter.echoing(),
            model="fake",
            session_path=tmp_path / "provider-session.db",
        )
        try:
            with pytest.raises(ValueError, match="live=True"):
                asyncio.run(run_provider_workflow(
                    agent, runtime.execution, "hello", workspace=workspace,
                ))
            evidence = asyncio.run(run_provider_workflow(
                agent,
                runtime.execution,
                "hello",
                workspace=workspace,
                live=True,
            ))
            assert evidence.success
            assert evidence.external
            assert runtime.execution.get_run(evidence.run_id).state is RunState.COMPLETED
        finally:
            agent.close()


def test_provider_workflow_rejects_raw_credentials_before_task_creation(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    with LIPASRuntime.open(tmp_path / "state", sandbox="local") as runtime:
        agent = Agent(
            adapter=FakeAdapter.echoing(),
            model="fake",
            session_path=tmp_path / "provider-session.db",
        )
        try:
            with pytest.raises(ValueError, match="raw secret|potential raw secret"):
                asyncio.run(run_provider_workflow(
                    agent,
                    runtime.execution,
                    "please use sk-provider-secret-value",
                    workspace=workspace,
                    live=True,
                ))
            assert runtime.execution.list_tasks() == ()
        finally:
            agent.close()


def test_compiled_workflow_executes_in_dependency_order_and_bounds_failure():
    workflow = compile_workflow(
        "ship",
        fixed_steps=(
            WorkflowStep("verify", "verify", "fixed", depends_on=("build",)),
            WorkflowStep("build", "build", "fixed"),
        ),
        adaptive_steps=1,
        max_adaptive_steps=1,
    )
    seen: list[str] = []

    async def run(step: WorkflowStep, context: dict[str, object]):
        seen.append(step.step_id)
        return {"step": step.step_id, "prior": sorted(context["outputs"])}

    result = asyncio.run(execute_compiled_workflow(workflow, run))
    assert result.succeeded
    assert seen == ["build", "verify", "adaptive-1"]
    assert [item.status for item in result.steps] == ["succeeded"] * 3

    def fail(step: WorkflowStep, _context: dict[str, object]):
        if step.step_id == "build":
            raise RuntimeError("build failed")
        return {"ok": True}

    failed = asyncio.run(execute_compiled_workflow(workflow, fail))
    assert failed.status == "failed"
    assert failed.steps[0].status == "failed"
    assert all(item.status == "skipped" for item in failed.steps[1:])


def test_runtime_executes_workflow_as_one_durable_run(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    with LIPASRuntime.open(tmp_path / "state", sandbox="local") as runtime:
        workflow = runtime.compile_workflow(
            "inspect project",
            workspace=project,
            fixed_steps=({"step_id": "inspect", "goal": "inspect"},),
            adaptive_steps=0,
        )
        result = asyncio.run(
            runtime.execute_workflow(
                workflow,
                lambda step, _context: {"step": step.step_id},
            ),
        )
        assert result.succeeded
        task = next(
            item for item in runtime.execution.list_tasks()
            if item.goal == "inspect project"
        )
        run = runtime.execution.list_runs(task_id=task.id)[0]
        assert run.state is RunState.COMPLETED
        assert runtime.execution.agent_events(run.id)[0].type == "workflow_step_started"


def test_network_bindings_fail_closed_without_tls(tmp_path: Path):
    with ExecutionStore(tmp_path / "execution.db") as execution:
        operator = LocalWebOperator(execution, require_authentication=True)
        with pytest.raises(ValueError, match="require TLS"):
            operator.make_server(host="0.0.0.0")

        class Worker:
            capabilities = WorkerCapabilities("worker")

            async def execute(self, _task, _lease):
                return {"status": "ok"}

        with pytest.raises(ValueError, match="require TLS"):
            RemoteWorkerHTTPServer(
                ("0.0.0.0", 0), Worker(), attestation_secret="0123456789abcdef",
            )


def test_operator_exposes_metrics_incidents_and_cost_projections(tmp_path: Path):
    with LIPASRuntime.open(tmp_path / "state", sandbox="local") as runtime:
        task, run = runtime.workbench.create_task("observe", tmp_path)
        claimed = runtime.execution.claim_run(run.id)
        runtime.execution.complete_run(
            run.id, claimed.lease_token or "", result={"usage": {"tokens": 3}},
        )
        operator = runtime.operator()
        metrics = operator._get(("api", "metrics"), {})
        assert metrics["metrics"]["completed"] == 1
        assert metrics["slo"]["healthy"] is True
        assert operator._get(("api", "incidents"), {})["incidents"] == []
        assert operator._get(("api", "cost"), {})["cost"]["totals"] == {"tokens": 3.0}
