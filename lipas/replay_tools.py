"""LIPAS · ToolReplayer — replay safety policy.

ToolReplayer mirrors the role of ReplayCursor (LLM-only) for tool
calls, but with a richer policy surface because tools have side
effects:

    ReplayCursor     : LLM nodes,  policy = always-substitute
    ToolReplayer     : tool nodes, policy = matrix(SideEffectClass x ReplayMode)

The decision matrix below is part of the public replay contract. Changes to
it require an explicit compatibility note because it decides whether replay
may touch the live world.

Integration
-----------
ToolReplayer is consumed by ToolHarness. The harness asks the replayer
for a ReplayDecision after tool resolution but before schema bind /
guards / budget gate. The decision drives one of four
operations:

    substitute  : harness folds mirrored intent + result + decision,
                  returns the recorded ToolResult, never executes.
    re-execute  : harness folds a decision claim, then proceeds with
                  the normal pipeline (schema -> guards -> budget ->
                  intent -> execute -> result -> spend).
    refuse      : harness folds a decision claim + intent + rejected
                  (reason "replay:refused_external"); raises
                  ReplayRefused to the caller.
    fail        : harness folds a decision claim and raises
                  ReplayMissing immediately; intent/result are not
                  folded for the call itself.

Independence from ReplayCursor
------------------------------
ReplayCursor and ToolReplayer share a single source EffectView but
never see each other's nodes (cursor walks LLM-kind, replayer walks
tool-kind). They are independently constructed and independently
attached to LLMHarness / ToolHarness.

Visibility window
-----------------
``frozen_max_seq`` controls which recorded nodes are visible to
``lookup`` / ``decide``. Filtering uses ``intent.seq`` (assigned by
ClaimStore.fold). For STRICT_TAPE, ``frozen_max_seq`` MUST be a
finite int (auto-captured at construction from the view's current
max seq if not passed). For BEST_EFFORT / LIVE_REROUTE, None means
"see the entire view".
"""
from __future__ import annotations

import logging
import warnings
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable, Literal, Mapping, Optional

TAG_REPLAY_DECISION = "replay_decision"

from .calculus import Claim
from .exceptions import LipasError
from .rows.effect import (
    EffectNode,
    EffectView,
    F_ARGUMENTS,
    F_DECLARED_SIDE_EFFECT,
    F_EFFECT_ID,
    F_OUTPUT,
    F_STATUS,
    F_TOOL_NAME,
)
from .tools import SideEffectClass, Tool


logger = logging.getLogger(__name__)


__all__ = [
    "ReplayMode",
    "ReplayDecision",
    "ReplayOperation",
    "ToolReplayer",
    # Decision-claim tag and field constants.
    "TAG_REPLAY_DECISION",
    "F_DECISION_OPERATION",
    "F_DECISION_REASON",
    "F_DECISION_DECLARED_CLASS",
    "F_DECISION_EFFECTIVE_CLASS",
    "F_DECISION_MODE",
    "F_DECISION_SOURCE_EFFECT_ID",
    "F_DECISION_TARGET_EFFECT_ID",
    "F_DECISION_FROZEN_MAX_SEQ",
    "F_DECISION_SESSION_INIT",
    # Error hierarchy.
    "LipasReplayError",
    "LipasReplayFatal",
    "LipasReplayRecoverable",
    "ReplayMissing",
    "ReplayMismatch",
    "ReplayRefused",
    "ReplayConfigError",
    "ReplayCrashPoint",
    "LipasReplayClassDowngradeError",
    # Warning hierarchy.
    "LipasReplayWarning",
    "LipasReplayClassUpgradeWarning",
    "LipasReplayClassDowngradeWarning",
    "LipasIdempotencyKeyMissingWarning",
    "LipasDangerousReplayWarning",
]


# =====================================================================
# Decision-claim schema
# =====================================================================

F_DECISION_OPERATION         = "operation"
F_DECISION_REASON            = "reason"
F_DECISION_DECLARED_CLASS    = "declared_class"
F_DECISION_EFFECTIVE_CLASS   = "effective_class"
F_DECISION_MODE              = "mode"
F_DECISION_SOURCE_EFFECT_ID  = "source_effect_id"
F_DECISION_TARGET_EFFECT_ID  = "target_effect_id"
F_DECISION_FROZEN_MAX_SEQ    = "frozen_max_seq"
F_DECISION_SESSION_INIT      = "session_init"


# =====================================================================
# Replay modes
# =====================================================================

class ReplayMode(str, Enum):
    """Replay session mode. One mode per replayer instance, lifetime-bound."""
    STRICT_TAPE  = "strict_tape"
    BEST_EFFORT  = "best_effort"
    LIVE_REROUTE = "live_reroute"


# =====================================================================
# SideEffectClass lattice
# =====================================================================
# Order:  PURE < READ_ONLY < IDEMPOTENT_WRITE < EXTERNAL_WRITE
_LATTICE_ORDER: tuple[SideEffectClass, ...] = (
    SideEffectClass.PURE,
    SideEffectClass.READ_ONLY,
    SideEffectClass.IDEMPOTENT_WRITE,
    SideEffectClass.EXTERNAL_WRITE,
)


def _rank(cls: SideEffectClass) -> int:
    return _LATTICE_ORDER.index(cls)


def _stricter(a: SideEffectClass, b: SideEffectClass) -> SideEffectClass:
    return a if _rank(a) >= _rank(b) else b


def _normalize_class(value: Any) -> SideEffectClass:
    """Coerce a stored field value into a SideEffectClass enum.

    Handles three on-the-wire shapes for forward compatibility:
      - SideEffectClass instance     (returned as-is)
      - string equal to .value       (e.g. "external_write")
      - string equal to .name        (e.g. "EXTERNAL_WRITE")
    """
    if isinstance(value, SideEffectClass):
        return value
    if isinstance(value, str):
        try:
            return SideEffectClass(value)
        except ValueError:
            try:
                return SideEffectClass[value]
            except KeyError:
                pass
    raise ValueError(f"cannot interpret {value!r} as SideEffectClass")


# =====================================================================
# Error / warning hierarchy
# =====================================================================

class LipasReplayError(LipasError):
    """Abstract base for all replay-time errors. Never raised directly."""


class LipasReplayFatal(LipasReplayError):
    """Fatal subtree: the session must abort."""


class LipasReplayRecoverable(LipasReplayError):
    """Recoverable subtree: the session may continue."""


class ReplayMissing(LipasReplayFatal):
    """STRICT_TAPE could not find a matching recorded result. (§4.2)"""


class ReplayMismatch(LipasReplayFatal):
    """re-execute hit schema drift between recorded and current. (§5.2)

    Reserved for v0.2; not raised by this module yet.
    """


class ReplayRefused(LipasReplayFatal):
    """refuse operation fired (e.g. EXTERNAL_WRITE under LIVE_REROUTE). (§4)"""


class ReplayConfigError(LipasReplayFatal):
    """STRICT_TAPE constructed without frozen_max_seq, etc. (§5.7)"""


class ReplayCrashPoint(LipasReplayFatal):
    """Source recorded an orphan intent; replay reproduces the crash. (§5.5)

    Reserved for v0.2; not raised by this module yet.
    """


class LipasReplayClassDowngradeError(LipasReplayFatal):
    """Recorded class > current class without explicit opt-in. (§5.1)"""


# Warnings live under LipasReplayRecoverable -> LipasReplayWarning so that
# `except LipasReplayFatal` cannot accidentally catch them.  They are
# emitted via warnings.warn (not raised); the class identity makes them
# pattern-matchable in tests.
class LipasReplayWarning(LipasReplayRecoverable, UserWarning):
    """Informational; emitted via warnings.warn, not raised."""


class LipasReplayClassUpgradeWarning(LipasReplayWarning):
    """Recorded class < current class. Always non-fatal. (§5.1)"""


class LipasReplayClassDowngradeWarning(LipasReplayWarning):
    """Recorded class > current class with allow_class_downgrade=True. (§5.1)"""


class LipasIdempotencyKeyMissingWarning(LipasReplayWarning):
    """LIVE_REROUTE of IDEMPOTENT_WRITE without a key; downgraded to substitute."""


class LipasDangerousReplayWarning(LipasReplayWarning):
    """LIVE_REROUTE of EXTERNAL_WRITE with allow_external_write=True."""


# No class may inherit from both fatal and recoverable branches. Run this
# from both Fatal and Recoverable.  Run at import time so packaging
# bugs surface before any session is constructed.
def _assert_disjoint_subtrees() -> None:
    fatal: set[type] = set()
    recov: set[type] = set()

    def _walk(root: type, into: set[type]) -> None:
        into.add(root)
        for sub in root.__subclasses__():
            _walk(sub, into)

    _walk(LipasReplayFatal, fatal)
    _walk(LipasReplayRecoverable, recov)
    overlap = fatal & recov
    if overlap:
        raise RuntimeError(
            f"replay error hierarchy is non-disjoint; classes appear under "
            f"both Fatal and Recoverable subtrees: "
            f"{sorted(c.__name__ for c in overlap)}"
        )


_assert_disjoint_subtrees()


# =====================================================================
# ReplayDecision
# =====================================================================

ReplayOperation = Literal["substitute", "re-execute", "refuse", "fail"]


@dataclass(frozen=True)
class ReplayDecision:
    """Outcome of ToolReplayer.decide. Pure data; no side effects."""

    operation: ReplayOperation
    reason: str
    declared_class: SideEffectClass
    effective_class: SideEffectClass
    # Populated iff a recorded match was found (regardless of operation).
    recorded_node: Optional[EffectNode] = None
    # Populated iff ``operation == "substitute"``.
    substitute_output: Any = None
    substitute_status: Optional[str] = None  # "ok" | "error"


# =====================================================================
# ToolReplayer
# =====================================================================

# Sentinel for "use the view's current max seq at construction".
_CAPTURE_VIEW_SEQ: Any = object()


@dataclass
class ToolReplayer:
    """Decides replay operations for tool calls against a recorded EffectView.

    Constructor arguments
    ---------------------
    view:
        The source EffectView to read recorded effects from.
    mode:
        ReplayMode for the entire session.
    allow_external_write:
        Required for LIVE_REROUTE of EXTERNAL_WRITE tools (and for
        re-executing EXTERNAL_WRITE tools whose recording is absent
        under BEST_EFFORT). Off by default.
    allow_class_downgrade:
        Permit recorded > current SideEffectClass mismatch. Off by
        default. Never overrides STRICT_TAPE's unconditional abort.
    frozen_max_seq:
        Highest claim seq considered visible. Required to be a finite
        int for STRICT_TAPE; may be None for BEST_EFFORT /
        LIVE_REROUTE (meaning "see the full view"). Defaults to
        capturing the view's current max seq at construction; pass an
        explicit int (or None) to override.

    The session-init claim is built by ``session_init_claim()`` and
    must be folded by the caller (ToolHarness does this on its first
    call). Per-call decision claims are built by ``decision_claim()``.
    """

    view: EffectView
    mode: ReplayMode = ReplayMode.STRICT_TAPE
    allow_external_write: bool = False
    allow_class_downgrade: bool = False
    frozen_max_seq: Optional[int] = field(default=_CAPTURE_VIEW_SEQ)  # type: ignore[assignment]

    # Internal: cached visible tool node list, populated lazily.
    _tool_nodes_cache: Optional[list[EffectNode]] = field(
        default=None, init=False, repr=False,
    )

    def __post_init__(self) -> None:
        if self.frozen_max_seq is _CAPTURE_VIEW_SEQ:
            self.frozen_max_seq = self._capture_view_max_seq()

        if self.mode is ReplayMode.STRICT_TAPE and self.frozen_max_seq is None:
            raise ReplayConfigError(
                "STRICT_TAPE requires a finite frozen_max_seq; pass an int "
                "explicitly, or use BEST_EFFORT / LIVE_REROUTE if you "
                "deliberately want a live view."
            )

        if self.allow_external_write and self.mode is ReplayMode.LIVE_REROUTE:
            warnings.warn(
                "LIVE_REROUTE with allow_external_write=True will re-execute "
                "EXTERNAL_WRITE tools against live systems.",
                LipasDangerousReplayWarning,
                stacklevel=2,
            )

    # ── session-init / per-call audit claims ──────────────────────

    def session_init_claim(self) -> Claim:
        """Build the per-session preamble decision claim.

        Caller folds this into the target store exactly once per
        session.
        """
        return Claim(
            tag=TAG_REPLAY_DECISION,
            fields={
                F_DECISION_SESSION_INIT:    True,
                F_DECISION_MODE:            self.mode.value,
                F_DECISION_FROZEN_MAX_SEQ:  self.frozen_max_seq,
                F_DECISION_OPERATION:       "session_init",
                F_DECISION_REASON:          "replay session preamble",
            },
            source="replay_tools.session_init",
        )

    def decision_claim(
        self,
        decision: ReplayDecision,
        *,
        target_effect_id: str,
    ) -> Claim:
        """Build the per-call decision claim.

        ``target_effect_id`` is the effect_id of the call in the
        *target* (current) run, distinct from the source effect_id of
        the recorded match (preserved as F_DECISION_SOURCE_EFFECT_ID).
        """
        source_eid = (
            decision.recorded_node.intent.fields.get(F_EFFECT_ID)
            if decision.recorded_node is not None
            else None
        )
        return Claim(
            tag=TAG_REPLAY_DECISION,
            fields={
                F_DECISION_SESSION_INIT:     False,
                F_DECISION_MODE:             self.mode.value,
                F_DECISION_OPERATION:        decision.operation,
                F_DECISION_REASON:           decision.reason,
                F_DECISION_DECLARED_CLASS:   decision.declared_class.value,
                F_DECISION_EFFECTIVE_CLASS:  decision.effective_class.value,
                F_DECISION_SOURCE_EFFECT_ID: source_eid,
                F_DECISION_TARGET_EFFECT_ID: target_effect_id,
            },
            source="replay_tools.decide",
        )

    # ── core API ──────────────────────────────────────────────────

    def lookup(
        self,
        tool: Tool,
        arguments: Mapping[str, Any],
    ) -> Optional[EffectNode]:
        """Find a recorded tool effect matching (tool.name, arguments).

        Match policy: exact equality on tool_name AND
        arguments. First-match-wins in fold order over the visible
        (frozen seq) window.

        Public so wrappers (e.g. a future ShadowToolReplayer) can
        reuse the match algorithm without re-implementing it.
        """
        for node in self._iter_visible_tool_nodes():
            intent = node.intent
            if intent is None:
                continue
            if intent.fields.get(F_TOOL_NAME) != tool.name:
                continue
            if intent.fields.get(F_ARGUMENTS) != arguments:
                continue
            return node
        return None

    def decide(
        self,
        tool: Tool,
        arguments: Mapping[str, Any],
    ) -> ReplayDecision:
        """Apply the replay decision matrix as a pure function.

        No folds, no I/O. Caller decides what to do with the
        operation; ToolHarness is the canonical consumer.
        """
        declared = tool.side_effect
        recorded = self.lookup(tool, arguments)

        # Class-mismatch resolution (only when there is a recording).
        # Compares *declared* classes between recorded run and current
        # run.  Must run BEFORE the observability-only downgrade: it is a
        # per-run policy on the resolved declared class, not a tape
        # mismatch — folding it in first would make every obs-only
        # EXTERNAL_WRITE tool look like a downgrade and abort.
        current_declared = declared
        if recorded is not None and recorded.intent is not None:
            recorded_class_raw = recorded.intent.fields.get(F_DECLARED_SIDE_EFFECT)
            if recorded_class_raw is not None:
                recorded_class = _normalize_class(recorded_class_raw)
                current_declared = self._resolve_class_mismatch(
                    recorded=recorded_class, current=declared,
                )

        # Observability-only downgrade applied AFTER mismatch
        # resolution.  The flag is preserved on the decision record so
        # audit reflects both declared and effective.
        observability_only = bool(getattr(tool, "observability_only", False))
        effective = (
            SideEffectClass.READ_ONLY
            if (observability_only
                and _rank(current_declared) > _rank(SideEffectClass.READ_ONLY))
            else current_declared
        )

        if recorded is not None:
            return self._present_branch(declared, effective, recorded)
        return self._absent_branch(declared, effective)

    # ── matrix dispatch ───────────────────────────────────────────

    def _present_branch(
        self,
        declared: SideEffectClass,
        effective: SideEffectClass,
        recorded: EffectNode,
    ) -> ReplayDecision:
        mode = self.mode

        if mode in (ReplayMode.STRICT_TAPE, ReplayMode.BEST_EFFORT):
            sub_output, sub_status = self._extract_substitute(recorded)
            return ReplayDecision(
                operation="substitute",
                reason=f"matrix.present.{mode.value}.{effective.value}",
                declared_class=declared,
                effective_class=effective,
                recorded_node=recorded,
                substitute_output=sub_output,
                substitute_status=sub_status,
            )

        # mode is LIVE_REROUTE
        if effective is SideEffectClass.EXTERNAL_WRITE:
            if not self.allow_external_write:
                return ReplayDecision(
                    operation="refuse",
                    reason="replay:refused_external",
                    declared_class=declared,
                    effective_class=effective,
                    recorded_node=recorded,
                )
            warnings.warn(
                "LIVE_REROUTE re-executing EXTERNAL_WRITE tool against live system",
                LipasDangerousReplayWarning,
                stacklevel=3,
            )

        if effective is SideEffectClass.IDEMPOTENT_WRITE:
            # Idempotency-key inspection is delegated to the tool itself.
            # Downgrade to substitute, the
            # safe behaviour the matrix prescribes.
            tool_name = (
                recorded.intent.fields.get(F_TOOL_NAME)
                if recorded.intent is not None else "?"
            )
            warnings.warn(
                f"LIVE_REROUTE of IDEMPOTENT_WRITE tool {tool_name!r} without "
                f"idempotency-key inspection; downgrading to substitute.",
                LipasIdempotencyKeyMissingWarning,
                stacklevel=3,
            )
            sub_output, sub_status = self._extract_substitute(recorded)
            return ReplayDecision(
                operation="substitute",
                reason="matrix.present.live_reroute.idem_no_key",
                declared_class=declared,
                effective_class=effective,
                recorded_node=recorded,
                substitute_output=sub_output,
                substitute_status=sub_status,
            )

        # PURE / READ_ONLY (or EXTERNAL_WRITE-with-opt-in falling through).
        return ReplayDecision(
            operation="re-execute",
            reason=f"matrix.present.live_reroute.{effective.value}",
            declared_class=declared,
            effective_class=effective,
            recorded_node=recorded,
        )

    def _absent_branch(
        self,
        declared: SideEffectClass,
        effective: SideEffectClass,
    ) -> ReplayDecision:
        mode = self.mode

        if mode is ReplayMode.STRICT_TAPE:
            return ReplayDecision(
                operation="fail",
                reason="matrix.absent.strict_tape",
                declared_class=declared,
                effective_class=effective,
            )

        # BEST_EFFORT or LIVE_REROUTE.
        if effective is SideEffectClass.EXTERNAL_WRITE:
            if not self.allow_external_write:
                return ReplayDecision(
                    operation="refuse",
                    reason="replay:refused_external_unrecorded",
                    declared_class=declared,
                    effective_class=effective,
                )
            warnings.warn(
                "Replay re-executing EXTERNAL_WRITE tool with no recorded baseline.",
                LipasDangerousReplayWarning,
                stacklevel=3,
            )

        if effective is SideEffectClass.IDEMPOTENT_WRITE:
            warnings.warn(
                "Replay re-executing IDEMPOTENT_WRITE tool with no recorded "
                "baseline; no idempotency-key inspection in v1.",
                LipasIdempotencyKeyMissingWarning,
                stacklevel=3,
            )

        return ReplayDecision(
            operation="re-execute",
            reason=f"matrix.absent.{mode.value}.{effective.value}",
            declared_class=declared,
            effective_class=effective,
        )

    # ── class-mismatch resolution (§5.1) ──────────────────────────

    def _resolve_class_mismatch(
        self,
        *,
        recorded: SideEffectClass,
        current: SideEffectClass,
    ) -> SideEffectClass:
        if recorded == current:
            return current

        if _rank(recorded) < _rank(current):
            # Upgrade: recorded was looser, current is stricter. Always safe.
            warnings.warn(
                f"SideEffectClass upgrade between runs: "
                f"recorded={recorded.value} current={current.value}; "
                f"using current (stricter).",
                LipasReplayClassUpgradeWarning,
                stacklevel=4,
            )
            return _stricter(recorded, current)

        # Downgrade: recorded was stricter, current is looser. Suspicious.
        if self.mode is ReplayMode.STRICT_TAPE:
            raise LipasReplayClassDowngradeError(
                f"STRICT_TAPE rejects SideEffectClass downgrade: "
                f"recorded={recorded.value} current={current.value}"
            )
        if not self.allow_class_downgrade:
            raise LipasReplayClassDowngradeError(
                f"SideEffectClass downgrade detected (recorded={recorded.value} "
                f"current={current.value}); pass allow_class_downgrade=True "
                f"to acknowledge."
            )
        warnings.warn(
            f"SideEffectClass downgrade allowed by flag: "
            f"recorded={recorded.value} current={current.value}; "
            f"using stricter (recorded).",
            LipasReplayClassDowngradeWarning,
            stacklevel=4,
        )
        return _stricter(recorded, current)

    # ── substitution payload extraction ───────────────────────────

    @staticmethod
    def _extract_substitute(
        node: Optional[EffectNode],
    ) -> tuple[Any, Optional[str]]:
        """Pull (output, status) from a recorded result for substitution.

        Caller (ToolHarness) wraps these into a tool_result block and
        folds a mirrored effect_result claim. We do not synthesize the
        wrapping shape here — that is harness territory.
        """
        if node is None or node.result is None:
            return None, None
        out    = node.result.fields.get(F_OUTPUT)
        status = node.result.fields.get(F_STATUS)
        return out, status

    # ── visible-window iteration ──────────────────────────────────

    def _iter_visible_tool_nodes(self) -> Iterable[EffectNode]:
        """Yield tool-kind effect nodes within the frozen seq window.

        Filtering uses ``intent.seq`` (assigned by ClaimStore.fold).
        Nodes whose intent has no seq (-1, never folded) are excluded
        defensively. When ``frozen_max_seq is None`` no filtering is
        applied.
        """
        if self._tool_nodes_cache is None:
            self._tool_nodes_cache = list(self.view.tool_nodes())

        cap = self.frozen_max_seq
        for node in self._tool_nodes_cache:
            if cap is not None:
                seq = getattr(node.intent, "seq", -1)
                if seq < 0 or seq > cap:
                    continue
            yield node

    def _capture_view_max_seq(self) -> Optional[int]:
        """Best-effort capture of the view's current max claim seq.

        Walks the visible tool nodes (intent + result + rejection) for
        the highest seq.  Returns None if the view is empty or no node
        has a non-negative seq — STRICT_TAPE then catches the None in
        __post_init__ and raises ReplayConfigError.
        """
        max_seen: int = -1
        for node in self.view.tool_nodes():
            for part in (node.intent, node.result, node.rejection):
                if part is None:
                    continue
                seq = getattr(part, "seq", -1)
                if seq > max_seen:
                    max_seen = seq
        return max_seen if max_seen >= 0 else None
