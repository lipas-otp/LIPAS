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
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Mapping
from urllib.parse import urljoin, urlsplit

from .operations import (
    Operation, OperationJournal, OperationStateError, PendingOperation,
)

__all__ = [
    "EgressPolicy", "HttpClient", "HttpClientError", "HttpResponse",
    "HttpOperationUncertain", "RateLimitPolicy", "RateLimitExceeded",
    "ConnectorSpec", "ConnectorRegistry",
]


def _finite_number(value: Any, name: str, *, positive: bool = False) -> float:
    """Validate numeric connector settings without leaking float overflow."""
    try:
        valid = (
            not isinstance(value, bool)
            and isinstance(value, (int, float))
            and math.isfinite(float(value))
            and (not positive or value > 0)
        )
    except (OverflowError, TypeError, ValueError):
        valid = False
    if not valid:
        qualifier = "finite and positive" if positive else "finite"
        raise ValueError(f"{name} must be {qualifier}")
    return float(value)


def _default_port(scheme: str) -> int | None:
    return {"http": 80, "https": 443}.get(scheme.lower())


def _strict_json_copy(value: Any, name: str) -> Any:
    """Detach JSON data and reject values Python would otherwise coerce."""
    active: set[int] = set()

    def validate(item: Any, path: str) -> None:
        if item is None or isinstance(item, (bool, int, str)):
            return
        if isinstance(item, float):
            if not math.isfinite(item):
                raise ValueError(f"{path} contains a non-finite number")
            return
        if not isinstance(item, (list, tuple, Mapping)):
            raise TypeError(f"{path} contains unsupported {type(item).__name__}")
        marker = id(item)
        if marker in active:
            raise ValueError(f"{path} contains a reference cycle")
        active.add(marker)
        try:
            if isinstance(item, Mapping):
                for key, child in item.items():
                    if not isinstance(key, str):
                        raise ValueError(f"{path} must use string object keys")
                    validate(child, f"{path}.{key}")
            else:
                for index, child in enumerate(item):
                    validate(child, f"{path}[{index}]")
        finally:
            active.remove(marker)

    try:
        validate(value, name)
        encoded = json.dumps(
            value, sort_keys=True, separators=(",", ":"),
            ensure_ascii=False, allow_nan=False,
        )
        return json.loads(encoded)
    except (TypeError, ValueError, RecursionError) as exc:
        raise HttpClientError(f"{name} must be strict JSON") from exc


@dataclass(frozen=True, slots=True)
class EgressPolicy:
    """Allow only explicitly listed HTTP(S) hosts."""

    allowed_hosts: frozenset[str] = frozenset()
    allow_http: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.allowed_hosts, (set, frozenset, tuple, list)):
            raise TypeError("allowed_hosts must be a collection of host names")
        if any(not isinstance(value, str) or not value.strip() for value in self.allowed_hosts):
            raise ValueError("allowed_hosts must contain non-empty strings")
        hosts = frozenset(value.lower().strip() for value in self.allowed_hosts)
        if len(hosts) != len(self.allowed_hosts):
            raise ValueError("allowed_hosts contains duplicate values after normalization")
        if not isinstance(self.allow_http, bool):
            raise TypeError("allow_http must be bool")
        object.__setattr__(self, "allowed_hosts", hosts)

    def check(self, url: str) -> None:
        parsed = urlsplit(url)
        if parsed.username is not None or parsed.password is not None:
            raise HttpClientError("HTTP URL embedded credentials are disallowed")
        scheme = parsed.scheme.lower()
        if scheme not in {"https", "http"}:
            raise HttpClientError("HTTP URL must use https or http")
        if scheme == "http" and not self.allow_http:
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
        return json.loads(
            self.body.decode("utf-8"),
            parse_constant=lambda raw: (_ for _ in ()).throw(
                ValueError(f"non-JSON numeric constant {raw!r}")
            ),
        )


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


class RateLimitExceeded(HttpClientError):
    """A connector call was locally throttled before egress."""


@dataclass
class RateLimitPolicy:
    """Small process-local sliding-window limiter for first-party connectors."""

    max_requests: int
    window_s: float = 1.0

    def __post_init__(self) -> None:
        if isinstance(self.max_requests, bool) or not isinstance(self.max_requests, int) or self.max_requests < 1:
            raise ValueError("max_requests must be a positive int")
        self.window_s = _finite_number(self.window_s, "window_s", positive=True)
        self._lock = threading.Lock()
        self._calls: deque[float] = deque()

    def acquire(self, *, now: float | None = None) -> None:
        if now is None:
            timestamp = time.monotonic()
        else:
            timestamp = _finite_number(now, "now")
        with self._lock:
            cutoff = timestamp - self.window_s
            while self._calls and self._calls[0] <= cutoff:
                self._calls.popleft()
            if len(self._calls) >= self.max_requests:
                retry_after = max(0.0, self._calls[0] + self.window_s - timestamp)
                raise RateLimitExceeded(f"connector rate limit exceeded; retry after {retry_after:.3f}s")
            self._calls.append(timestamp)


@dataclass(frozen=True, slots=True)
class ConnectorSpec:
    """Declared connector boundary; registration grants no execution rights."""

    name: str
    version: str = "1"
    capabilities: frozenset[str] = frozenset()
    supports_reconciliation: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("connector name must be non-empty")
        if not isinstance(self.version, str) or not self.version.strip():
            raise ValueError("connector version must be non-empty")
        if not isinstance(self.capabilities, frozenset):
            raise TypeError("connector capabilities must be a frozenset")
        if any(not isinstance(item, str) or not item.strip() for item in self.capabilities):
            raise ValueError("connector capabilities must contain non-empty strings")
        if not isinstance(self.supports_reconciliation, bool):
            raise TypeError("supports_reconciliation must be bool")
        normalized = frozenset(item.strip() for item in self.capabilities)
        if len(normalized) != len(self.capabilities):
            raise ValueError("connector capabilities contain duplicate values after normalization")
        object.__setattr__(self, "name", self.name.strip())
        object.__setattr__(self, "version", self.version.strip())
        object.__setattr__(self, "capabilities", normalized)


class ConnectorRegistry:
    """Host-owned connector registry with immutable names and descriptors."""

    def __init__(self) -> None:
        self._items: dict[str, tuple[ConnectorSpec, Any]] = {}

    def register(self, spec: ConnectorSpec, connector: Any) -> ConnectorSpec:
        if not isinstance(spec, ConnectorSpec):
            raise TypeError("spec must be ConnectorSpec")
        if connector is None:
            raise TypeError("connector must not be None")
        previous = self._items.get(spec.name)
        if previous is not None and previous[0] != spec:
            raise ValueError(f"connector {spec.name!r} is already registered with a different contract")
        self._items[spec.name] = (spec, connector)
        return spec

    def get(self, name: str) -> Any | None:
        item = self._items.get(name)
        return None if item is None else item[1]

    def spec(self, name: str) -> ConnectorSpec | None:
        item = self._items.get(name)
        return None if item is None else item[0]

    def list(self) -> tuple[ConnectorSpec, ...]:
        return tuple(self._items[name][0] for name in sorted(self._items))


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
        rate_limit: RateLimitPolicy | None = None,
    ) -> None:
        if base_url is not None:
            self._validate_url(base_url)
        timeout_s = _finite_number(timeout_s, "timeout_s", positive=True)
        self.base_url = base_url.rstrip("/") + "/" if base_url else None
        if egress is None:
            parsed_base = urlsplit(base_url) if base_url else None
            host = (parsed_base.hostname or "").lower() if parsed_base else ""
            egress = EgressPolicy(frozenset({host}) if host else frozenset())
        self.egress = egress
        self.journal = journal
        self.timeout_s = timeout_s
        if headers is not None and not isinstance(headers, Mapping):
            raise TypeError("headers must be a mapping or None")
        self.headers = dict(headers or {})
        self._validate_headers(self.headers)
        self._client = client
        if rate_limit is not None and not isinstance(rate_limit, RateLimitPolicy):
            raise TypeError("rate_limit must be RateLimitPolicy or None")
        self.rate_limit = rate_limit

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
        if not isinstance(method, str):
            raise TypeError("method must be a string")
        method = method.upper().strip()
        if not method or any(char.isspace() for char in method):
            raise ValueError("method must be non-empty")
        if params is not None and not isinstance(params, Mapping):
            raise TypeError("params must be a mapping or None")
        if headers is not None and not isinstance(headers, Mapping):
            raise TypeError("headers must be a mapping or None")
        normalized_params = self._normalize_params(params)
        normalized_body = _strict_json_copy(json_body, "json_body")
        target = self._resolve_url(url)
        self.egress.check(target)
        write = method not in {"GET", "HEAD", "OPTIONS"}
        if request_id is not None and (
            not isinstance(request_id, str) or not request_id.strip()
        ):
            raise ValueError("request_id must be a non-empty string or None")
        if request_id is not None:
            request_id = request_id.strip()
        if idempotency_key is not None and (
            not isinstance(idempotency_key, str) or not idempotency_key.strip()
        ):
            raise HttpClientError(
                "idempotency_key must be a non-empty string or None",
            )
        if idempotency_key is not None:
            idempotency_key = idempotency_key.strip()
        if write and idempotency_key is None:
            raise HttpClientError(
                "external HTTP writes require an explicit idempotency_key",
            )
        if request_id is None:
            request_id = idempotency_key
        if request_id is None:
            request_id = self._request_fingerprint(
                method, target, normalized_body, params=normalized_params,
            )
        # ``idempotency_key`` is the LIPAS operation identity; ``request_id``
        # is the provider correlation identity. They may intentionally differ,
        # but both are durable and never silently replaced once supplied.
        assert isinstance(request_id, str) and request_id.strip()
        operation: Operation | None = None
        request_payload = {
            "method": method,
            "url": target,
            "params": normalized_params,
            "body_sha256": self._body_hash(normalized_body),
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
        self._validate_headers(merged)
        # Provider identity is part of the durable operation contract. A
        # caller-supplied header must not be able to silently disagree with
        # the identity recorded in the journal.
        for key in tuple(merged):
            if key.lower() in {"x-request-id", "idempotency-key"}:
                del merged[key]
        merged["X-Request-ID"] = request_id
        if write:
            merged["Idempotency-Key"] = idempotency_key or request_id
        if self.rate_limit is not None:
            self.rate_limit.acquire()
        try:
            response = await self._send(
                method, target, params=normalized_params, json_body=normalized_body,
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
        if not isinstance(response.status_code, int) or isinstance(
            response.status_code, bool,
        ) or not 100 <= response.status_code <= 599:
            cause = HttpClientError("provider returned an invalid HTTP status")
            if write and operation is not None and self.journal is not None:
                current = self._mark_uncertain(operation.key, cause)
                raise HttpOperationUncertain(current, cause)
            raise cause
        if 300 <= response.status_code < 400:
            cause = HttpClientError(
                f"provider redirected HTTP request to status {response.status_code}",
            )
            if write and operation is not None and self.journal is not None:
                current = self._mark_uncertain(operation.key, cause)
                raise HttpOperationUncertain(current, cause)
            raise cause
        if not self._same_origin(target, response.url):
            cause = HttpClientError(
                "provider response origin differs from requested HTTP origin",
            )
            if write and operation is not None and self.journal is not None:
                current = self._mark_uncertain(operation.key, cause)
                raise HttpOperationUncertain(current, cause)
            raise cause
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
                method, url, timeout=self.timeout_s, follow_redirects=False, **kwargs,
            )
        else:
            try:
                import httpx
            except ImportError as exc:  # pragma: no cover - optional dependency
                raise HttpClientError("install lipas[compatible] for HttpClient") from exc
            async with httpx.AsyncClient(timeout=self.timeout_s) as client:
                response = await client.request(
                    method, url, timeout=self.timeout_s,
                    follow_redirects=False, **kwargs,
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
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
            raise HttpClientError("URL must be an absolute HTTP(S) URL")

    @staticmethod
    def _body_hash(body: Any) -> str:
        body = _strict_json_copy(body, "json_body")
        raw = json.dumps(
            body, sort_keys=True, separators=(",", ":"),
            ensure_ascii=False, allow_nan=False,
        ).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()

    @staticmethod
    def _response_header(headers: Mapping[str, Any], name: str) -> str | None:
        """Read a response header case-insensitively from custom clients."""
        wanted = name.lower()
        for key, value in headers.items():
            if str(key).lower() == wanted:
                return str(value)
        return None

    @staticmethod
    def _normalize_params(params: Mapping[str, Any] | None) -> dict[str, Any]:
        if params is None:
            return {}
        normalized = _strict_json_copy(dict(params), "params")
        if not isinstance(normalized, dict):
            raise HttpClientError("params must be a JSON object")
        return normalized

    @staticmethod
    def _validate_headers(headers: Mapping[str, Any]) -> None:
        for key, value in headers.items():
            if not isinstance(key, str) or not key.strip():
                raise ValueError("HTTP header names must be non-empty strings")
            if not isinstance(value, str):
                raise TypeError("HTTP header values must be strings")
            if "\r" in key or "\n" in key or "\r" in value or "\n" in value:
                raise ValueError("HTTP headers must not contain CR/LF")

    @staticmethod
    def _same_origin(requested: str, returned: str) -> bool:
        try:
            left = urlsplit(requested)
            right = urlsplit(returned)
            left_port = left.port or _default_port(left.scheme)
            right_port = right.port or _default_port(right.scheme)
        except (TypeError, ValueError):
            return False
        return (
            left.scheme.lower() == right.scheme.lower()
            and (left.hostname or "").lower() == (right.hostname or "").lower()
            and left_port == right_port
        )

    @classmethod
    def _request_fingerprint(
        cls,
        method: str,
        url: str,
        body: Any,
        *,
        params: Mapping[str, Any] | None = None,
    ) -> str:
        return "http_" + hashlib.sha256(
            json.dumps(
                {
                    "method": method,
                    "url": url,
                    "params": dict(params or {}),
                    "body_sha256": cls._body_hash(body),
                },
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            ).encode(),
        ).hexdigest()[:24]

    @staticmethod
    def _response_from_operation(operation: Operation) -> HttpResponse:
        result = operation.result
        if not isinstance(result, Mapping):
            raise HttpClientError("journalled HTTP result is malformed")
        status = result.get("status_code")
        headers = result.get("headers", {})
        body = result.get("body", "")
        url = operation.request.get("url")
        if (
            isinstance(status, bool) or not isinstance(status, int)
            or not 100 <= status <= 599
            or not isinstance(headers, Mapping)
            or any(not isinstance(key, str) or not isinstance(value, str)
                   for key, value in headers.items())
            or not isinstance(body, str)
            or not isinstance(url, str)
        ):
            raise HttpClientError("journalled HTTP result is malformed")
        return HttpResponse(
            status,
            dict(headers),
            body.encode("utf-8"),
            url,
            operation.provider_request_id,
            operation.key,
        )
