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

from dataclasses import dataclass, field
from enum import Enum
from copy import deepcopy
import hashlib
import json
import math
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
    "EffectProposal",
    "EffectDecision",
    "EffectObservation",
    "F_PROPOSAL_ID",
    "F_PROPOSAL_KIND",
    "F_ACTOR",
    "F_CAPABILITIES",
    "F_RISK",
    "F_ESTIMATE",
    "F_PROPOSAL_METADATA",
    # Shared field-name constants — re-exported by lipas.rows.effect
    # for convenience; canonical home is here.
    "F_EFFECT_ID",
    "F_PROVIDER_REQUEST_ID",
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

    def __post_init__(self) -> None:
        from .adapter import Request

        if not isinstance(self.request, Request):
            raise TypeError("LLMTarget.request must be an adapter Request")


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

    def __post_init__(self) -> None:
        from .tools import Tool

        if not isinstance(self.tool, Tool):
            raise TypeError("ToolTarget.tool must be a Tool")
        if not isinstance(self.arguments, Mapping):
            raise TypeError("ToolTarget.arguments must be a mapping")
        object.__setattr__(self, "arguments", deepcopy(dict(self.arguments)))


# A discriminated union.  Static type checkers (pyright/mypy) can
# narrow on ``isinstance(target, LLMTarget)`` / ``ToolTarget`` after
# matching; the runtime ``kind`` ClassVar is for human/serializer use.
EffectTarget = Union[LLMTarget, ToolTarget]


@dataclass(frozen=True)
class EffectProposal:
    """A request for change in the world, before Runtime policy admits it.

    Agents and orchestration layers may construct proposals, but a proposal
    is not an execution and grants no authority.  The host Runtime decides
    whether the declared capabilities, budget, risk, and causal context are
    acceptable before an existing Harness/connector performs the effect.
    """

    effect_id: str
    kind: str
    actor: str
    capabilities: frozenset[str] = frozenset()
    estimate: Mapping[str, float] = field(default_factory=dict)
    risk: str = "none"
    caused_by: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("effect_id", "kind", "actor", "risk"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"EffectProposal.{name} must be non-empty")
            object.__setattr__(self, name, value.strip())
        if not isinstance(self.capabilities, frozenset):
            raise TypeError("EffectProposal.capabilities must be a frozenset")
        if any(not isinstance(value, str) or not value.strip() for value in self.capabilities):
            raise ValueError("EffectProposal.capabilities must contain non-empty strings")
        normalized_capabilities = tuple(value.strip() for value in self.capabilities)
        if len(set(normalized_capabilities)) != len(normalized_capabilities):
            raise ValueError(
                "EffectProposal.capabilities contains duplicate values after normalization",
            )
        object.__setattr__(self, "capabilities", frozenset(normalized_capabilities))
        if not isinstance(self.estimate, Mapping):
            raise TypeError("EffectProposal.estimate must be a mapping")
        estimate = dict(self.estimate)
        normalized_estimate: dict[str, float] = {}
        for bucket, amount in estimate.items():
            if not isinstance(bucket, str) or not bucket.strip():
                raise ValueError("EffectProposal.estimate must contain finite non-negative numbers")
            try:
                valid_amount = (
                    not isinstance(amount, bool)
                    and isinstance(amount, (int, float))
                    and math.isfinite(float(amount))
                    and amount >= 0
                )
            except (OverflowError, ValueError, TypeError):
                valid_amount = False
            if not valid_amount:
                raise ValueError("EffectProposal.estimate must contain finite non-negative numbers")
            normalized_bucket = bucket.strip()
            if normalized_bucket in normalized_estimate:
                raise ValueError(
                    "EffectProposal.estimate contains duplicate bucket names after normalization",
                )
            normalized_estimate[normalized_bucket] = float(amount)
        object.__setattr__(self, "estimate", normalized_estimate)
        if not isinstance(self.metadata, Mapping):
            raise TypeError("EffectProposal.metadata must be a mapping")
        metadata = _strict_json_copy(dict(self.metadata), "EffectProposal.metadata")
        object.__setattr__(self, "metadata", metadata)
        if self.caused_by is not None and (
            not isinstance(self.caused_by, str) or not self.caused_by.strip()
        ):
            raise ValueError("EffectProposal.caused_by must be non-empty or None")
        if self.caused_by is not None:
            object.__setattr__(self, "caused_by", self.caused_by.strip())

    def as_dict(self) -> dict[str, Any]:
        """Return a redaction-free structural view for host-side auditing.

        Metadata is intentionally copied, not interpreted: hosts must redact
        secrets before constructing a proposal and must apply their own export
        policy before sending this view outside the trusted process.
        """
        return {
            "effect_id": self.effect_id,
            "kind": self.kind,
            "actor": self.actor,
            "capabilities": sorted(self.capabilities),
            "estimate": dict(self.estimate),
            "risk": self.risk,
            "caused_by": self.caused_by,
            "metadata": deepcopy(dict(self.metadata)),
        }

    def claim_id(self, kind: EffectKind) -> str:
        """Return the stable EffectRow id used by a concrete Harness.

        Proposal ids are product identities and may be names such as
        ``email-send-42``.  The historical EffectRow schema intentionally
        uses ``call_<12 hex>`` / ``tool_<12 hex>`` ids, so non-conforming
        proposal ids are mapped deterministically rather than silently
        discarded or used as an invalid claim.
        """
        if not isinstance(kind, EffectKind):
            raise TypeError("kind must be EffectKind")
        prefix = "call" if kind is EffectKind.LLM_CALL else "tool"
        candidate = self.effect_id
        suffix = candidate[len(prefix) + 1:] if candidate.startswith(f"{prefix}_") else ""
        if len(suffix) == 12 and all(char in "0123456789abcdef" for char in suffix):
            return candidate
        digest = hashlib.sha256(
            f"lipas-effect:{kind.value}:{candidate}".encode("utf-8"),
        ).hexdigest()[:12]
        return f"{prefix}_{digest}"

    def claim_fields(self) -> dict[str, Any]:
        """Return proposal provenance fields carried by an Effect intent."""
        # Keep product metadata namespaced.  Flattening it into the intent
        # makes caller keys collide with reserved audit fields and creates
        # two representations of the same data.  The intent is a durable
        # evidence record, so reserved fields must be owned by the contract.
        fields: dict[str, Any] = {
            F_PROPOSAL_ID: self.effect_id,
            F_PROPOSAL_KIND: self.kind,
            F_ACTOR: self.actor,
            F_CAPABILITIES: sorted(self.capabilities),
            F_RISK: self.risk,
            F_ESTIMATE: dict(self.estimate),
            F_PROPOSAL_METADATA: deepcopy(dict(self.metadata)),
        }
        if self.caused_by is not None:
            fields[F_CAUSED_BY] = self.caused_by
        return fields


@dataclass(frozen=True)
class EffectDecision:
    """The Runtime's explicit admission result for an EffectProposal."""

    allowed: bool
    reason: str = "allowed"
    policy: str = "runtime"
    detail: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.allowed, bool):
            raise TypeError("EffectDecision.allowed must be bool")
        if not isinstance(self.reason, str) or not self.reason.strip():
            raise ValueError("EffectDecision.reason must be non-empty")
        if not isinstance(self.policy, str) or not self.policy.strip():
            raise ValueError("EffectDecision.policy must be non-empty")
        if not isinstance(self.detail, Mapping):
            raise TypeError("EffectDecision.detail must be a mapping")
        object.__setattr__(
            self,
            "detail",
            _strict_json_copy(dict(self.detail), "EffectDecision.detail"),
        )
        object.__setattr__(self, "reason", self.reason.strip())
        object.__setattr__(self, "policy", self.policy.strip())

    def as_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "reason": self.reason,
            "policy": self.policy,
            "detail": deepcopy(dict(self.detail)),
        }


@dataclass(frozen=True)
class EffectObservation:
    """What the world reported after an admitted effect was attempted."""

    effect_id: str
    status: str
    result: Any = None
    evidence: Mapping[str, Any] = field(default_factory=dict)
    claim_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.effect_id, str) or not self.effect_id.strip():
            raise ValueError("EffectObservation.effect_id must be non-empty")
        if not isinstance(self.status, str) or self.status not in {
            "succeeded", "failed", "uncertain", "rejected",
        }:
            raise ValueError("EffectObservation.status is invalid")
        object.__setattr__(self, "result", deepcopy(self.result))
        object.__setattr__(self, "effect_id", self.effect_id.strip())
        if not isinstance(self.evidence, Mapping):
            raise TypeError("EffectObservation.evidence must be a mapping")
        object.__setattr__(
            self,
            "evidence",
            _strict_json_copy(dict(self.evidence), "EffectObservation.evidence"),
        )
        if self.claim_id is not None and (
            not isinstance(self.claim_id, str) or not self.claim_id.strip()
        ):
            raise ValueError("EffectObservation.claim_id must be non-empty or None")
        if self.claim_id is not None:
            object.__setattr__(self, "claim_id", self.claim_id.strip())

    def as_dict(self) -> dict[str, Any]:
        return {
            "effect_id": self.effect_id,
            "status": self.status,
            "result": self.result,
            "evidence": deepcopy(dict(self.evidence)),
            "claim_id": self.claim_id,
        }


# =====================================================================
# Tag string values
# =====================================================================
# Historical strings — DO NOT change without a store migration.

TAG_EFFECT_INTENT   = "call_intent"
TAG_EFFECT_RESULT   = "call_result"
TAG_EFFECT_REJECTED = "call_rejected"


def _strict_json_copy(value: Any, name: str) -> Any:
    """Detach a structural value while rejecting non-JSON numbers/objects."""
    _validate_json_shape(value, name)
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
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


# =====================================================================
# Field-name constants
# =====================================================================
#
# Shared (intent / result / rejected) ─────────────────────────────────
F_EFFECT_ID    = "call_id"          # historical; opaque str
F_PROPOSAL_ID = "proposal_id"       # product-facing EffectProposal identity
F_PROPOSAL_KIND = "proposal_kind"   # host semantic kind, e.g. email_send
F_ACTOR = "actor"
F_CAPABILITIES = "capabilities"
F_RISK = "risk"
F_ESTIMATE = "estimate"
F_PROPOSAL_METADATA = "proposal_metadata"
F_PROVIDER_REQUEST_ID = "provider_request_id"
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
