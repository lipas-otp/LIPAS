"""
LIPAS · LLM Harness (P2.4 → P2.6 → P2.7 → P2.8 → P3.0).

The harness is the smallest unit that combines retry behavior (P2.3)
with auditable, idempotent record-keeping over the ClaimStore.  One
harness call produces, depending on path:

  Replay short-circuit (P2.7):
    No claims folded.  Recorded Reply returned directly.

  Pre-flight rejection paths (P2.6 / P2.8):
    1. effect_intent (kind=llm_call)
    2. effect_rejected (kind=llm_call, reason ∈ {"budget_exhausted",
                        "guard:<slug>"})

  Normal path:
    1. effect_intent (kind=llm_call)
    2. call_with_retry()       drives the adapter
    3. effect_result (kind=llm_call, F_REPLY always present)
    4. resource_spent ×N       routed to TAG_BUDGET_OVERRUN if folding
                               a normal spend would breach budget.
                               Recorded on success, AND on errors that
                               carry non-zero Usage (provider billed for
                               partial output before failing).

Pre-flight order (top of call):
    replay → budget → guards → record_intent → live adapter

P3.0 changes
------------
- Folds carry ``F_KIND = EffectKind.LLM_CALL.value`` on intent /
  result / rejected.  Tool-call folding is the parallel ToolHarness's
  responsibility (P3.1); both write into the same effect-namespace
  schema.
- Guards now receive ``LLMTarget(request)`` rather than a bare
  ``Request``.  See ``lipas.guard``.

Replay short-circuits BEFORE any pre-flight: the original run already
passed those gates, and re-evaluating against (possibly fresh) state
either always-passes (a tape of failures would be lost to "plenty of
budget") or always-fails (a tape of successes blocked by a stale
budget snapshot).  Either is wrong.  Replay is a tape, not a policy.
"""
from __future__ import annotations

import logging
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Callable, Awaitable, AsyncIterator

from lipas.adapter import Reply, Request, ResourceEstimate, Usage
from lipas.adapter.errors import (
    DEFAULT_POLICY, ErrorKind, RetryPolicy, classify,
)
from lipas.adapter.protocol import LLMAdapter, StreamProtocolError
from lipas.adapter.types import Done, StreamEvent

from lipas.calculus import Claim
from lipas.effect import EffectKind, LLMTarget
from lipas.guard import Guard, GuardVerdict
from lipas.replay import ReplayCursor
from lipas.retry import RetryOutcome, call_with_retry
from lipas.rows import RowSet
from lipas.rows.capability import (
    CapabilityRow,
    F_AMOUNT, F_BUCKET as CAP_F_BUCKET,
    TAG_BUDGET_OVERRUN, TAG_RESOURCE_SPENT,
)
from lipas.rows.effect import (
    F_ATTEMPTS, F_CAUSED_BY, F_COMPENSATES, F_DETAIL, F_EFFECT_ID, F_ERROR,
    F_KIND, F_MODEL, F_REASON, F_REPLY, F_REQUEST, F_STATUS,
    TAG_EFFECT_INTENT, TAG_EFFECT_REJECTED, TAG_EFFECT_RESULT,
)


__all__ = [
    "LLMHarness",
    "BudgetRejection",
    "GuardRejection",
    "default_bucket_extractor",
    "BucketExtractor",
]

logger = logging.getLogger(__name__)


BucketExtractor = Callable[[Reply], dict[str, float]]


def default_bucket_extractor(reply: Reply) -> dict[str, float]:
    u = reply.usage
    out: dict[str, float] = {}
    if u.input:
        out["tokens_in"] = float(u.input)
    if u.output:
        out["tokens_out"] = float(u.output)
    return out


# =====================================================================
# Pre-flight outcomes
# =====================================================================

@dataclass(frozen=True)
class BudgetRejection:
    bucket:   str
    spent:    float
    estimate: float
    limit:    float

    @property
    def reason(self) -> str:
        return "budget_exhausted"

    def as_detail(self) -> dict:
        return {
            "bucket":   self.bucket,
            "spent":    self.spent,
            "estimate": self.estimate,
            "limit":    self.limit,
        }


@dataclass(frozen=True)
class GuardRejection:
    """A guard-side denial.

    ``reason`` is namespaced as ``"guard:<slug>"`` so effect_rejected
    consumers can grep guard denials cleanly without parsing detail.
    """
    guard_name: str
    verdict: GuardVerdict

    @property
    def reason(self) -> str:
        return f"guard:{self.verdict.reason}"

    def as_detail(self) -> dict:
        return {
            "guard":  self.guard_name,
            "reason": self.verdict.reason,
            **self.verdict.detail,
        }


# =====================================================================
# Harness
# =====================================================================

@dataclass
class LLMHarness:
    """Orchestrates one LLM call as a sequence of folded claims.

    Concurrent calls are safe iff the underlying ClaimStore serialises
    folds atomically. Without that, two concurrent calls can each pass
    budget pre-flight against a stale projection and both record as
    resource_spent rather than budget_overrun.


    Attributes
    ----------
    adapter:
        The LLMAdapter to drive (P2.1).
    rowset:
        The RowSet that owns the ClaimStore.
    retry_policy:
        Forwarded to call_with_retry.
    bucket_extractor:
        Maps a successful Reply to {bucket: amount}.
    guards (P2.8 / P3.0):
        Sequence of Guards consulted after the budget gate.  Receive
        ``LLMTarget(request)`` as their target.  First Deny wins.
    replay_cursor (P2.7):
        Optional ReplayCursor.  When set and not exhausted, call()
        returns recorded Replies WITHOUT folding any new claims.
    """

    adapter: LLMAdapter
    rowset: RowSet
    retry_policy: Mapping[ErrorKind, RetryPolicy] = field(
        default_factory=lambda: DEFAULT_POLICY,
    )
    bucket_extractor: BucketExtractor = field(
        default=default_bucket_extractor,
    )
    guards: Sequence[Guard] = ()
    replay_cursor: ReplayCursor | None = None

    # ── public API ─────────────────────────────────────────────

    async def call(
        self,
        request: Request,
        *,
        compensates: str | None = None,
        caused_by: str | None = None,
        _replay_call_id: str | None = None,
    ) -> Reply:
        """Execute one LLM call.

        See module docstring for the full pre-flight order.
        """
        # 0. Replay short-circuit (P2.7). NO folds happen on this path.
        if self.replay_cursor is not None and not self.replay_cursor.exhausted():
            return self.replay_cursor.advance(request)

        effect_id = _replay_call_id or f"call_{uuid.uuid4().hex[:12]}"
        target = LLMTarget(request=request)

        # Compute estimate at most once per call, lazily.
        _estimate_cache: list[ResourceEstimate] = []
        async def _estimate() -> ResourceEstimate:
            if not _estimate_cache:
                _estimate_cache.append(await self.adapter.estimate_cost(request))
            return _estimate_cache[0]

        # 1. Pre-flight: budget (P2.6).
        bud_rej = await self._preflight_budget(request, _estimate)
        if bud_rej is not None:
            return self._record_rejection(
                effect_id=effect_id, request=request,
                compensates=compensates, caused_by=caused_by, rejection=bud_rej,
            )

        # 2. Pre-flight: guards (P2.8 / P3.0).
        guard_rej = await self._preflight_guards(target, _estimate)
        if guard_rej is not None:
            return self._record_rejection(
                effect_id=effect_id, request=request,
                compensates=compensates, caused_by=caused_by, rejection=guard_rej,
            )

        # 3. Record intent.
        self._fold_intent(effect_id, request, compensates, caused_by)

        # 4. Drive the adapter.
        outcome: RetryOutcome = await call_with_retry(
            self.adapter, request, policy_table=self.retry_policy,
        )
        reply = outcome.reply

        # 5. Record terminal result.
        self._fold_result(effect_id, outcome)

        # 6. Resource accounting — success only.
        if reply.stop_reason != "error" or reply.usage.input or reply.usage.output:
            self._fold_spend(effect_id, reply)

        return reply

    async def stream(
        self, request: Request, *, compensates: str | None = None,
        caused_by: str | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """Caller-facing event stream with the same audit boundary as ``call``.

        A streamed attempt is intentionally not retried after any event has
        been exposed: token delivery cannot be rolled back. Provider errors
        still end in a durable error ``Reply`` inside ``Done``.
        """
        if self.replay_cursor is not None and not self.replay_cursor.exhausted():
            yield Done(self.replay_cursor.advance(request))
            return
        effect_id = f"call_{uuid.uuid4().hex[:12]}"
        target = LLMTarget(request=request)
        cache: list[ResourceEstimate] = []
        async def estimate() -> ResourceEstimate:
            if not cache: cache.append(await self.adapter.estimate_cost(request))
            return cache[0]
        rejection = await self._preflight_budget(request, estimate)
        if rejection is None:
            rejection = await self._preflight_guards(target, estimate)
        if rejection is not None:
            yield Done(self._record_rejection(effect_id=effect_id, request=request, compensates=compensates, caused_by=caused_by, rejection=rejection))
            return
        self._fold_intent(effect_id, request, compensates, caused_by)
        async for event in self.adapter.stream(request):
            yield event
            if isinstance(event, Done):
                outcome = RetryOutcome(reply=event.reply, attempts=1)
                self._fold_result(effect_id, outcome)
                if event.reply.stop_reason != "error" or event.reply.usage.input or event.reply.usage.output:
                    self._fold_spend(effect_id, event.reply)
                return
        raise StreamProtocolError("adapter stream ended without terminal Done")

    # ── pre-flight: budget (P2.6) ──────────────────────────────

    async def _preflight_budget(
        self, request: Request,
        estimate_fn: Callable[[], Awaitable[ResourceEstimate]],
    ) -> BudgetRejection | None:
        cap = self._capability_row()
        if cap is None or not cap.budgets:
            return None

        estimate = await estimate_fn()

        upper: dict[str, float] = {
            "tokens_in":  float(estimate.input_tokens),
            "tokens_out": float(estimate.max_output_tokens),
            "cost_usd":   float(estimate.max_cost_usd),
        }

        proj = cap.project(self.rowset.store)
        for bucket, est_amount in upper.items():
            if bucket not in cap.budgets:
                continue
            info = proj[bucket]
            if info["spent"] + est_amount > info["limit"]:
                return BudgetRejection(
                    bucket=bucket,
                    spent=info["spent"],
                    estimate=est_amount,
                    limit=info["limit"],
                )
        return None

    def _capability_row(self) -> CapabilityRow | None:
        for r in self.rowset.rows:
            if isinstance(r, CapabilityRow):
                return r
        return None

    # ── pre-flight: guards (P2.8 / P3.0) ───────────────────────

    async def _preflight_guards(
        self, target: LLMTarget,
        estimate_fn: Callable[[], Awaitable[ResourceEstimate]],
    ) -> GuardRejection | None:
        if not self.guards:
            return None

        # Estimate is computed lazily and shared across guards.
        estimate: ResourceEstimate | None = None
        estimate_computed = False

        for g in self.guards:
            if not estimate_computed:
                estimate = await estimate_fn()
                estimate_computed = True

            verdict = await g.check(target, estimate)
            if not isinstance(verdict, GuardVerdict):
                raise TypeError(
                    f"guard {g.name!r} returned {type(verdict).__name__}, "
                    f"expected GuardVerdict"
                )
            if not verdict.allowed:
                return GuardRejection(guard_name=g.name, verdict=verdict)
        return None

    # ── fold helpers ───────────────────────────────────────────

    def _fold_intent(
        self,
        effect_id: str,
        request: Request,
        compensates: str | None,
        caused_by: str | None,
    ) -> None:
        intent_fields: dict = {
            F_EFFECT_ID: effect_id,
            F_KIND:      EffectKind.LLM_CALL.value,
            F_MODEL:     getattr(request, "model", None),
            F_REQUEST:   request,
        }
        if compensates is not None:
            intent_fields[F_COMPENSATES] = compensates
        if caused_by is not None:
            intent_fields[F_CAUSED_BY] = caused_by
        self.rowset.fold(Claim(
            tag=TAG_EFFECT_INTENT,
            fields=intent_fields,
            source="harness.call",
        ))

    def _fold_result(self, effect_id: str, outcome: RetryOutcome) -> None:
        reply = outcome.reply
        result_fields: dict = {
            F_EFFECT_ID: effect_id,
            F_KIND:      EffectKind.LLM_CALL.value,
            F_ATTEMPTS:  outcome.attempts,
            F_REPLY:     reply,            # P2.7: always present.
        }
        if reply.stop_reason == "error":
            kind = classify(reply)
            result_fields[F_STATUS] = "error"
            result_fields[F_ERROR] = {
                "kind":   kind.value,
                "detail": reply.error_detail,
            }
        else:
            result_fields[F_STATUS] = "ok"

        self.rowset.fold(Claim(
            tag=TAG_EFFECT_RESULT,
            fields=result_fields,
            source="harness.call",
        ))

    def _fold_spend(self, effect_id: str, reply: Reply) -> None:
        buckets = self.bucket_extractor(reply)
        if not buckets:
            return

        cap = self._capability_row()
        proj = cap.project(self.rowset.store) if cap is not None else None

        for bucket, amount in buckets.items():
            if amount <= 0:
                continue

            is_overrun = (
                cap is not None
                and proj is not None
                and bucket in cap.budgets
                and proj[bucket]["spent"] + amount > proj[bucket]["limit"]
            )

            if is_overrun:
                logger.warning(
                    "lipas: in-band budget overrun bucket=%r amount=%s "
                    "effect_id=%s — recorded under %s.  Investigate adapter "
                    "estimate accuracy.",
                    bucket, amount, effect_id, TAG_BUDGET_OVERRUN,
                )
                self.rowset.fold(Claim(
                    tag=TAG_BUDGET_OVERRUN,
                    fields={
                        CAP_F_BUCKET:  bucket,
                        F_AMOUNT:      amount,
                        F_EFFECT_ID:   effect_id,
                    },
                    source="harness.call",
                ))
            else:
                self.rowset.fold(Claim(
                    tag=TAG_RESOURCE_SPENT,
                    fields={
                        CAP_F_BUCKET:  bucket,
                        F_AMOUNT:      amount,
                        F_EFFECT_ID:   effect_id,
                    },
                    source="harness.call",
                ))

    # ── rejection path (shared P2.6 / P2.8) ────────────────────

    def _record_rejection(
        self,
        *,
        effect_id: str,
        request: Request,
        compensates: str | None,
        caused_by: str | None,
        rejection: BudgetRejection | GuardRejection,
    ) -> Reply:
        """Fold effect_intent + effect_rejected; return synthesized Reply."""
        self._fold_intent(effect_id, request, compensates, caused_by)
        self.rowset.fold(Claim(
            tag=TAG_EFFECT_REJECTED,
            fields={
                F_EFFECT_ID: effect_id,
                F_KIND:      EffectKind.LLM_CALL.value,
                F_REASON:    rejection.reason,
                F_DETAIL:    rejection.as_detail(),
            },
            source="harness.call",
        ))
        return self._synthesize_rejection_reply(request, rejection)

    @staticmethod
    def _synthesize_rejection_reply(
        request: Request,
        rejection: BudgetRejection | GuardRejection,
    ) -> Reply:
        return Reply(
            content=(),
            usage=Usage(),
            stop_reason="error",
            model=request.model,
            error_detail={
                "type":   "preflight_rejection",
                "reason": rejection.reason,
                **rejection.as_detail(),
            },
        )
