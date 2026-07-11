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

from .session import (
    open_session,
    replay,
    ReplaySession
)
from .skills import Skill, SkillError, SkillRegistry, discover_skills, load_skill
from .trace import iter_trace, render_trace, write_jsonl
from .agent import Agent
from .operations import Operation, OperationJournal, PendingOperation
from .team import Team
from .supervisor_projection import SupervisorProjection, project_supervisor

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
    # Portable Markdown skills
    "Skill", "SkillError", "SkillRegistry", "load_skill", "discover_skills",
    # Audit-log views
    "iter_trace", "render_trace", "write_jsonl",
    "Agent",
    "Operation", "OperationJournal", "PendingOperation",
    "Team", "SupervisorProjection", "project_supervisor",
]
