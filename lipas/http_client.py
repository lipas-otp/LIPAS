"""First-party, policy-aware HTTP capability.

This is intentionally smaller than a general HTTP SDK.  It gives LIPAS
connectors one boundary for URL egress, timeouts, redaction, provider request
identity, and uncertain external writes.  Read requests are ordinary fetches;
non-read requests require an idempotency key and an :class:`OperationJournal`.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
from dataclasses import dataclass
from typing import Any, Mapping
from urllib.parse import urljoin, urlsplit

from .operations import (
    Operation, OperationJournal, OperationStateError, PendingOperation,
)

__all__ = [
    "EgressPolicy", "HttpClient", "HttpClientError", "HttpResponse",
    "HttpOperationUncertain",
]


@dataclass(frozen=True, slots=True)
class EgressPolicy:
    """Allow only explicitly listed HTTP(S) hosts."""

    allowed_hosts: frozenset[str] = frozenset()
    allow_http: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.allowed_hosts, (set, frozenset, tuple, list)):
            raise TypeError("allowed_hosts must be a collection of host names")
        hosts = frozenset(
            value.lower().strip()
            for value in self.allowed_hosts
            if isinstance(value, str) and value.strip()
        )
        if len(hosts) != len(self.allowed_hosts):
            raise ValueError("allowed_hosts must contain non-empty strings")
        if not isinstance(self.allow_http, bool):
            raise TypeError("allow_http must be bool")
        object.__setattr__(self, "allowed_hosts", hosts)

    def check(self, url: str) -> None:
        parsed = urlsplit(url)
        if parsed.scheme not in {"https", "http"}:
            raise HttpClientError("HTTP URL must use https or http")
        if parsed.scheme == "http" and not self.allow_http:
            raise HttpClientError("plain HTTP is disabled by egress policy")
        host = (parsed.hostname or "").lower()
        if not host:
            raise HttpClientError("HTTP URL has no host")
        if not self.allowed_hosts:
            raise HttpClientError("egress policy has no allowed hosts")
        if host not in self.allowed_hosts:
            raise HttpClientError(f"egress host {host!r} is not allowlisted")


@dataclass(frozen=True, slots=True)
class HttpResponse:
    status_code: int
    headers: Mapping[str, str]
    body: bytes
    url: str
    request_id: str | None = None
    operation_key: str | None = None

    def json(self) -> Any:
        return json.loads(self.body.decode("utf-8"))


class HttpClientError(RuntimeError):
    """A policy, transport, or response failure."""


class HttpOperationUncertain(HttpClientError):
    """A write may have reached the provider and must be reconciled."""

    def __init__(self, operation: Operation, cause: BaseException) -> None:
        self.operation = operation
        self.cause = cause
        super().__init__(
            f"HTTP operation {operation.key!r} is uncertain; reconcile before retry",
        )


class HttpClient:
    """Async HTTP client with explicit egress and operation semantics."""

    def __init__(
        self,
        *,
        base_url: str | None = None,
        egress: EgressPolicy | None = None,
        journal: OperationJournal | None = None,
        timeout_s: float = 30.0,
        headers: Mapping[str, str] | None = None,
        client: Any | None = None,
    ) -> None:
        if base_url is not None:
            self._validate_url(base_url)
        if (
            isinstance(timeout_s, bool)
            or not isinstance(timeout_s, (int, float))
            or not math.isfinite(float(timeout_s))
            or timeout_s <= 0
        ):
            raise ValueError("timeout_s must be finite and positive")
        self.base_url = base_url.rstrip("/") + "/" if base_url else None
        if egress is None:
            parsed_base = urlsplit(base_url) if base_url else None
            host = (parsed_base.hostname or "").lower() if parsed_base else ""
            egress = EgressPolicy(frozenset({host}) if host else frozenset())
        self.egress = egress
        self.journal = journal
        self.timeout_s = float(timeout_s)
        self.headers = {str(k): str(v) for k, v in (headers or {}).items()}
        self._client = client

    async def request(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        json_body: Any = None,
        headers: Mapping[str, str] | None = None,
        request_id: str | None = None,
        idempotency_key: str | None = None,
        effect_id: str | None = None,
        kind: str = "http_request",
    ) -> HttpResponse:
        method = method.upper().strip()
        if not method:
            raise ValueError("method must be non-empty")
        target = self._resolve_url(url)
        self.egress.check(target)
        request_id = request_id or idempotency_key
        write = method not in {"GET", "HEAD", "OPTIONS"}
        if write and not idempotency_key:
            raise HttpClientError(
                "external HTTP writes require an explicit idempotency_key",
            )
        if request_id is None:
            request_id = self._request_fingerprint(method, target, json_body)
        if not isinstance(request_id, str) or not request_id.strip():
            raise ValueError("request_id must be a non-empty string")
        operation: Operation | None = None
        request_payload = {
            "method": method,
            "url": target,
            "params": dict(params or {}),
            "body_sha256": self._body_hash(json_body),
            "provider_request_id": request_id,
        }
        if write:
            if self.journal is None:
                raise HttpClientError(
                    "an OperationJournal is required for external HTTP writes",
                )
            operation, owns_submission = self.journal._prepare(
                key=idempotency_key or request_id,
                kind=kind,
                request=request_payload,
                effect_id=effect_id,
                provider_request_id=request_id,
            )
            if operation.state == "succeeded":
                return self._response_from_operation(operation)
            if not owns_submission:
                raise PendingOperation(
                    f"HTTP operation {operation.key!r} is {operation.state}; reconcile first",
                )
        merged = {**self.headers, **dict(headers or {})}
        # Provider identity is part of the durable operation contract. A
        # caller-supplied header must not be able to silently disagree with
        # the identity recorded in the journal.
        for key in tuple(merged):
            if key.lower() in {"x-request-id", "idempotency-key"}:
                del merged[key]
        merged["X-Request-ID"] = request_id
        if write:
            merged["Idempotency-Key"] = idempotency_key or request_id
        try:
            response = await self._send(
                method, target, params=params, json_body=json_body,
                headers=merged,
            )
        except asyncio.CancelledError as exc:
            # Cancellation does not prove that the provider did not receive
            # the request. Persist uncertainty, but preserve cooperative
            # cancellation semantics for the caller.
            if operation is not None and self.journal is not None:
                self._mark_uncertain(operation.key, exc)
            raise
        except Exception as exc:
            if operation is not None and self.journal is not None:
                current = self._mark_uncertain(operation.key, exc)
                raise HttpOperationUncertain(current, exc) from exc
            raise HttpClientError(str(exc)) from exc
        except BaseException as exc:
            # Preserve process-level interrupt semantics while retaining the
            # safety invariant that an interrupted write is uncertain.
            if operation is not None and self.journal is not None:
                self._mark_uncertain(operation.key, exc)
            raise
        if write and operation is not None and self.journal is not None:
            if response.status_code >= 400:
                failed = self.journal.fail(
                    operation.key,
                    error={
                        "type": "provider_http_error",
                        "status_code": response.status_code,
                    },
                )
                return HttpResponse(
                    response.status_code,
                    response.headers,
                    response.body,
                    response.url,
                    failed.provider_request_id,
                    failed.key,
                )
            settled = self.journal.settle(
                operation.key,
                result={
                    "status_code": response.status_code,
                    "headers": dict(response.headers),
                    "body": response.body.decode("utf-8", errors="replace"),
                },
                provider_reference=self._response_header(
                    response.headers, "idempotency-key",
                ) or self._response_header(response.headers, "x-request-id"),
            )
            return self._response_from_operation(settled)
        return response

    async def _send(self, method: str, url: str, **kwargs: Any) -> HttpResponse:
        # ``json_body`` is the explicit LIPAS boundary name; map it to the
        # actual httpx keyword instead of leaking an internal name into the
        # transport. The injected client follows the same httpx contract.
        if "json_body" in kwargs:
            kwargs["json"] = kwargs.pop("json_body")
        if self._client is not None:
            response = await self._client.request(
                method, url, timeout=self.timeout_s, **kwargs,
            )
        else:
            try:
                import httpx
            except ImportError as exc:  # pragma: no cover - optional dependency
                raise HttpClientError("install lipas[compatible] for HttpClient") from exc
            async with httpx.AsyncClient(timeout=self.timeout_s) as client:
                response = await client.request(
                    method, url, timeout=self.timeout_s, **kwargs,
                )
        if response.status_code >= 500:
            raise HttpClientError(f"provider returned HTTP {response.status_code}")
        return HttpResponse(
            response.status_code, dict(response.headers), response.content, str(response.url),
        )

    def _mark_uncertain(self, key: str, cause: BaseException) -> Operation:
        assert self.journal is not None
        try:
            return self.journal.mark_uncertain(
                key,
                error={"type": type(cause).__name__, "message": str(cause)},
            )
        except OperationStateError:
            # A concurrent reconciler may already have settled the operation;
            # its durable outcome is authoritative.
            latest = self.journal.get(key)
            if latest is None:
                raise
            return latest

    def _resolve_url(self, value: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("url must be a non-empty string")
        target = urljoin(self.base_url or "", value)
        self._validate_url(target)
        return target

    @staticmethod
    def _validate_url(value: str) -> None:
        parsed = urlsplit(value)
        if parsed.query or parsed.fragment or parsed.username or parsed.password:
            raise HttpClientError("URL query/fragment/embedded credentials are disallowed")
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise HttpClientError("URL must be an absolute HTTP(S) URL")

    @staticmethod
    def _body_hash(body: Any) -> str:
        raw = json.dumps(body, sort_keys=True, separators=(",", ":"), default=str).encode()
        return hashlib.sha256(raw).hexdigest()

    @staticmethod
    def _response_header(headers: Mapping[str, Any], name: str) -> str | None:
        """Read a response header case-insensitively from custom clients."""
        wanted = name.lower()
        for key, value in headers.items():
            if str(key).lower() == wanted:
                return str(value)
        return None

    @classmethod
    def _request_fingerprint(cls, method: str, url: str, body: Any) -> str:
        return "http_" + hashlib.sha256(
            f"{method}\0{url}\0{cls._body_hash(body)}".encode(),
        ).hexdigest()[:24]

    @staticmethod
    def _response_from_operation(operation: Operation) -> HttpResponse:
        result = operation.result
        if not isinstance(result, Mapping):
            raise HttpClientError("journalled HTTP result is malformed")
        body = str(result.get("body", "")).encode("utf-8")
        return HttpResponse(
            int(result.get("status_code", 0)),
            {str(k): str(v) for k, v in (result.get("headers") or {}).items()},
            body,
            str(operation.request.get("url", "")),
            operation.provider_request_id,
            operation.key,
        )
