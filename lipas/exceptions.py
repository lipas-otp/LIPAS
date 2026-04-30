"""Exception and warning types for lipas.

All errors inherit from LipasError. All warnings inherit from UserWarning so that
users can escalate with `warnings.filterwarnings('error', category=...)` in CI.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .types import StreamEvent


class LipasError(Exception):
    """Base class for all lipas-raised exceptions."""


class ToolAlreadyInvoked(LipasError):
    """Raised on the 2nd+ call to ToolCall.invoke / ainvoke on the same instance.

    See INV-TOOLCALL-ONCE. The invocation counter is set on handler *entry*,
    not completion — a raising handler does NOT unlock retry. Use
    ToolCall.copy() to create a fresh instance for retry.
    """


class LipasStreamError(LipasError):
    """Raised when an in-flight stream fails (provider 500, network drop, timeout).

    The `Done` event is NOT emitted when this is raised. Events already yielded
    before failure are preserved in `.partial` for debugging — they are not
    rolled back (the caller may have already written tokens to stdout).
    """

    def __init__(
        self,
        cause: BaseException,
        partial: "list[StreamEvent] | None" = None,
    ):
        super().__init__(f"lipas stream failed: {cause!r}")
        self.cause = cause
        self.partial: list = list(partial or [])


class LipasDesyncWarning(UserWarning):
    """Emitted when _lipas.content_hash does not match the current message content.

    Indicates the user edited message['content'] or message['tool_calls'] after
    _lipas was attached. lipas falls back to the generic OpenAI-format path for
    this message, which means:

      - prompt cache will not hit on this message;
      - provider-native features (thinking signatures, multimodal tool_result,
        cache_control markers) will not be used.

    Suppress: `del msg['_lipas']` after editing, or `lipas.invalidate_native(msg)`.
    Escalate: `warnings.filterwarnings('error', category=LipasDesyncWarning)`.

    See INV-LIPAS-HASH.
    """


# Alias — same class, two names for two call sites in the spec text.
LipasStaleNativeWarning = LipasDesyncWarning


class LipasUnknownStopReasonWarning(UserWarning):
    """Emitted when a provider returns a stop_reason value lipas does not know.

    Reply.stop_reason is set to 'end_turn' as a safe default; the raw value is
    preserved in Reply.provider_message for debugging / manual branching.

    See INV-STOP-REASON-NORMALIZED.
    """


class LipasUnknownProviderWarning(UserWarning):
    """Emitted when Reply.as_message() encounters a provider not in KNOWN_PROVIDERS.

    The `_lipas` sidecar is NOT attached — cross-adapter continuation would
    silently degrade anyway, and the typo case (`"opeani"`) is far more common
    than "I have a legitimate new provider". This warning is the only place
    users find out their round-trip is degrading due to a provider-name typo.

    Suppress by: fixing the typo, OR (if you truly have a new provider) adding
    it to `lipas.KNOWN_PROVIDERS` before constructing the Reply, OR attaching
    the sidecar manually with `lipas.attach_lipas(...)`.
    """
