"""Standardized error classification for adapter calls.

Provider-specific exceptions are mapped to one of these kinds before
being recorded on an effect result. This lets policy and replay
reason about errors uniformly across providers.

The transient/permanent distinction is advisory — it informs default
retry policy but does not force it. Policy decides.
"""
from __future__ import annotations
import math
from enum import Enum


class ErrorKind(str, Enum):
    # Transient — typically retryable as-is.
    RATE_LIMIT = "rate_limit"
    TIMEOUT = "timeout"
    NETWORK = "network"
    SERVER_ERROR = "server_error"

    # Permanent — not retryable without changing the request.
    AUTH = "auth"
    INVALID_REQUEST = "invalid_request"
    CONTEXT_LENGTH = "context_length"
    CONTENT_FILTER = "content_filter"

    # Catch-all for unmappable provider errors.
    UNKNOWN = "unknown"

    @property
    def is_transient(self) -> bool:
        return self in _TRANSIENT


_TRANSIENT = frozenset({
    ErrorKind.RATE_LIMIT,
    ErrorKind.TIMEOUT,
    ErrorKind.NETWORK,
    ErrorKind.SERVER_ERROR,
})


# ============================================================
# Below: P2.2 additions. Above is frozen (P2.1).
# ============================================================

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, TypedDict, cast

if TYPE_CHECKING:
    # Avoid circular import: lipas.adapter.__init__ imports ErrorKind
    # from this module before .types is registered. Annotations are
    # strings under `from __future__ import annotations`, and
    # classify() only duck-types reply.stop_reason / reply.error_detail
    # at runtime — no concrete Reply class needed.
    from .types import Reply

# -- ErrorDetail variants ---------------------------------------------
#
# These mirror the dict literals constructed by AnthropicAdapter's
# error reply helpers. Reply.error_detail stays typed as
# dict[str, Any] in adapter.py — TypedDict is purely a consumer-side
# discipline for classify(), not an adapter-side construction
# constraint. Adding a new provider error shape = add a TypedDict +
# add it to the union; not a breaking change.

class HTTPErrorDetail(TypedDict):
    type: Literal["http_error"]
    status_code: int
    body: dict[str, Any]


class NetworkErrorDetail(TypedDict):
    type: Literal["network_error"]
    exception_type: str
    message: str


class ProviderErrorDetail(TypedDict):
    type: Literal["provider_error"]
    provider_error: dict[str, Any]


ErrorDetail = HTTPErrorDetail | NetworkErrorDetail | ProviderErrorDetail


# -- Classifier -------------------------------------------------------

# Exception class names (httpx + stdlib) that indicate timeout rather
# than generic network failure. P2.1's _network_error_reply collapses
# everything to exception_type: str, so this is the only signal we
# have. Maintained as a closed set here; adding a new variant is a
# one-line change driven by real integration (not speculation).
_TIMEOUT_EXC_NAMES = frozenset({
    "ReadTimeout", "WriteTimeout", "PoolTimeout", "ConnectTimeout",
    "TimeoutException",  # httpx base class
})


def classify(reply: Reply) -> ErrorKind:
    """Map a failed Reply to an ErrorKind.

    Contract
    --------
    * reply.stop_reason MUST be "error" — calling on a successful
      reply is a programmer error and raises ValueError. The
      classifier is never the right place to ask "did this
      succeed?".
    * reply.error_detail content is treated permissively: unknown
      `type` values fall through to ErrorKind.UNKNOWN rather than
      raising. New provider error shapes must not break the
      classifier.

    The raise/return asymmetry is deliberate: structural mistakes
    (wrong reply kind) raise; semantic novelty (unrecognised error
    payload) returns UNKNOWN.
    """
    if reply.stop_reason != "error":
        raise ValueError(
            f"classify() requires stop_reason='error'; got "
            f"{reply.stop_reason!r}. This is a programmer error — "
            f"check the call site, do not catch this."
        )

    detail = reply.error_detail or {}
    dtype = detail.get("type")

    if dtype == "http_error":
        http_detail = cast(HTTPErrorDetail, detail)
        return _classify_http(
            http_detail["status_code"], http_detail.get("body") or {},
        )

    if dtype == "network_error":
        network_detail = cast(NetworkErrorDetail, detail)
        if network_detail["exception_type"] in _TIMEOUT_EXC_NAMES:
            return ErrorKind.TIMEOUT
        return ErrorKind.NETWORK

    if dtype == "provider_error":
        provider_detail = cast(ProviderErrorDetail, detail)
        return _classify_provider(provider_detail["provider_error"])

    return ErrorKind.UNKNOWN


def _classify_http(status: int, body: dict[str, Any]) -> ErrorKind:
    if status == 429:
        return ErrorKind.RATE_LIMIT
    if status in (401, 403):
        return ErrorKind.AUTH
    if status == 400:
        # Anthropic surfaces context-length as 400 with a specific
        # error.type. Inspect before falling back to INVALID_REQUEST.
        err = (body.get("error") or {}) if isinstance(body, dict) else {}
        etype = err.get("type", "")
        if "context" in etype.lower() or "too long" in str(err.get("message", "")).lower():
            return ErrorKind.CONTEXT_LENGTH
        return ErrorKind.INVALID_REQUEST
    if 400 <= status < 500:
        return ErrorKind.INVALID_REQUEST
    if 500 <= status < 600:
        return ErrorKind.SERVER_ERROR
    return ErrorKind.UNKNOWN


def _classify_provider(provider_err: dict[str, Any]) -> ErrorKind:
    # Anthropic SSE error shape: {"type": "overloaded_error", ...}
    # or {"type": "rate_limit_error", ...}, etc.
    etype = str(provider_err.get("type", "")).lower()
    if "rate_limit" in etype:
        return ErrorKind.RATE_LIMIT
    if "overloaded" in etype:
        # Folded into SERVER_ERROR for now (see backlog B-4).
        return ErrorKind.SERVER_ERROR
    if "authentication" in etype or "permission" in etype:
        return ErrorKind.AUTH
    if "invalid_request" in etype:
        return ErrorKind.INVALID_REQUEST
    if "content_policy" in etype or "content_filter" in etype:
        return ErrorKind.CONTENT_FILTER
    return ErrorKind.UNKNOWN


# -- RetryPolicy ------------------------------------------------------

@dataclass(frozen=True)
class RetryPolicy:
    """Advisory retry parameters for an ErrorKind.

    ``call_with_retry`` consumes this table. ``base_delay_s`` is a hint,
    not a hard interval — the executor layers jitter/backoff on top.
    """
    should_retry: bool
    base_delay_s: float
    max_attempts: int

    def __post_init__(self) -> None:
        if not isinstance(self.should_retry, bool):
            raise TypeError("RetryPolicy.should_retry must be bool")
        if (
            isinstance(self.base_delay_s, bool)
            or not isinstance(self.base_delay_s, (int, float))
            or not math.isfinite(float(self.base_delay_s))
            or self.base_delay_s < 0
        ):
            raise ValueError("RetryPolicy.base_delay_s must be finite and non-negative")
        if (
            isinstance(self.max_attempts, bool)
            or not isinstance(self.max_attempts, int)
            or self.max_attempts < 1
        ):
            raise ValueError("RetryPolicy.max_attempts must be a positive int")
        if not self.should_retry and self.max_attempts != 1:
            raise ValueError(
                "a non-retrying RetryPolicy must have max_attempts=1",
            )


# Defaults are deliberately conservative. Real values get tuned
# against production telemetry once we have any.
DEFAULT_POLICY: dict[ErrorKind, RetryPolicy] = {
    ErrorKind.RATE_LIMIT:       RetryPolicy(True,  2.0, 5),
    ErrorKind.TIMEOUT:          RetryPolicy(True,  1.0, 3),
    ErrorKind.NETWORK:          RetryPolicy(True,  1.0, 3),
    ErrorKind.SERVER_ERROR:     RetryPolicy(True,  2.0, 4),
    ErrorKind.AUTH:             RetryPolicy(False, 0.0, 1),
    ErrorKind.INVALID_REQUEST:  RetryPolicy(False, 0.0, 1),
    ErrorKind.CONTEXT_LENGTH:   RetryPolicy(False, 0.0, 1),
    ErrorKind.CONTENT_FILTER:   RetryPolicy(False, 0.0, 1),
    ErrorKind.UNKNOWN:          RetryPolicy(False, 0.0, 1),
}
# Invariant enforced by test_default_policy_covers_all_kinds.
