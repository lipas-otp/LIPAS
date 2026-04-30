"""Reply: assembled result of an LLM call.

P2.0 amendment (P2.1 review): added `error_detail` field. Required by
the locked adapter error contract (see protocol.py docstring): when
stop_reason='error', error_detail carries provider-raw diagnostic
info sufficient for the call_result layer to classify into ErrorKind.
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Literal, Mapping, Sequence

from .content import ContentBlock
from .usage import Usage


StopReason = Literal[
    "end_turn",        # model finished naturally
    "max_tokens",      # hit max_tokens ceiling
    "stop_sequence",   # hit a configured stop sequence
    "tool_use",        # model emitted a tool_use block, awaiting result
    "error",           # provider/transport/runtime failure; see error_detail
]


@dataclass(frozen=True, slots=True)
class Reply:
    """One terminal reply from an LLMAdapter.stream() call.

    Usage on error replies
    ----------------------
    When ``stop_reason == "error"``, ``usage`` MAY be non-zero if
    the provider billed for partial output before the error
    surfaced (mid-stream disconnect, content filter trip after N
    tokens generated, max_tokens-then-error). Adapters SHOULD
    report whatever the provider actually billed, even on error.

    Adapters that cannot determine partial usage MUST set
    ``Usage()`` (all zeros) rather than guess.

    Downstream:
        - LLMHarness records non-zero usage on error replies as
          ``resource_spent`` / ``budget_overrun`` so the audit
          ledger reflects what the provider actually charged.
        - Replay reproduces the recorded ``usage`` verbatim; do
          not recompute on the replay path.

    error_detail
    ------------
    When ``stop_reason == "error"``, ``error_detail`` MUST conform
    to one of the TypedDicts in lipas.adapter.errors
    (``HTTPErrorDetail`` / ``NetworkErrorDetail`` /
    ``ProviderErrorDetail``). Non-conforming shapes degrade
    classify() to ``ErrorKind.UNKNOWN`` and force the call to be
    treated as non-retryable.
    """

    content: Sequence[ContentBlock]
    usage: Usage
    stop_reason: StopReason
    model: str
    # Provider-raw error info. None when stop_reason != "error".
    # Conventionally carries at least: {"type": str, "message": str,
    # "status_code": int | None, "provider_raw": Any}.
    # Exact schema is the adapter's choice; the call_result layer
    # consumes this and maps to ErrorKind.
    error_detail: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        if self.stop_reason == "error" and self.error_detail is None:
            raise ValueError(
                "Reply with stop_reason='error' must populate error_detail"
            )
        if self.stop_reason != "error" and self.error_detail is not None:
            raise ValueError(
                f"Reply with stop_reason={self.stop_reason!r} must not "
                f"populate error_detail (got {self.error_detail!r})"
            )
