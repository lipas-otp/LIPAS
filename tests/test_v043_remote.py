"""0.43 Hybrid execution transport and request-identity contracts."""
from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from lipas import (
    RemoteExecutionResult,
    RemoteWorkerHTTPClient,
    RemoteWorkerHTTPServer,
    RemoteWorkerLease,
    RemoteWorkerEvent,
    WorkerCapabilities,
)
from lipas.execution import Task, TaskState


def test_remote_attempt_deduplicates_concurrent_retries_and_allows_heartbeat_refresh(
    tmp_path: Path,
):
    worker_capabilities = WorkerCapabilities("  worker-a  ", capabilities=frozenset({"code"}))
    calls = 0
    calls_lock = threading.Lock()

    class Worker:
        capabilities = worker_capabilities

        async def execute(self, task, lease):
            nonlocal calls
            await asyncio.sleep(0.05)
            with calls_lock:
                calls += 1
            return RemoteExecutionResult(result={"ok": True})

    try:
        server = RemoteWorkerHTTPServer(
            ("127.0.0.1", 0), Worker(), attestation_secret="0123456789abcdef",
        )
    except PermissionError:
        pytest.skip("loopback sockets are restricted in this environment")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        task = Task("task", "run", str(tmp_path), TaskState.OPEN, 1.0, 1.0)
        lease = RemoteWorkerLease(
            "run", task.id, "worker-a", 1, "lease-token", 9_999_999_999.0,
        )
        client = RemoteWorkerHTTPClient(
            f"http://127.0.0.1:{server.server_port}", worker_capabilities,
            attestation_secret="0123456789abcdef", allow_http=True,
        )

        def call():
            return asyncio.run(client.execute(task, lease))

        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _item: call(), range(4)))
        assert all(result == results[0] for result in results)
        assert calls == 1

        # A heartbeat changes expiry but not the logical attempt identity.
        refreshed = RemoteWorkerLease(
            lease.run_id, lease.task_id, lease.worker_id, lease.attempt,
            lease.lease_token, 9_999_999_998.0,
        )
        assert asyncio.run(client.execute(task, refreshed)) == results[0]
        assert calls == 1
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_remote_request_identity_rejects_stable_payload_tampering(tmp_path: Path):
    worker_capabilities = WorkerCapabilities("worker-b")

    class Worker:
        capabilities = worker_capabilities

        async def execute(self, task, lease):
            return {"ok": True}

    try:
        server = RemoteWorkerHTTPServer(
            ("127.0.0.1", 0), Worker(), attestation_secret="0123456789abcdef",
        )
    except PermissionError:
        pytest.skip("loopback sockets are restricted in this environment")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        task = Task("task", "run", str(tmp_path), TaskState.OPEN, 1.0, 1.0)
        lease = RemoteWorkerLease(
            "run", task.id, "worker-b", 1, "lease-token", 9_999_999_999.0,
        )
        client = RemoteWorkerHTTPClient(
            f"http://127.0.0.1:{server.server_port}", worker_capabilities,
            attestation_secret="0123456789abcdef", allow_http=True,
        )
        assert asyncio.run(client.execute(task, lease)) == {"ok": True}
        tampered = RemoteWorkerLease(
            lease.run_id, lease.task_id, lease.worker_id, lease.attempt,
            "different-token", lease.expires_at,
        )
        with pytest.raises(RuntimeError, match="403"):
            asyncio.run(client.execute(task, tampered))
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_remote_contracts_normalize_and_reject_ambiguous_values():
    with pytest.raises(ValueError, match="duplicate"):
        WorkerCapabilities("worker", capabilities=frozenset({"read", " read"}))
    event = RemoteWorkerEvent(" event ", " phase ", {"ok": True})
    assert event.identity == "event" and event.type == "phase"
    with pytest.raises(ValueError, match="strict JSON"):
        RemoteWorkerEvent("event", "phase", {"cost": float("nan")})
    from lipas import RemoteEffectObservation

    with pytest.raises(ValueError, match="status"):
        RemoteEffectObservation("effect", [])
