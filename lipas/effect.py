"""
LIPAS · Effect schema.

This module defines the kind-discriminated effect schema shared across
LLM calls and tool calls.  The design choice (D1) is *one* effect family
with an ``F_KIND`` discriminator, rather than parallel ``call_*`` /
``tool_*`` tag families.  Rationale:

  - Compensation can cross kinds (a tool call may compensate an LLM
    call's hallucinated commitment, an LLM call may produce text that
    compensates a tool's prior write).  A single lineage graph is
    cheaper than two views that need to be joined at every query.

  - EffectView walking (``chain``, ``descendants``) is identical
    regardless of kind; only payload extraction differs.

  - Pre-flight (Guard) is symmetric: ``Guard.check(target, estimate)``
    where ``target`` is an EffectTarget union.  Guards that only care
    about LLM behavior pattern-match on ``LLMTarget``; guards that
    only care about tool behavior pattern-match on ``ToolTarget``;
    cross-cutting guards (cost ceilings, rate limits) read fields
    common to both.

Two-module layout note
----------------------
This file (``lipas.effect``) owns the *schema types* — the kind enum,
the target union, and shared field-name constants — but NOT the row.
The row lives at ``lipas.rows.effect.EffectRow`` and imports from here.
The split exists so that ``tools.py`` and ``guard.py`` can depend on
the schema types without dragging in the row machinery (which depends
on ``store.py``).  The two-``effect`` modules look symmetric in the
import graph but only one of them touches the store.

Persisted tag names
-------------------
The on-disk tag values are ``"call_intent"``, ``"call_result"``, and
``"call_rejected"``. They are opaque storage values; the public Python names
are ``TAG_EFFECT_*`` because one lifecycle covers model and tool effects.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any, ClassVar, Mapping, Union

if TYPE_CHECKING:
    # Import-cycle guard.  ``Tool`` lives in ``lipas.tools`` which has
    # no runtime dependency on this module; only the type annotations
    # below need it, and TYPE_CHECKING keeps it out of the runtime
    # import graph.  ``Request`` is from ``lipas.adapter`` and is also
    # only used in annotations on the dataclasses.
    from .adapter import Request
    from .tools import Tool


__all__ = [
    "EffectKind",
    "LLMTarget",
    "ToolTarget",
    "EffectTarget",
    # Shared field-name constants — re-exported by lipas.rows.effect
    # for convenience; canonical home is here.
    "F_EFFECT_ID",
    "F_KIND",
    "F_COMPENSATES",
    "F_CAUSED_BY",
    "F_STATUS",
    "F_ATTEMPTS",
    "F_TOTAL_USAGE",
    "F_SPEND",
    "F_ERROR",
    "F_REASON",
    "F_DETAIL",
    "F_MODEL",
    "F_REQUEST",
    "F_REPLY",
    "F_TOOL_NAME",
    "F_ARGUMENTS",
    "F_DECLARED_SIDE_EFFECT",
    "F_OUTPUT",
    "F_SIDE_EFFECT",
    "TAG_EFFECT_INTENT",
    "TAG_EFFECT_RESULT",
    "TAG_EFFECT_REJECTED",
]


# =====================================================================
# Kind discriminator
# =====================================================================

class EffectKind(str, Enum):
    """The kind of effect a node represents.

    String-valued so that ``Claim.fields[F_KIND]`` round-trips through
    JSON / pickle without an enum-shaped type.  Comparisons against
    raw strings ("llm_call" == EffectKind.LLM_CALL) work transparently.

    More kinds may be added in a future release (e.g. ``HTTP_REQUEST`` for direct
    out-of-band fetches the harness wraps).  Adding a kind is a
    schema-evolution operation: the EffectRow validator switches must
    grow a new branch, and any code reading kind-specific fields must
    handle the new branch defensively.  Removing a kind is forbidden
    without a store migration.
    """
    LLM_CALL  = "llm_call"
    TOOL_CALL = "tool_call"


# =====================================================================
# Effect targets — the input-side, pre-effect view
# =====================================================================
#
# A "target" is the about-to-happen effect: the request being made to
# the LLM, or the tool invocation being launched.  Targets are pure
# data, NOT claims; they cross the Guard boundary as parameters but
# never get folded into the store directly (the harness folds the
# corresponding ``effect_intent`` claim from the target).
#
# Frozen dataclasses with ``kind`` as a ClassVar: kind is fixed by the
# class, not a field, so ``LLMTarget(...)`` and ``ToolTarget(...)``
# cannot be constructed with the wrong kind.

@dataclass(frozen=True)
class LLMTarget:
    """Target representing an upcoming LLM call."""
    kind: ClassVar[EffectKind] = EffectKind.LLM_CALL
    request: "Request"


@dataclass(frozen=True)
class ToolTarget:
    """Target representing an upcoming tool invocation.

    ``arguments`` is the kwargs dict to pass to ``Tool.acall`` /
    ``Tool.call``.  The harness will fold these onto the
    ``effect_intent`` claim verbatim (they are part of the audit
    trail).  Callers that wish to redact secrets should redact before
    constructing the target.
    """
    kind: ClassVar[EffectKind] = EffectKind.TOOL_CALL
    tool: "Tool"
    arguments: Mapping[str, Any]


# A discriminated union.  Static type checkers (pyright/mypy) can
# narrow on ``isinstance(target, LLMTarget)`` / ``ToolTarget`` after
# matching; the runtime ``kind`` ClassVar is for human/serializer use.
EffectTarget = Union[LLMTarget, ToolTarget]


# =====================================================================
# Tag string values
# =====================================================================
# Historical strings — DO NOT change without a store migration.

TAG_EFFECT_INTENT   = "call_intent"
TAG_EFFECT_RESULT   = "call_result"
TAG_EFFECT_REJECTED = "call_rejected"


# =====================================================================
# Field-name constants
# =====================================================================
#
# Shared (intent / result / rejected) ─────────────────────────────────
F_EFFECT_ID    = "call_id"          # historical; opaque str
F_KIND         = "kind"             # value: EffectKind member's str
F_COMPENSATES  = "compensates"      # str | absent
# Causal parent outside the effect graph (for example, a Team mailbox id).
# This deliberately stays separate from ``compensates``: compensation is an
# effect-to-effect semantic relation, while causation may begin at a handoff.
F_CAUSED_BY    = "caused_by"         # str | absent

# Shared on result / rejected ─────────────────────────────────────────
F_STATUS   = "status"               # "ok" | "error"
F_ATTEMPTS = "attempts"             # int (retry layer)
F_TOTAL_USAGE = "total_usage"       # aggregate Usage across LLM retries
F_SPEND = "spend"                   # exact tool spend admitted/observed
F_ERROR    = "error"                # dict, present iff status="error"
F_REASON   = "reason"               # rejection reason slug
F_DETAIL   = "detail"               # rejection detail dict

# LLM-specific intent fields ──────────────────────────────────────────
F_MODEL   = "model"
F_REQUEST = "request"
"""
F_REQUEST carries the full Request object for audit. The
Request is structurally unhashable (messages: tuple[dict,...]);
consumers that need to index/dedupe Requests must derive a
hashable key (e.g. effect_id, or a content fingerprint).
"""

# LLM-specific result fields ──────────────────────────────────────────
F_REPLY = "reply"

# Tool-specific intent fields ─────────────────────────────────────────
F_TOOL_NAME            = "tool_name"
F_ARGUMENTS            = "arguments"
F_DECLARED_SIDE_EFFECT = "declared_side_effect"  # SideEffectClass.value

# Tool-specific result fields ─────────────────────────────────────────
F_OUTPUT      = "output"
F_SIDE_EFFECT = "side_effect"  # SideEffectClass.value (≤ declared, see ToolHarness)
