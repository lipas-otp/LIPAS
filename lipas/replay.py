"""
LIPAS · P2.7 — Replay (P3.0-aware).

Two complementary mechanisms for re-running a recorded LLM session:

  1. ``ReplayCursor`` — harness-level. Walks recorded LLM-kind
     terminal claims in fold order; the harness advances it on each
     call() and SKIPS folds entirely.  Result: re-running deterministic
     agent code produces identical Replies with no new claims in the
     store.  Use for: "did my agent code change behavior against a
     fixed input log?"

  2. ``ReplayingAdapter`` — adapter-level. A drop-in LLMAdapter that
     yields recorded Replies via Done events.  The harness drives it
     normally and folds fresh intent/result/spend claims into a NEW
     store.  Use for: "rerun this session into a fresh audit trail
     (e.g. with a different RowSet / different budgets to see how the
     same calls would have been gated)."

P3.0 — kind filtering
---------------------
Tool-call nodes (``EffectKind.TOOL_CALL``) in the source view are
SILENTLY SKIPPED by both mechanisms.  Tool replay needs different
machinery (side-effect-aware: re-execute PURE/READ_ONLY/IDEMPOTENT_WRITE,
substitute output for EXTERNAL_WRITE, etc.) which lives in the
ToolHarness (P3.1).  This module is LLM-only by design; mixing kinds
would either require the cursor to know about both protocols or
silently corrupt one of them.

Replay does NOT re-run pre-flight (budget, guard).  Re-evaluation
against new state is meaningless: the original run was admitted under
the original state, and the cursor/adapter is a faithful tape, not a
new policy decision.

Mismatch handling
-----------------
``ReplayCursor.strict_match=True`` compares each call's
``request.model`` and ``request.system`` against the recorded intent.
On mismatch the cursor raises ``ReplayMismatch``.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from decimal import Decimal
from typing import AsyncIterator, ClassVar, Sequence

from .adapter import (
    Done, Reply, Request, ResourceEstimate, StreamEvent, Usage,
)
from .calculus import Claim
from .effect import EffectKind
from .rows.effect import (
    EffectView, F_DETAIL, F_MODEL, F_REASON, F_REPLY, F_REQUEST,
)


__all__ = [
    "ReplayCursor",
    "ReplayingAdapter",
    "ReplayExhausted",
    "ReplayMismatch",
]


# =====================================================================
# Exceptions
# =====================================================================

class ReplayExhausted(RuntimeError):
    """The replay tape has no more recorded entries."""


class ReplayMismatch(RuntimeError):
    """A replay request does not match the recorded intent."""


# =====================================================================
# Recorded entries
# =====================================================================

@dataclass(frozen=True)
class _RecordedCall:
    """One LLM-kind terminal node materialized for replay."""
    effect_id: str
    reply: Reply
    recorded_request: Request | None
    is_rejection: bool


def _entries_from_view(view: EffectView) -> tuple[_RecordedCall, ...]:
    """Project an EffectView into the ordered LLM replay tape.

    Tool-kind nodes are silently skipped — see module docstring.
    Orphans (no terminal) are also skipped: nothing to replay.
    """
    entries: list[_RecordedCall] = []
    for eid, node in view.nodes.items():
        if node.kind is not EffectKind.LLM_CALL:
            continue  # not our concern; tool replay is elsewhere

        recorded_req = node.intent.fields.get(F_REQUEST)
        recorded_req = recorded_req if isinstance(recorded_req, Request) else None

        if node.result is not None:
            reply = node.result.fields.get(F_REPLY)
            if not isinstance(reply, Reply):
                # Bypass-induced corruption; skip rather than crash.
                continue
            entries.append(_RecordedCall(
                effect_id=eid,
                reply=reply,
                recorded_request=recorded_req,
                is_rejection=False,
            ))
        elif node.rejection is not None:
            entries.append(_RecordedCall(
                effect_id=eid,
                reply=_synthesize_rejection_reply(node.intent, node.rejection),
                recorded_request=recorded_req,
                is_rejection=True,
            ))
        # orphan: skip
    return tuple(entries)


def _synthesize_rejection_reply(intent: Claim, rejection: Claim) -> Reply:
    """Reconstruct the rejection Reply from intent + effect_rejected claim.

    Mirrors ``LLMHarness._synthesize_rejection_reply`` so that replayed
    rejections are byte-equivalent to fresh ones.
    """
    model = intent.fields.get(F_MODEL) or "?"
    reason = rejection.fields.get(F_REASON) or "unknown"
    detail = rejection.fields.get(F_DETAIL) or {}
    return Reply(
        content=[],
        usage=Usage(),
        stop_reason="error",
        model=model,
        error_detail={
            "type":   "preflight_rejection",
            "reason": reason,
            **detail,
        },
    )


# =====================================================================
# ReplayCursor — harness-level, no-fold replay
# =====================================================================

@dataclass
class ReplayCursor:
    """Sequential cursor over a recorded session's LLM-kind terminal nodes."""

    _entries: tuple[_RecordedCall, ...]
    strict_match: bool = False
    _pos: int = 0

    @classmethod
    def from_view(
        cls, view: EffectView, *, strict_match: bool = False,
    ) -> "ReplayCursor":
        return cls(_entries=_entries_from_view(view), strict_match=strict_match)

    @classmethod
    def from_store(cls, store, *, strict_match: bool = False) -> "ReplayCursor":
        from .rows.effect import EffectRow
        view = EffectRow().project(store)
        return cls.from_view(view, strict_match=strict_match)

    def __len__(self) -> int:
        return len(self._entries) - self._pos

    def exhausted(self) -> bool:
        return self._pos >= len(self._entries)

    def peek(self) -> _RecordedCall | None:
        return None if self.exhausted() else self._entries[self._pos]

    def advance(self, request: Request) -> Reply:
        if self.exhausted():
            raise ReplayExhausted(
                f"replay cursor exhausted at position {self._pos}; "
                f"caller must check exhausted() before advance()."
            )
        entry = self._entries[self._pos]
        if self.strict_match:
            self._validate_match(entry, request)
        self._pos += 1
        # Callers receive a value, never a mutable reference into the recorded
        # tape. This keeps later replay calls independent.
        return deepcopy(entry.reply)

    def _validate_match(self, entry: _RecordedCall, request: Request) -> None:
        rec = entry.recorded_request
        if rec is None:
            return
        if rec.model != request.model:
            raise ReplayMismatch(
                f"replay model mismatch at effect_id={entry.effect_id!r}: "
                f"recorded={rec.model!r}, current={request.model!r}"
            )
        if rec.system != request.system:
            raise ReplayMismatch(
                f"replay system mismatch at effect_id={entry.effect_id!r}: "
                f"recorded len={len(rec.system)}, current len={len(request.system)}"
            )

    def reset(self) -> None:
        self._pos = 0


# =====================================================================
# ReplayingAdapter — adapter-level, fold-fresh-trail replay
# =====================================================================

@dataclass
class ReplayingAdapter:
    """LLMAdapter that yields recorded Replies via Done events."""

    name: ClassVar[str] = "replay"

    _entries: tuple[_RecordedCall, ...]
    _pos: int = 0

    @classmethod
    def from_view(cls, view: EffectView) -> "ReplayingAdapter":
        return cls(_entries=_entries_from_view(view))

    @classmethod
    def from_store(cls, store) -> "ReplayingAdapter":
        from .rows.effect import EffectRow
        return cls.from_view(EffectRow().project(store))

    @classmethod
    def from_replies(cls, replies: Sequence[Reply]) -> "ReplayingAdapter":
        entries = tuple(
            _RecordedCall(
                effect_id=f"call_{i:012x}",
                reply=r,
                recorded_request=None,
                is_rejection=(r.stop_reason == "error"
                              and isinstance(r.error_detail, dict)
                              and r.error_detail.get("type") == "preflight_rejection"),
            )
            for i, r in enumerate(replies)
        )
        return cls(_entries=entries)

    async def estimate_cost(self, request: Request) -> ResourceEstimate:
        if self._pos >= len(self._entries):
            return ResourceEstimate(
                model=request.model,
                input_tokens=0,
                max_output_tokens=request.max_tokens,
                max_cost_usd=Decimal("0"),
            )
        entry = self._entries[self._pos]
        u = entry.reply.usage
        return ResourceEstimate(
            model=request.model,
            input_tokens=u.input,
            max_output_tokens=u.output or request.max_tokens,
            max_cost_usd=Decimal("0"),
        )

    async def stream(self, request: Request) -> AsyncIterator[StreamEvent]:
        if self._pos >= len(self._entries):
            raise ReplayExhausted(
                f"ReplayingAdapter tape exhausted at position {self._pos}"
            )
        entry = self._entries[self._pos]
        self._pos += 1
        yield Done(reply=deepcopy(entry.reply))
