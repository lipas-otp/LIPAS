"""Persistent local Task dispatcher for the first-party workbench product."""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import json
import math
import socket
import ssl
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Protocol, cast
from urllib.error import HTTPError, URLError
from urllib.request import (
    HTTPRedirectHandler,
    HTTPSHandler,
    Request as URLRequest,
    build_opener,
)
from urllib.parse import urlsplit
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .execution import (
    ExecutionLeaseError,
    ExecutionStateError,
    ExecutionStore,
    Run,
    Task,
    TaskState,
)
from .security import TLSConfig

__all__ = [
    "DispatchOutcome", "HybridWorker",
    "RemoteWorkerEvent", "RemoteCheckpoint", "RemoteEffectObservation",
    "RemoteExecutionResult", "RemoteWorkerLease", "RemoteWorkerRunner",
    "WorkerCapabilities", "WorkerAttestation", "RemoteWorkerHTTPClient",
    "RemoteWorkerHTTPServer",
    "TaskDispatcher", "TaskExecutor",
]


TaskExecutor = Callable[[Task, Run], Awaitable[None]]
OutcomeSink = Callable[["DispatchOutcome"], None]


def _is_finite_number(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(float(value))
    except (OverflowError, TypeError, ValueError):
        return False


def _remote_host_is_non_loopback(host: str) -> bool:
    normalized = str(host).strip().lower().strip("[]")
    if normalized in {"localhost", "ip6-localhost"}:
        return False
    try:
        import ipaddress
        return not ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return True


@dataclass(frozen=True, slots=True)
class WorkerCapabilities:
    """An explicit, host-verifiable declaration for a remote execution site."""

    worker_id: str
    version: str = "1"
    capabilities: frozenset[str] = field(default_factory=frozenset)
    endpoint: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.worker_id, str) or not self.worker_id.strip():
            raise ValueError("worker_id must be a non-empty string")
        if not isinstance(self.version, str) or not self.version.strip():
            raise ValueError("worker version must be a non-empty string")
        if not isinstance(self.capabilities, frozenset):
            raise TypeError("capabilities must be a frozenset")
        if any(not isinstance(item, str) or not item.strip() for item in self.capabilities):
            raise ValueError("capabilities must contain non-empty strings")
        normalized_capabilities = frozenset(item.strip() for item in self.capabilities)
        if len(normalized_capabilities) != len(self.capabilities):
            raise ValueError("capabilities contain duplicate values after normalization")
        if self.endpoint is not None and (
            not isinstance(self.endpoint, str) or not self.endpoint.strip()
        ):
            raise ValueError("endpoint must be a non-empty string or None")
        if not isinstance(self.metadata, Mapping):
            raise TypeError("worker metadata must be a mapping")
        object.__setattr__(self, "capabilities", frozenset(self.capabilities))
        # Capabilities cross a transport/attestation boundary.  A shallow
        # ``json.dumps`` check would coerce integer mapping keys and would
        # leave nested caller-owned objects live while the request is being
        # signed.  Take the same detached, strict-JSON snapshot used by all
        # other remote payloads.
        object.__setattr__(
            self,
            "metadata",
            _strict_json_copy(dict(self.metadata), "worker metadata"),
        )
        object.__setattr__(self, "worker_id", self.worker_id.strip())
        object.__setattr__(self, "version", self.version.strip())
        object.__setattr__(self, "capabilities", normalized_capabilities)
        if self.endpoint is not None:
            object.__setattr__(self, "endpoint", self.endpoint.strip())

    def as_dict(self) -> dict[str, Any]:
        return {
            "worker_id": self.worker_id,
            "version": self.version,
            "capabilities": sorted(self.capabilities),
            "endpoint": self.endpoint,
            "metadata": dict(self.metadata),
        }

    @property
    def fingerprint(self) -> str:
        """Stable digest covered by a worker attestation."""
        payload = json.dumps(
            self.as_dict(),
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
        return hashlib.sha256(payload).hexdigest()

    def attest(self, secret: bytes | str) -> "WorkerAttestation":
        return WorkerAttestation.sign(self, secret)


@dataclass(frozen=True, slots=True)
class WorkerAttestation:
    """HMAC-SHA256 proof that a transport payload belongs to a worker contract."""

    worker_id: str
    fingerprint: str
    signature: str
    algorithm: str = "hmac-sha256"

    def __post_init__(self) -> None:
        if not all(isinstance(value, str) and value.strip() for value in (
            self.worker_id, self.fingerprint, self.signature, self.algorithm,
        )):
            raise ValueError("worker attestation fields must be non-empty")
        if self.algorithm != "hmac-sha256":
            raise ValueError("unsupported worker attestation algorithm")
        if (
            len(self.fingerprint) != 64 or len(self.signature) != 64
            or any(char not in "0123456789abcdef" for char in self.fingerprint)
            or any(char not in "0123456789abcdef" for char in self.signature)
        ):
            raise ValueError("worker attestation digests must be SHA-256 hex")

    @classmethod
    def sign(cls, capabilities: WorkerCapabilities, secret: bytes | str) -> "WorkerAttestation":
        key = _attestation_secret(secret)
        signature = hmac.new(
            key, f"{capabilities.worker_id}:{capabilities.fingerprint}".encode(), hashlib.sha256,
        ).hexdigest()
        return cls(capabilities.worker_id, capabilities.fingerprint, signature)

    def verify(self, capabilities: WorkerCapabilities, secret: bytes | str) -> bool:
        expected = WorkerAttestation.sign(capabilities, secret)
        return (
            self.worker_id == expected.worker_id
            and self.fingerprint == expected.fingerprint
            and hmac.compare_digest(self.signature, expected.signature)
        )

    def as_dict(self) -> dict[str, str]:
        return {
            "worker_id": self.worker_id,
            "fingerprint": self.fingerprint,
            "signature": self.signature,
            "algorithm": self.algorithm,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "WorkerAttestation":
        if not isinstance(value, Mapping):
            raise TypeError("worker attestation must be an object")
        return cls(
            value.get("worker_id", ""), value.get("fingerprint", ""),
            value.get("signature", ""), value.get("algorithm", "hmac-sha256"),
        )


@dataclass(frozen=True, slots=True)
class RemoteWorkerLease:
    """A fenced execution lease; the attempt is the fencing generation."""

    run_id: str
    task_id: str
    worker_id: str
    attempt: int
    lease_token: str = field(repr=False)
    expires_at: float
    cancel_requested: bool = False

    def __post_init__(self) -> None:
        for name in ("run_id", "task_id", "worker_id", "lease_token"):
            if not isinstance(getattr(self, name), str) or not getattr(self, name).strip():
                raise ValueError(f"{name} must be a non-empty string")
        if isinstance(self.attempt, bool) or not isinstance(self.attempt, int) or self.attempt < 1:
            raise ValueError("attempt must be a positive int")
        if not _is_finite_number(self.expires_at):
            raise ValueError("expires_at must be finite")
        if not isinstance(self.cancel_requested, bool):
            raise TypeError("cancel_requested must be bool")
        object.__setattr__(self, "run_id", self.run_id.strip())
        object.__setattr__(self, "task_id", self.task_id.strip())
        object.__setattr__(self, "worker_id", self.worker_id.strip())
        object.__setattr__(self, "lease_token", self.lease_token.strip())

    @property
    def fence(self) -> str:
        return f"{self.run_id}:{self.attempt}"

    def as_dict(self) -> dict[str, Any]:
        # Lease tokens are transport secrets and never belong in events or UI.
        return {
            "run_id": self.run_id,
            "task_id": self.task_id,
            "worker_id": self.worker_id,
            "attempt": self.attempt,
            "fence": self.fence,
            "expires_at": self.expires_at,
            "cancel_requested": self.cancel_requested,
        }


@dataclass(frozen=True, slots=True)
class RemoteWorkerEvent:
    """Provider-neutral event returned by a remote execution transport."""

    identity: str
    type: str
    data: Mapping[str, Any] = field(default_factory=dict)
    iteration: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.identity, str) or not self.identity.strip():
            raise ValueError("remote event identity must be non-empty")
        if not isinstance(self.type, str) or not self.type.strip():
            raise ValueError("remote event type must be non-empty")
        if any(char in self.type for char in "\r\n"):
            raise ValueError("remote event type must not contain CR/LF")
        if isinstance(self.iteration, bool) or not isinstance(self.iteration, int) or self.iteration < 0:
            raise ValueError("remote event iteration must be a non-negative int")
        if not isinstance(self.data, Mapping):
            raise TypeError("remote event data must be a mapping")
        object.__setattr__(self, "identity", self.identity.strip())
        object.__setattr__(self, "type", self.type.strip())
        object.__setattr__(self, "data", _strict_json_copy(dict(self.data), "remote event data"))


@dataclass(frozen=True, slots=True)
class RemoteCheckpoint:
    """A checkpoint produced by a worker for the leased Run."""

    expected_version: int
    phase: str
    state: Mapping[str, Any]

    def __post_init__(self) -> None:
        if isinstance(self.expected_version, bool) or not isinstance(self.expected_version, int) or self.expected_version < 0:
            raise ValueError("checkpoint expected_version must be non-negative")
        if not isinstance(self.phase, str) or not self.phase.strip():
            raise ValueError("checkpoint phase must be non-empty")
        if not isinstance(self.state, Mapping):
            raise TypeError("checkpoint state must be a mapping")
        object.__setattr__(self, "phase", self.phase.strip())
        object.__setattr__(self, "state", _strict_json_copy(dict(self.state), "checkpoint state"))


@dataclass(frozen=True, slots=True)
class RemoteEffectObservation:
    """An effect outcome reported by a worker, never an authority grant."""

    effect_id: str
    status: str
    evidence: Mapping[str, Any] = field(default_factory=dict)
    result: Any = None
    claim_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.effect_id, str) or not self.effect_id.strip():
            raise ValueError("remote effect_id must be non-empty")
        if not isinstance(self.status, str) or self.status.strip() not in {
            "succeeded", "failed", "uncertain", "rejected",
        }:
            raise ValueError("remote effect status is invalid")
        if not isinstance(self.evidence, Mapping):
            raise TypeError("remote effect evidence must be a mapping")
        if self.claim_id is not None and (not isinstance(self.claim_id, str) or not self.claim_id.strip()):
            raise ValueError("remote effect claim_id must be non-empty or None")
        object.__setattr__(self, "effect_id", self.effect_id.strip())
        object.__setattr__(self, "status", self.status.strip())
        object.__setattr__(self, "evidence", _strict_json_copy(dict(self.evidence), "remote effect evidence"))
        object.__setattr__(self, "result", _strict_json_copy(self.result, "remote effect result"))


@dataclass(frozen=True, slots=True)
class RemoteExecutionResult:
    """Structured remote result with replayable events and evidence hooks.

    A worker may return this value over any provider-neutral channel; the
    local runner persists each part through the existing Run authority before
    completion. The reference HTTP transport uses this shape on the wire.
    """

    result: Any = None
    events: tuple[RemoteWorkerEvent, ...] = ()
    checkpoint: RemoteCheckpoint | None = None
    effects: tuple[RemoteEffectObservation, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.events, tuple) or any(not isinstance(item, RemoteWorkerEvent) for item in self.events):
            raise TypeError("remote events must be a tuple of RemoteWorkerEvent")
        if self.checkpoint is not None and not isinstance(self.checkpoint, RemoteCheckpoint):
            raise TypeError("checkpoint must be RemoteCheckpoint or None")
        if not isinstance(self.effects, tuple) or any(not isinstance(item, RemoteEffectObservation) for item in self.effects):
            raise TypeError("remote effects must be a tuple of RemoteEffectObservation")
        object.__setattr__(self, "result", _strict_json_copy(self.result, "remote result"))


class HybridWorker(Protocol):
    """Minimal remote-worker boundary; network transport remains host-owned."""

    capabilities: WorkerCapabilities

    async def execute(self, task: Task, lease: RemoteWorkerLease) -> Any: ...


class _NoRedirectHandler(HTTPRedirectHandler):
    """Reject redirects rather than forwarding lease-bearing requests."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


class RemoteWorkerHTTPClient:
    """A real HTTP transport implementing the provider-neutral worker protocol.

    The local runner remains lease authority. This client only forwards a
    fenced lease to a remote service over HTTP and returns a structured
    `RemoteExecutionResult`; it never claims or settles the Run itself.
    """

    def __init__(
        self,
        endpoint: str,
        capabilities: WorkerCapabilities,
        *,
        attestation_secret: bytes | str,
        timeout_s: float = 30.0,
        allow_http: bool = False,
        tls: TLSConfig | ssl.SSLContext | None = None,
    ) -> None:
        if not isinstance(endpoint, str) or not endpoint.strip():
            raise ValueError("remote worker endpoint must be non-empty")
        endpoint = endpoint.strip()
        parsed_endpoint = urlsplit(endpoint)
        scheme = parsed_endpoint.scheme.lower()
        if scheme not in {"https", "http"}:
            raise ValueError("remote worker endpoint must be HTTP(S)")
        if scheme == "http" and not allow_http:
            raise ValueError("remote worker transport requires HTTPS")
        if (
            parsed_endpoint.query or parsed_endpoint.fragment
            or parsed_endpoint.username or parsed_endpoint.password
            or not parsed_endpoint.hostname
        ):
            raise ValueError("remote worker endpoint must not contain query or credentials")
        if not isinstance(capabilities, WorkerCapabilities):
            raise TypeError("capabilities must be WorkerCapabilities")
        _attestation_secret(attestation_secret)
        if not _is_finite_number(timeout_s) or timeout_s <= 0:
            raise ValueError("timeout_s must be finite and positive")
        # Preserve the parsed scheme spelling only in the URL itself; all
        # policy decisions above are case-insensitive and fail closed.
        self.endpoint = endpoint.rstrip("/") + "/v1/execute"
        self._scheme = scheme
        self.capabilities = capabilities
        self._secret = attestation_secret
        self.timeout_s = float(timeout_s)
        self._tls_lock = threading.RLock()
        if tls is not None and scheme != "https":
            raise ValueError("TLS client context requires an HTTPS endpoint")
        if tls is None:
            self._tls_context = None
        elif isinstance(tls, TLSConfig):
            self._tls_context = tls.client_context()
        elif isinstance(tls, ssl.SSLContext):
            self._tls_context = tls
        else:
            raise TypeError("tls must be TLSConfig, ssl.SSLContext, or None")

    def reload_tls(self, tls: TLSConfig | ssl.SSLContext) -> None:
        """Replace the trust context used by future HTTPS requests.

        Requests already handed to ``urllib`` retain their context; the
        replacement is deliberately limited to subsequent connections.  This
        lets an operator rotate a CA/client certificate without changing the
        endpoint or interrupting an in-flight worker attempt.
        """
        if self._scheme != "https":
            raise ValueError("TLS client context requires an HTTPS endpoint")
        if isinstance(tls, TLSConfig):
            context = tls.client_context()
        elif isinstance(tls, ssl.SSLContext):
            context = tls
        else:
            raise TypeError("tls must be TLSConfig or ssl.SSLContext")
        with self._tls_lock:
            self._tls_context = context

    async def execute(self, task: Task, lease: RemoteWorkerLease) -> Any:
        payload = {
            # One logical lease attempt has one transport identity.  A client
            # retry after a lost response can therefore be deduplicated by a
            # reference server instead of invoking the worker twice.
            "request_id": f"{lease.run_id}:{lease.attempt}",
            "task": {
                "id": task.id, "goal": task.goal, "workspace": task.workspace,
                "state": task.state.value, "created_at": task.created_at,
                "updated_at": task.updated_at,
            },
            "lease": {
                "run_id": lease.run_id, "task_id": lease.task_id,
                "worker_id": lease.worker_id, "attempt": lease.attempt,
                "lease_token": lease.lease_token, "expires_at": lease.expires_at,
                "cancel_requested": lease.cancel_requested,
            },
            "attestation": self.capabilities.attest(self._secret).as_dict(),
        }
        response = await asyncio.to_thread(self._post, payload)
        if not isinstance(response, Mapping):
            raise RuntimeError("remote worker response must be an object")
        if response.get("structured") is True:
            return _remote_result_from_mapping(response)
        return response.get("result")

    def _post(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        try:
            body = json.dumps(
                payload,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError, RecursionError) as exc:
            raise RuntimeError("remote worker request must be strict JSON") from exc
        request = URLRequest(
            self.endpoint, data=body,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        try:
            # Never follow redirects for a request carrying a lease token.
            # A redirect could silently move an HTTPS payload to another host
            # (or downgrade it to HTTP) while preserving the bearer secret.
            handlers: list[Any] = [_NoRedirectHandler()]
            with self._tls_lock:
                tls_context = self._tls_context
            if tls_context is not None:
                handlers.append(HTTPSHandler(context=tls_context))
            opener = build_opener(*handlers)
            with opener.open(request, timeout=self.timeout_s) as response:  # noqa: S310 - endpoint is explicit and HTTPS-gated
                raw = response.read(10 * 1024 * 1024 + 1)
                if len(raw) > 10 * 1024 * 1024:
                    raise RuntimeError("remote worker response exceeds 10 MiB")
        except (HTTPError, URLError, TimeoutError) as exc:
            raise RuntimeError(f"remote worker transport failed: {exc}") from exc
        try:
            value = json.loads(
                raw.decode("utf-8"),
                parse_constant=lambda raw: (_ for _ in ()).throw(
                    ValueError(f"non-JSON numeric constant {raw!r}")
                ),
            )
        except ValueError as exc:
            raise RuntimeError("remote worker response is not JSON") from exc
        if not isinstance(value, Mapping):
            raise RuntimeError("remote worker response must be an object")
        return value


class RemoteWorkerHTTPServer(ThreadingHTTPServer):
    """Reference HTTP service for a `HybridWorker` implementation."""

    tls_enabled: bool = False
    tls_context: ssl.SSLContext | None = None
    _tls_lock: Any = None

    def get_request(self) -> tuple[socket.socket, Any]:  # noqa: D401 - stdlib hook
        """Accept one worker request using the context current at accept time."""
        request, client_address = super().get_request()
        lock = getattr(self, "_tls_lock", None)
        if lock is None:
            context = self.tls_context
        else:
            with lock:
                context = self.tls_context
        if context is None:
            return request, client_address
        try:
            return context.wrap_socket(request, server_side=True), client_address
        except BaseException:
            request.close()
            raise

    def reload_tls(self, tls: TLSConfig | ssl.SSLContext) -> None:
        """Switch certificates for future worker connections without rebinding."""
        if isinstance(tls, TLSConfig):
            context = tls.server_context()
        elif isinstance(tls, ssl.SSLContext):
            context = tls
        else:
            raise TypeError("tls must be TLSConfig or ssl.SSLContext")
        lock = getattr(self, "_tls_lock", None)
        if lock is None:
            self.tls_context = context
            self.tls_enabled = True
        else:
            with lock:
                self.tls_context = context
                self.tls_enabled = True

    def __init__(
        self,
        address: tuple[str, int],
        worker: HybridWorker,
        *,
        attestation_secret: bytes | str,
        tls: TLSConfig | ssl.SSLContext | None = None,
        allow_insecure: bool = False,
    ) -> None:
        if not isinstance(worker.capabilities, WorkerCapabilities):
            raise TypeError("worker.capabilities must be WorkerCapabilities")
        if not isinstance(allow_insecure, bool):
            raise TypeError("allow_insecure must be bool")
        host = address[0]
        if _remote_host_is_non_loopback(host) and tls is None and not allow_insecure:
            raise ValueError("non-loopback remote worker binds require TLS")
        self.worker = worker
        self.attestation_secret = _attestation_secret(attestation_secret)
        if tls is None:
            context = None
        elif isinstance(tls, TLSConfig):
            context = tls.server_context()
        elif isinstance(tls, ssl.SSLContext):
            context = tls
        else:
            raise TypeError("tls must be TLSConfig, ssl.SSLContext, or None")
        self._cache_lock = threading.Lock()
        self._tls_lock = threading.RLock()
        self._max_response_cache = 1024
        self._max_response_tombstones = 4096
        self._response_cache: dict[str, dict[str, Any]] = {}
        self._response_fingerprints: dict[str, str] = {}
        # Evicting a completed response must never make a retried request
        # executable again: the remote handler may already have performed an
        # external side effect.  Tombstones retain the identity of evicted
        # attempts and force the caller to obtain a fresh fenced attempt.
        self._response_tombstones: dict[str, str] = {}
        self._inflight: dict[str, threading.Event] = {}
        self._inflight_fingerprints: dict[str, str] = {}
        super().__init__(address, _RemoteWorkerHTTPHandler)
        self.tls_enabled = context is not None
        self.tls_context = context


class _RemoteWorkerHTTPHandler(BaseHTTPRequestHandler):
    server: RemoteWorkerHTTPServer

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/v1/execute":
            self._send(404, {"error": "not_found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "-1"))
            if length < 1 or length > 10 * 1024 * 1024:
                raise ValueError("invalid Content-Length")
            raw = self.rfile.read(length)
            payload = json.loads(
                raw.decode("utf-8"),
                parse_constant=lambda raw: (_ for _ in ()).throw(
                    ValueError(f"non-JSON numeric constant {raw!r}")
                ),
            )
            if not isinstance(payload, Mapping):
                raise ValueError("request must be an object")
            task_data = payload.get("task")
            lease_data = payload.get("lease")
            request_id = payload.get("request_id")
            if not isinstance(request_id, str) or not request_id.strip():
                raise ValueError("request_id is required")
            attestation = WorkerAttestation.from_mapping(payload.get("attestation", {}))
            if not isinstance(task_data, Mapping) or not isinstance(lease_data, Mapping):
                raise ValueError("task and lease are required")
            if not attestation.verify(self.server.worker.capabilities, self.server.attestation_secret):
                raise PermissionError("worker attestation failed")
            if lease_data.get("worker_id") != self.server.worker.capabilities.worker_id:
                raise PermissionError("worker id does not match attestation")
            task_id = task_data.get("id")
            task_goal = task_data.get("goal")
            task_workspace = task_data.get("workspace")
            task_state = task_data.get("state")
            task_created = task_data.get("created_at")
            task_updated = task_data.get("updated_at")
            if (
                not isinstance(task_id, str) or not task_id.strip()
                or not isinstance(task_goal, str) or not task_goal.strip()
                or not isinstance(task_workspace, str) or not task_workspace.strip()
                or not isinstance(task_state, str)
                or isinstance(task_created, bool)
                or not isinstance(task_created, (int, float))
                or not _is_finite_number(task_created)
                or isinstance(task_updated, bool)
                or not isinstance(task_updated, (int, float))
                or not _is_finite_number(task_updated)
            ):
                raise ValueError("task payload has invalid fields")
            task = Task(
                task_id.strip(), task_goal, task_workspace,
                TaskState(task_state),
                float(task_created), float(task_updated),
            )
            lease_run_id = lease_data.get("run_id")
            lease_task_id = lease_data.get("task_id")
            lease_worker_id = lease_data.get("worker_id")
            lease_attempt = lease_data.get("attempt")
            lease_token = lease_data.get("lease_token")
            lease_expires = lease_data.get("expires_at")
            lease_cancel = lease_data.get("cancel_requested", False)
            if (
                not isinstance(lease_run_id, str) or not lease_run_id.strip()
                or not isinstance(lease_task_id, str) or not lease_task_id.strip()
                or not isinstance(lease_worker_id, str) or not lease_worker_id.strip()
                or not isinstance(lease_token, str) or not lease_token.strip()
                or isinstance(lease_attempt, bool) or not isinstance(lease_attempt, int)
                or isinstance(lease_expires, bool)
                or not isinstance(lease_expires, (int, float))
                or not _is_finite_number(lease_expires)
                or not isinstance(lease_cancel, bool)
            ):
                raise ValueError("lease payload has invalid fields")
            lease = RemoteWorkerLease(
                lease_run_id, lease_task_id, lease_worker_id, lease_attempt,
                lease_token, float(lease_expires), lease_cancel,
            )
            if lease.task_id != task.id:
                raise PermissionError("lease task does not match task payload")
            expected_request_id = f"{lease.run_id}:{lease.attempt}"
            if request_id != expected_request_id:
                raise PermissionError("request id does not match lease fence")
            # ``expires_at`` and ``cancel_requested`` are mutable transport
            # observations.  They must not change the identity of a logical
            # run attempt when a caller retries after a heartbeat.  Stable
            # lease identity (including the secret token) remains bound.
            stable_lease = {
                "run_id": lease_data.get("run_id"),
                "task_id": lease_data.get("task_id"),
                "worker_id": lease_data.get("worker_id"),
                "attempt": lease_data.get("attempt"),
                "lease_token": lease_data.get("lease_token"),
            }
            request_fingerprint = hashlib.sha256(
                json.dumps(
                    {"task": dict(task_data), "lease": stable_lease},
                    sort_keys=True, separators=(",", ":"), allow_nan=False,
                ).encode("utf-8"),
            ).hexdigest()
            with self.server._cache_lock:
                cached = self.server._response_cache.get(request_id)
                if cached is not None and self.server._response_fingerprints.get(request_id) != request_fingerprint:
                    raise PermissionError("request id was reused with different payload")
                if cached is None and request_id in self.server._response_tombstones:
                    if self.server._response_tombstones[request_id] != request_fingerprint:
                        raise PermissionError("request id was reused with different payload")
                    raise RuntimeError(
                        "remote response is no longer replayable; obtain a new lease attempt",
                    )
                event = self.server._inflight.get(request_id)
                owner = cached is None and event is None
                if owner:
                    event = threading.Event()
                    self.server._inflight[request_id] = event
                    self.server._inflight_fingerprints[request_id] = request_fingerprint
                elif event is not None and self.server._inflight_fingerprints.get(request_id) != request_fingerprint:
                    raise PermissionError("request id was reused with different payload")
            if cached is not None:
                # A completed response is safe to replay after the original
                # lease expiry: a reclaimed Run receives a new attempt and
                # therefore a different request identity.  This closes the
                # lost-response window without allowing stale execution.
                self._send(200, cached)
                return
            if not owner:
                assert event is not None
                if not event.wait(timeout=30.0):
                    raise RuntimeError("duplicate remote request is still in flight")
                with self.server._cache_lock:
                    cached = self.server._response_cache.get(request_id)
                if cached is None:
                    raise RuntimeError("remote request owner did not publish a result")
                self._send(200, cached)
                return
            if lease.cancel_requested:
                with self.server._cache_lock:
                    self.server._inflight.pop(request_id, None)
                    self.server._inflight_fingerprints.pop(request_id, None)
                raise PermissionError("remote worker lease has been cancelled")
            if lease.expires_at <= time.time():
                with self.server._cache_lock:
                    self.server._inflight.pop(request_id, None)
                    self.server._inflight_fingerprints.pop(request_id, None)
                raise PermissionError("remote worker lease has expired")
            try:
                result = asyncio.run(self.server.worker.execute(task, lease))
                # A provider may outlive the fenced attempt.  Do not publish
                # a success that a host can no longer safely associate with
                # this lease; the tombstone in the exception path forces an
                # explicit fresh attempt/reconciliation instead.
                if lease.expires_at <= time.time():
                    raise PermissionError("remote worker lease expired during execution")
                encoded_result = _remote_result_mapping(result)
                with self.server._cache_lock:
                    if len(self.server._response_cache) >= self.server._max_response_cache:
                        evicted = next(iter(self.server._response_cache))
                        self.server._response_cache.pop(evicted)
                        evicted_fingerprint = self.server._response_fingerprints.pop(
                            evicted, None,
                        )
                        if evicted_fingerprint is not None:
                            self.server._response_tombstones[evicted] = evicted_fingerprint
                            while (
                                len(self.server._response_tombstones)
                                > self.server._max_response_tombstones
                            ):
                                self.server._response_tombstones.pop(
                                    next(iter(self.server._response_tombstones)),
                                )
                    self.server._response_cache[request_id] = encoded_result
                    self.server._response_fingerprints[request_id] = request_fingerprint
                self._send(200, encoded_result)
            except BaseException:
                # A worker may have performed an external action before its
                # result failed validation (or before it raised).  Never make
                # that attempt executable again under the same fence: retain
                # a tombstone so callers must obtain a fresh lease attempt and
                # reconcile the world explicitly.
                with self.server._cache_lock:
                    self.server._response_tombstones[request_id] = request_fingerprint
                    while (
                        len(self.server._response_tombstones)
                        > self.server._max_response_tombstones
                    ):
                        self.server._response_tombstones.pop(
                            next(iter(self.server._response_tombstones)),
                        )
                raise
            finally:
                with self.server._cache_lock:
                    completed = self.server._inflight.pop(request_id, None)
                    self.server._inflight_fingerprints.pop(request_id, None)
                    if completed is not None:
                        completed.set()
        except PermissionError as exc:
            self._send(403, {"error": str(exc)})
        except asyncio.CancelledError as exc:  # pragma: no cover - defensive transport boundary
            self._send(499, {"error": type(exc).__name__, "message": str(exc)})
        except Exception as exc:  # pragma: no cover - defensive transport boundary
            self._send(400, {"error": type(exc).__name__, "message": str(exc)})

    def log_message(self, _format: str, *_args: Any) -> None:
        return

    def _send(self, status: int, payload: Mapping[str, Any]) -> None:
        encoded = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
        if len(encoded) > 10 * 1024 * 1024:
            status = 413
            encoded = b'{"error":"remote worker response exceeds 10 MiB"}'
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        if self.server.tls_enabled:
            self.send_header("Strict-Transport-Security", "max-age=31536000")
        self.end_headers()
        self.wfile.write(encoded)


def _attestation_secret(value: bytes | str) -> bytes:
    if isinstance(value, str):
        value = value.encode("utf-8")
    if not isinstance(value, bytes) or len(value) < 16:
        raise ValueError("attestation_secret must contain at least 16 bytes")
    return value


def _strict_json_copy(value: Any, name: str) -> Any:
    """Return detached transport data while rejecting NaN and live objects."""
    try:
        _validate_json_shape(value, name)
    except (TypeError, ValueError, RecursionError) as exc:
        raise ValueError(f"{name} must be strict JSON") from exc
    try:
        encoded = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError, RecursionError) as exc:
        raise ValueError(f"{name} must be strict JSON") from exc
    return json.loads(encoded)


def _validate_json_shape(value: Any, path: str, *, _active: set[int] | None = None) -> None:
    if value is None or isinstance(value, (bool, int, str)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{path} must contain finite numbers")
        return
    if _active is None:
        _active = set()
    if isinstance(value, (list, tuple, Mapping)):
        identity = id(value)
        if identity in _active:
            raise ValueError(f"{path} must not contain reference cycles")
        _active.add(identity)
        try:
            if isinstance(value, Mapping):
                for key, item in value.items():
                    if not isinstance(key, str):
                        raise ValueError(f"{path} must use string object keys")
                    _validate_json_shape(item, f"{path}.{key}", _active=_active)
            else:
                for index, item in enumerate(value):
                    _validate_json_shape(item, f"{path}[{index}]", _active=_active)
        finally:
            _active.remove(identity)
        return
    raise TypeError(f"{path} contains unsupported {type(value).__name__}")


def _remote_result_mapping(result: Any) -> dict[str, Any]:
    if not isinstance(result, RemoteExecutionResult):
        payload = {"structured": False, "result": result}
    else:
        payload = {
            "structured": True,
            "result": result.result,
            "events": [
                {
                    "identity": item.identity,
                    "type": item.type,
                    "data": dict(item.data),
                    "iteration": item.iteration,
                }
                for item in result.events
            ],
            "checkpoint": None if result.checkpoint is None else {
                "expected_version": result.checkpoint.expected_version,
                "phase": result.checkpoint.phase,
                "state": dict(result.checkpoint.state),
            },
            "effects": [
                {
                    "effect_id": item.effect_id,
                    "status": item.status,
                    "evidence": dict(item.evidence),
                    "result": item.result,
                    "claim_id": item.claim_id,
                }
                for item in result.effects
            ],
        }
    # Validate the wire shape here rather than relying on whichever HTTP
    # server/client happens to serialize it.  This keeps NaN, cyclic values,
    # and live Python objects from crossing the transport boundary.
    return _strict_json_copy(payload, "remote worker result")


def _remote_result_from_mapping(value: Mapping[str, Any]) -> RemoteExecutionResult:
    if not isinstance(value, Mapping):
        raise TypeError("remote worker result must be an object")
    value = _strict_json_copy(dict(value), "remote worker result")
    if not isinstance(value, Mapping):
        raise TypeError("remote worker result must be an object")
    raw_events = value.get("events", ())
    if not isinstance(raw_events, (list, tuple)):
        raise ValueError("remote worker events must be an array")
    events = tuple(
        RemoteWorkerEvent(
            cast(str, item.get("identity")),
            cast(str, item.get("type")),
            item.get("data", {}),
            item.get("iteration", 0),
        )
        for item in raw_events
        if isinstance(item, Mapping)
    )
    if len(events) != len(raw_events):
        raise ValueError("remote worker events must contain objects")
    checkpoint_data = value.get("checkpoint")
    if checkpoint_data is not None and not isinstance(checkpoint_data, Mapping):
        raise ValueError("remote worker checkpoint must be an object or null")
    checkpoint = None if checkpoint_data is None else RemoteCheckpoint(
        cast(int, checkpoint_data.get("expected_version")),
        cast(str, checkpoint_data.get("phase")),
        cast(Mapping[str, Any], checkpoint_data.get("state")),
    )
    raw_effects = value.get("effects", ())
    if not isinstance(raw_effects, (list, tuple)):
        raise ValueError("remote worker effects must be an array")
    effects = tuple(
        RemoteEffectObservation(
            cast(str, item.get("effect_id")),
            cast(str, item.get("status")),
            item.get("evidence", {}),
            item.get("result"),
            item.get("claim_id"),
        )
        for item in raw_effects
        if isinstance(item, Mapping)
    )
    if len(effects) != len(raw_effects):
        raise ValueError("remote worker effects must contain objects")
    return RemoteExecutionResult(value.get("result"), events, checkpoint, effects)


class _LeaseManager:
    """Small adapter that binds remote worker calls to ExecutionStore fencing."""

    def __init__(self, execution_path: str | Path, capabilities: WorkerCapabilities):
        self.execution_path = Path(execution_path).expanduser().resolve()
        self.capabilities = capabilities

    def claim(self, run_id: str, *, lease_seconds: float = 60.0) -> RemoteWorkerLease:
        with ExecutionStore(self.execution_path) as execution:
            run = execution.claim_run(run_id, lease_seconds=lease_seconds)
        assert run.lease_token is not None and run.lease_expires is not None
        return RemoteWorkerLease(
            run.id, run.task_id, self.capabilities.worker_id, run.attempt,
            run.lease_token, run.lease_expires, run.cancel_requested,
        )

    def heartbeat(self, lease: RemoteWorkerLease, *, lease_seconds: float = 60.0) -> RemoteWorkerLease:
        with ExecutionStore(self.execution_path) as execution:
            run = execution.renew_lease(
                lease.run_id, lease.lease_token, lease_seconds=lease_seconds,
            )
        assert run.lease_token is not None and run.lease_expires is not None
        if run.attempt != lease.attempt or run.task_id != lease.task_id:
            raise ExecutionLeaseError("worker fence changed while heartbeating")
        return RemoteWorkerLease(
            run.id, run.task_id, lease.worker_id, run.attempt,
            run.lease_token, run.lease_expires, run.cancel_requested,
        )

    def recoverable(self) -> tuple[Run, ...]:
        with ExecutionStore(self.execution_path) as execution:
            return execution.list_claimable_runs()


@dataclass
class RemoteWorkerRunner:
    """Run one host-supplied worker with lease/heartbeat/fencing semantics."""

    execution_path: str | Path
    worker: HybridWorker
    lease_seconds: float = 60.0
    heartbeat_interval_s: float | None = None
    required_capabilities: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        if (
            isinstance(self.lease_seconds, bool)
            or not isinstance(self.lease_seconds, (int, float))
            or not _is_finite_number(self.lease_seconds)
            or self.lease_seconds <= 0
        ):
            raise ValueError("lease_seconds must be a positive finite number")
        self.lease_seconds = float(self.lease_seconds)
        if self.heartbeat_interval_s is None:
            self.heartbeat_interval_s = self.lease_seconds / 3
        if (
            isinstance(self.heartbeat_interval_s, bool)
            or not isinstance(self.heartbeat_interval_s, (int, float))
            or not _is_finite_number(self.heartbeat_interval_s)
            or self.heartbeat_interval_s <= 0
            or self.heartbeat_interval_s >= self.lease_seconds
        ):
            raise ValueError(
                "heartbeat_interval_s must be positive and shorter than lease_seconds",
            )
        self.heartbeat_interval_s = float(self.heartbeat_interval_s)
        self.execution_path = Path(self.execution_path).expanduser().resolve()
        if not isinstance(self.required_capabilities, frozenset):
            raise TypeError("required_capabilities must be a frozenset")
        if any(not isinstance(item, str) or not item.strip() for item in self.required_capabilities):
            raise ValueError("required_capabilities must contain non-empty strings")
        normalized_required = frozenset(item.strip() for item in self.required_capabilities)
        if len(normalized_required) != len(self.required_capabilities):
            raise ValueError(
                "required_capabilities contain duplicate values after normalization",
            )
        self.required_capabilities = normalized_required

    def claim(self, run_id: str) -> RemoteWorkerLease:
        if not isinstance(self.worker.capabilities, WorkerCapabilities):
            raise TypeError("worker.capabilities must be WorkerCapabilities")
        missing = self.required_capabilities - self.worker.capabilities.capabilities
        if missing:
            raise ExecutionStateError(
                "worker is missing required capabilities: " + ", ".join(sorted(missing)),
            )
        manager = _LeaseManager(self.execution_path, self.worker.capabilities)
        return manager.claim(run_id, lease_seconds=self.lease_seconds)

    def heartbeat(self, lease: RemoteWorkerLease) -> RemoteWorkerLease:
        if lease.worker_id != self.worker.capabilities.worker_id:
            raise ExecutionLeaseError("lease belongs to another worker")
        manager = _LeaseManager(self.execution_path, self.worker.capabilities)
        return manager.heartbeat(lease, lease_seconds=self.lease_seconds)

    async def execute(self, lease: RemoteWorkerLease) -> Any:
        if lease.worker_id != self.worker.capabilities.worker_id:
            raise ExecutionLeaseError("lease belongs to another worker")
        with ExecutionStore(Path(self.execution_path).expanduser().resolve()) as execution:
            task = execution.get_task(lease.task_id)
        if task is None:
            raise KeyError(lease.task_id)
        return await self.worker.execute(task, lease)

    async def run(self, run_id: str) -> Any:
        lease = self.claim(run_id)
        # A cancelled pending/expired Run can still win the SQL claim race.
        # Settle that cancellation before invoking remote code; otherwise a
        # worker could perform a new external effect after the operator has
        # already requested cancellation.
        with ExecutionStore(self.execution_path) as execution:
            current = execution.get_run(lease.run_id)
            if current is None:
                raise KeyError(lease.run_id)
            if current.cancel_requested:
                cancelled = execution.finish_cancelled(
                    lease.run_id, lease.lease_token,
                )
                return {
                    "result": None,
                    "lease": lease.as_dict(),
                    "run": {"id": cancelled.id, "state": cancelled.state.value},
                }
        current_lease = lease
        heartbeat_error: list[BaseException] = []
        interval = self.heartbeat_interval_s
        assert interval is not None

        async def heartbeat_loop() -> None:
            nonlocal current_lease
            try:
                while True:
                    await asyncio.sleep(float(interval))
                    current_lease = self.heartbeat(current_lease)
                    if current_lease.cancel_requested:
                        heartbeat_error.append(
                            ExecutionLeaseError(
                                f"run {current_lease.run_id!r} cancellation requested",
                            ),
                        )
                        return
            except asyncio.CancelledError:
                raise
            except BaseException as exc:
                heartbeat_error.append(exc)

        heartbeat_task = asyncio.create_task(heartbeat_loop())
        try:
            result = await self.execute(current_lease)
        except asyncio.CancelledError:
            # Cancellation does not prove that the remote provider stopped;
            # leave the lease to expire and let the recovery policy reconcile.
            raise
        except Exception as exc:
            if heartbeat_error:
                raise ExecutionLeaseError(
                    f"worker {self.worker.capabilities.worker_id!r} lost its lease",
                ) from heartbeat_error[0]
            self.fail(
                current_lease,
                {"type": type(exc).__name__, "message": str(exc)},
            )
            raise
        finally:
            heartbeat_task.cancel()
            await asyncio.gather(heartbeat_task, return_exceptions=True)
        if heartbeat_error:
            raise ExecutionLeaseError(
                f"worker {self.worker.capabilities.worker_id!r} lost its lease",
            ) from heartbeat_error[0]
        # Persist structured worker output before the terminal transition. A
        # process killed after this point can safely replay the idempotent
        # event identities; a checkpoint conflict leaves the Run recoverable
        # instead of silently discarding the worker's progress.
        result_value = result
        if isinstance(result, RemoteExecutionResult):
            try:
                self._persist_remote_result(current_lease, result)
            except Exception as exc:
                self.fail(
                    current_lease,
                    {"type": type(exc).__name__, "message": str(exc), "recovery_required": True},
                )
                raise
            result_value = result.result
        completed = self.complete(current_lease, result_value)
        return {
            "result": result_value,
            "lease": current_lease.as_dict(),
            "run": {"id": completed.id, "state": completed.state.value},
        }

    def _persist_remote_result(
        self,
        lease: RemoteWorkerLease,
        payload: RemoteExecutionResult,
    ) -> None:
        with ExecutionStore(self.execution_path) as execution:
            for event in payload.events:
                execution.append_agent_event(
                    lease.run_id,
                    event.type,
                    # The event identity belongs to the logical external
                    # action, not to a particular worker attempt. A
                    # redelivery on a different worker must replay the same
                    # event rather than create a second history branch.
                    identity=f"remote:{event.identity}",
                    iteration=event.iteration,
                    data=dict(event.data),
                )
            if payload.checkpoint is not None:
                execution.save_checkpoint(
                    lease.run_id,
                    lease.lease_token,
                    expected_version=payload.checkpoint.expected_version,
                    phase=payload.checkpoint.phase,
                    state=payload.checkpoint.state,
                )
            for observation in payload.effects:
                execution.append_agent_event(
                    lease.run_id,
                    "effect_observed",
                    identity=f"remote-effect:{observation.effect_id}",
                    data={
                        "effect_id": observation.effect_id,
                        "status": observation.status,
                        "claim_id": observation.claim_id,
                        "result": observation.result,
                        "evidence": dict(observation.evidence),
                    },
                )

    def complete(self, lease: RemoteWorkerLease, result: Any) -> Run:
        """Commit a remote result; an expired/fenced lease is rejected."""
        if lease.worker_id != self.worker.capabilities.worker_id:
            raise ExecutionLeaseError("lease belongs to another worker")
        with ExecutionStore(Path(self.execution_path).expanduser().resolve()) as execution:
            return execution.complete_run(lease.run_id, lease.lease_token, result=result)

    def fail(self, lease: RemoteWorkerLease, error: Mapping[str, Any]) -> Run:
        """Persist a worker failure through the same fenced transition."""
        if lease.worker_id != self.worker.capabilities.worker_id:
            raise ExecutionLeaseError("lease belongs to another worker")
        if not isinstance(error, Mapping):
            raise TypeError("worker error must be a mapping")
        with ExecutionStore(Path(self.execution_path).expanduser().resolve()) as execution:
            return execution.fail_run(lease.run_id, lease.lease_token, error=dict(error))


@dataclass(frozen=True, slots=True)
class DispatchOutcome:
    task_id: str
    run_id: str
    status: str
    attempt: int
    error_type: str | None = None


@dataclass
class TaskDispatcher:
    """Discover claimable Runs and execute them with bounded concurrency.

    The dispatcher owns no second queue. ``ExecutionStore`` remains the source
    of truth: pending Runs and expired running leases are discoverable, while
    ``claim_run`` inside the executor is the atomic ownership boundary.
    Waiting approvals consume no dispatcher slot after the executor returns.
    """

    execution_path: str | Path
    executor: TaskExecutor
    max_concurrency: int = 2
    lease_seconds: float = 60.0
    poll_interval_s: float = 1.0
    retry_delay_s: float = 5.0
    outcome_sink: OutcomeSink | None = None
    _active: dict[str, asyncio.Task[DispatchOutcome]] = field(
        default_factory=dict, init=False, repr=False,
    )
    _retry_after: dict[str, float] = field(
        default_factory=dict, init=False, repr=False,
    )
    _store: ExecutionStore | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        if (
            isinstance(self.max_concurrency, bool)
            or not isinstance(self.max_concurrency, int)
            or self.max_concurrency < 1
        ):
            raise ValueError("max_concurrency must be a positive integer")
        self.poll_interval_s = self._positive_seconds(
            self.poll_interval_s, "poll_interval_s",
        )
        self.lease_seconds = self._positive_seconds(
            self.lease_seconds, "lease_seconds",
        )
        self.retry_delay_s = self._positive_seconds(
            self.retry_delay_s, "retry_delay_s",
        )
        self.execution_path = Path(self.execution_path).expanduser().resolve()

    async def run_until_idle(self) -> tuple[DispatchOutcome, ...]:
        """Run every candidate visible in this invocation, then return.

        A worker/setup error is reported once rather than creating a tight
        retry loop. A later invocation can retry that still-pending Run.
        """
        with self._store_scope():
            outcomes: list[DispatchOutcome] = []
            attempted: set[str] = set()
            while True:
                candidates = [
                    run for run in self._claimable_runs()
                    if run.id not in attempted
                ]
                if not candidates:
                    return tuple(outcomes)
                batch = candidates[:self.max_concurrency]
                attempted.update(run.id for run in batch)
                completed = await asyncio.gather(*(
                    self._execute(run) for run in batch
                ))
                outcomes.extend(completed)
                for outcome in completed:
                    self._emit(outcome)

    async def serve(self, stop: asyncio.Event | None = None) -> None:
        """Continuously dispatch work until cancelled or ``stop`` is set."""
        stop = stop or asyncio.Event()
        with self._store_scope():
            try:
                while not stop.is_set():
                    self._reap_finished()
                    self._fill_slots()
                    await self._wait_for_progress(stop)
            finally:
                for task in self._active.values():
                    task.cancel()
                if self._active:
                    await asyncio.gather(
                        *self._active.values(), return_exceptions=True,
                    )
                self._active.clear()

    def _claimable_runs(self) -> tuple[Run, ...]:
        with self._store_call() as store:
            return store.list_claimable_runs()

    def _fill_slots(self) -> None:
        slots = self.max_concurrency - len(self._active)
        if slots <= 0:
            return
        now = time.monotonic()
        candidates = (
            run for run in self._claimable_runs()
            if run.id not in self._active
            and self._retry_after.get(run.id, 0.0) <= now
        )
        for run in candidates:
            self._active[run.id] = asyncio.create_task(self._execute(run))
            slots -= 1
            if slots == 0:
                break

    def _reap_finished(self) -> None:
        for run_id, task in tuple(self._active.items()):
            if not task.done():
                continue
            del self._active[run_id]
            outcome = task.result()
            if outcome.status == "worker_error":
                self._retry_after[run_id] = (
                    time.monotonic() + self.retry_delay_s
                )
            else:
                self._retry_after.pop(run_id, None)
            self._emit(outcome)

    async def _wait_for_progress(self, stop: asyncio.Event) -> None:
        stopper = asyncio.create_task(stop.wait())
        waiters: set[asyncio.Task[Any]] = {stopper}
        waiters.update(self._active.values())
        try:
            await asyncio.wait(
                waiters,
                timeout=self.poll_interval_s,
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            if not stopper.done():
                stopper.cancel()
            await asyncio.gather(stopper, return_exceptions=True)

    async def _execute(self, discovered: Run) -> DispatchOutcome:
        try:
            with self._store_call() as store:
                task = store.get_task(discovered.task_id)
                claimed = store.claim_run(
                    discovered.id, lease_seconds=self.lease_seconds,
                )
        except ExecutionLeaseError:
            # Another dispatcher won the conditional claim. This is expected
            # under multi-worker discovery and is not a task failure.
            return DispatchOutcome(
                discovered.task_id, discovered.id, "claimed_elsewhere",
                discovered.attempt,
            )
        if task is None:
            return DispatchOutcome(
                discovered.task_id, discovered.id, "worker_error",
                discovered.attempt, "MissingTask",
            )
        if claimed.cancel_requested:
            with self._store_call() as store:
                try:
                    cancelled = store.finish_cancelled(
                        claimed.id, claimed.lease_token or "",
                    )
                except (ExecutionLeaseError, ExecutionStateError):
                    cancelled = store.get_run(claimed.id)
            if cancelled is not None and cancelled.state.value == "cancelled":
                return DispatchOutcome(
                    task.id, cancelled.id, cancelled.state.value,
                    cancelled.attempt,
                )
            return DispatchOutcome(
                task.id, claimed.id, "worker_error", claimed.attempt,
                "CancellationSettlementRequired",
            )
        try:
            await self.executor(task, claimed)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return DispatchOutcome(
                task.id, discovered.id, "worker_error", claimed.attempt,
                type(exc).__name__,
            )

        with self._store_call() as store:
            current = store.get_run(discovered.id)
        if current is None:
            return DispatchOutcome(
                task.id, discovered.id, "worker_error", discovered.attempt,
                "MissingRun",
            )
        return DispatchOutcome(
            task.id, current.id, current.state.value, current.attempt,
            (
                str(current.error.get("type"))
                if current.error is not None and current.error.get("type")
                else None
            ),
        )

    def _emit(self, outcome: DispatchOutcome) -> None:
        if self.outcome_sink is not None:
            self.outcome_sink(outcome)

    @contextlib.contextmanager
    def _store_scope(self):
        """Keep one connection for a complete dispatcher invocation."""
        if self._store is not None:
            raise RuntimeError("TaskDispatcher is already running")
        with ExecutionStore(self.execution_path) as store:
            self._store = store
            try:
                yield
            finally:
                self._store = None

    @contextlib.contextmanager
    def _store_call(self):
        """Reuse the invocation store, with a safe fallback for private calls."""
        if self._store is not None:
            yield self._store
            return
        with ExecutionStore(self.execution_path) as store:
            yield store

    @staticmethod
    def _positive_seconds(value: float, name: str) -> float:
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not _is_finite_number(value)
            or value <= 0
        ):
            raise ValueError(f"{name} must be a positive finite number")
        return float(value)
