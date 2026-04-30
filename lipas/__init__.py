"""lipas — a thin, provider-agnostic LLM interface.

Public surface (v1). For normative invariants, see lipas.types module docstring.
"""
from __future__ import annotations

from ._lipas_meta import (
    KNOWN_PROVIDERS,
    LIPAS_KEY,
    LIPAS_SCHEMA_VERSION,
    attach_lipas,
    compute_content_hash,
    invalidate_native,
    should_use_native,
    strip_lipas,
)
from .exceptions import (
    LipasDesyncWarning,
    LipasError,
    LipasStaleNativeWarning,
    LipasStreamError,
    LipasUnknownProviderWarning,
    LipasUnknownStopReasonWarning,
    ToolAlreadyInvoked,
)
# Tool, @tool, ToolRegistry and tool-related errors all live in .tools.
# .types no longer defines Tool (removed in the dedup pass).
from .tools import (
    Tool,
    tool,
    ValidationError,
    InvalidArgumentsError,
    ToolRegistry,
)
from .types import (
    STOP_REASONS,
    Done,
    Message,
    ProviderEvent,
    Reply,
    StopReason,
    StreamEvent,
    TextDelta,
    ThinkingBlock,
    ThinkingDelta,
    ToolCall,
    ToolCallReady,
    ToolCallStart,
    Usage,
)

__all__ = [
    # Core types
    "Message", "Usage", "ThinkingBlock", "ToolCall", "Reply",
    "StopReason", "STOP_REASONS",
    # Stream events
    "StreamEvent", "TextDelta", "ThinkingDelta",
    "ToolCallStart", "ToolCallReady", "Done", "ProviderEvent",
    # Exceptions & warnings
    "LipasError", "ToolAlreadyInvoked", "LipasStreamError",
    "LipasDesyncWarning", "LipasStaleNativeWarning",
    "LipasUnknownStopReasonWarning", "LipasUnknownProviderWarning",
    # _lipas helpers (end-user-facing)
    "strip_lipas", "invalidate_native",
    # _lipas helpers (adapter-facing)
    "attach_lipas", "should_use_native", "compute_content_hash",
    "LIPAS_KEY", "LIPAS_SCHEMA_VERSION", "KNOWN_PROVIDERS",
    # Tool surface
    "Tool", "tool", "ValidationError", "InvalidArgumentsError",
    "ToolRegistry",
]
