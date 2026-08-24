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
import hashlib
import uuid
from copy import deepcopy
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Awaitable, AsyncIterator

from lipas.adapter import Reply, Request, ResourceEstimate, UnknownModelError, Usage
from lipas.adapter.errors import (
    DEFAULT_POLICY, ErrorKind, RetryPolicy, classify,
)
from lipas.adapter.protocol import LLMAdapter, StreamProtocolError
from lipas.adapter.types import Done, StreamEvent

from lipas.calculus import Claim
from lipas.effect import EffectKind, LLMTarget
from lipas.exceptions import OrphanedEffectError
from lipas.guard import Guard, GuardVerdict
from lipas.replay import ReplayCursor
from lipas.retry import RetryOutcome, call_with_retry
from lipas.serialization.codec import encode, make_default_codec_registry
from lipas.rows import RowSet
from lipas.rows.capability import (
    CapabilityRow,
    F_AMOUNT, F_BUCKET as CAP_F_BUCKET,
    TAG_BUDGET_OVERRUN, TAG_RESOURCE_SPENT,
)
from lipas.rows.effect import (
    EffectRow,
    F_ATTEMPTS, F_CAUSED_BY, F_COMPENSATES, F_DETAIL, F_EFFECT_ID, F_ERROR,
    F_KIND, F_MODEL, F_REASON, F_REPLY, F_REQUEST, F_STATUS, F_TOTAL_USAGE,
    F_PROVIDER_REQUEST_ID,
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
_RECOVERY_CODECS = make_default_codec_registry()


BucketExtractor = Callable[[Reply], dict[str, float]]


def default_bucket_extractor(reply: Reply) -> dict[str, float]:
    u = reply.usage
    out: dict[str, float] = {}
    total_input = u.input + u.cache_read + u.cache_write
    if total_input:
        out["tokens_in"] = float(total_input)
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
        effect_id: str | None = None,
        _replay_call_id: str | None = None,
        stream_sink: Callable[[StreamEvent], Awaitable[None] | None] | None = None,
    ) -> Reply:
        """Execute one LLM call.

        See module docstring for the full pre-flight order.
        """
        # 0. Replay short-circuit (P2.7). NO folds happen on this path.
        if self.replay_cursor is not None and not self.replay_cursor.exhausted():
            return self.replay_cursor.advance(request)

        if effect_id is not None and _replay_call_id is not None:
            raise ValueError("pass effect_id or _replay_call_id, not both")
        effect_id = effect_id or _replay_call_id or f"call_{uuid.uuid4().hex[:12]}"
        if request.request_id is None:
            request = replace(request, request_id=effect_id)
        recovered = self._recover_existing(effect_id, request)
        if recovered is not None:
            return recovered
        # Guards are observers. Give them an isolated copy so an accidental
        # mutation in policy code cannot rewrite the request that is admitted
        # or sent to a provider.
        target = LLMTarget(request=deepcopy(request))

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
            self.adapter,
            request,
            policy_table=self.retry_policy,
            on_event=stream_sink,
            on_attempt=lambda attempt, reply, kind: self._fold_attempt(
                effect_id, request, attempt, reply, kind,
            ),
        )
        reply = outcome.reply

        # 5. Record terminal result.
        self._fold_result(effect_id, outcome)

        # 6. Resource accounting — success only.
        if reply.stop_reason != "error" or outcome.billed_usage.total:
            self._fold_spend(
                effect_id,
                replace(reply, usage=outcome.billed_usage),
                pricing_model=request.model,
            )

        return reply

    def reconcile_orphan(
        self,
        effect_id: str,
        *,
        reply: Reply | None = None,
        error: Mapping[str, Any] | None = None,
        attempts: int = 1,
        total_usage: Usage | None = None,
    ) -> Reply:
        """Close an intent-only model Effect after provider reconciliation.

        A timeout or process interruption cannot establish whether a model
        provider billed or completed a request.  Recovery code must therefore
        supply either the provider's observed :class:`Reply` or an explicit
        error observation; this method records that terminal fact without
        issuing another model request.
        """
        if not isinstance(effect_id, str) or not effect_id.strip():
            raise ValueError("effect_id must be a non-empty string")
        if reply is not None and not isinstance(reply, Reply):
            raise TypeError("reply must be Reply or None")
        if error is not None and not isinstance(error, Mapping):
            raise TypeError("error must be a mapping or None")
        if reply is not None and error is not None:
            raise ValueError("pass reply or error, not both")
        if (
            isinstance(attempts, bool)
            or not isinstance(attempts, int)
            or attempts < 1
        ):
            raise ValueError("attempts must be a positive int")
        if total_usage is not None and not isinstance(total_usage, Usage):
            raise TypeError("total_usage must be Usage or None")
        effect_row = next(
            (row for row in self.rowset.rows if isinstance(row, EffectRow)), None,
        )
        if effect_row is None:
            raise OrphanedEffectError(f"LLM effect {effect_id!r} is not recorded")
        node = effect_row.project(self.rowset.store).nodes.get(effect_id)
        if node is None or node.intent is None:
            raise OrphanedEffectError(f"LLM effect {effect_id!r} has no intent")
        if node.kind is not EffectKind.LLM_CALL:
            raise ValueError(f"effect id {effect_id!r} belongs to a non-LLM effect")
        if node.result is not None:
            recorded = node.result.fields.get(F_REPLY)
            if not isinstance(recorded, Reply):
                raise TypeError(f"recorded LLM effect {effect_id!r} has no Reply")
            return deepcopy(recorded)
        if node.rejection is not None:
            raise OrphanedEffectError(
                f"LLM effect {effect_id!r} is already a rejection, not an orphan",
            )
        model = node.intent.fields.get(F_MODEL)
        if not isinstance(model, str) or not model:
            raise OrphanedEffectError(
                f"LLM effect {effect_id!r} has no model identity",
            )
        if reply is None:
            reply = Reply(
                content=(),
                usage=Usage(),
                stop_reason="error",
                model=model,
                error_detail={
                    "type": "reconciled_external_outcome",
                    **dict(error or {}),
                },
            )
        if reply.model != model:
            raise ValueError(
                f"reconciled reply model {reply.model!r} does not match {model!r}",
            )
        outcome = RetryOutcome(
            reply=reply,
            attempts=attempts,
            total_usage=total_usage or reply.usage,
        )
        self._fold_result(effect_id, outcome)
        if reply.stop_reason != "error" or outcome.billed_usage.total:
            self._fold_spend(
                effect_id,
                replace(reply, usage=outcome.billed_usage),
                pricing_model=model,
            )
        return deepcopy(reply)

    def _recover_existing(self, effect_id: str, request: Request) -> Reply | None:
        """Return an already-recorded terminal call without live submission."""
        effect_row = next(
            (row for row in self.rowset.rows if isinstance(row, EffectRow)),
            None,
        )
        if effect_row is None:
            return None
        node = effect_row.project(self.rowset.store).nodes.get(effect_id)
        if node is None:
            return None
        if node.kind is not EffectKind.LLM_CALL:
            raise ValueError(f"effect id {effect_id!r} belongs to a non-LLM effect")
        recorded_request = node.intent.fields.get(F_REQUEST)
        if (
            not isinstance(recorded_request, Request)
            or encode(recorded_request, _RECOVERY_CODECS)
            != encode(request, _RECOVERY_CODECS)
        ):
            raise ValueError(f"effect id {effect_id!r} was reused for a different request")
        if node.result is not None:
            reply = node.result.fields.get(F_REPLY)
            if not isinstance(reply, Reply):
                raise TypeError(f"recorded LLM effect {effect_id!r} has no Reply")
            total_usage = node.result.fields.get(F_TOTAL_USAGE, reply.usage)
            if not isinstance(total_usage, Usage):
                raise TypeError(
                    f"recorded LLM effect {effect_id!r} has invalid total usage",
                )
            if reply.stop_reason != "error" or total_usage.total:
                self._fold_spend(
                    effect_id,
                    replace(reply, usage=total_usage),
                    pricing_model=request.model,
                )
            return deepcopy(reply)
        if node.rejection is not None:
            fields = node.rejection.fields
            detail = fields.get(F_DETAIL)
            return Reply(
                content=(),
                usage=Usage(),
                stop_reason="error",
                model=request.model,
                error_detail={
                    "type": "preflight_rejection",
                    "reason": fields.get(F_REASON),
                    **(dict(detail) if isinstance(detail, Mapping) else {}),
                },
            )
        raise OrphanedEffectError(
            f"LLM effect {effect_id!r} has intent but no terminal outcome",
        )

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
        target = LLMTarget(request=deepcopy(request))
        cache: list[ResourceEstimate] = []

        async def estimate() -> ResourceEstimate:
            if not cache:
                cache.append(await self.adapter.estimate_cost(request))
            return cache[0]

        stream_rejection: BudgetRejection | GuardRejection | None = (
            await self._preflight_budget(request, estimate)
        )
        if stream_rejection is None:
            stream_rejection = await self._preflight_guards(target, estimate)
        if stream_rejection is not None:
            yield Done(self._record_rejection(effect_id=effect_id, request=request, compensates=compensates, caused_by=caused_by, rejection=stream_rejection))
            return
        self._fold_intent(effect_id, request, compensates, caused_by)
        async for event in self.adapter.stream(request):
            yield event
            if isinstance(event, Done):
                outcome = RetryOutcome(reply=event.reply, attempts=1)
                self._fold_result(effect_id, outcome)
                if event.reply.stop_reason != "error" or event.reply.usage.total:
                    self._fold_spend(
                        effect_id,
                        event.reply,
                        pricing_model=request.model,
                    )
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
            F_PROVIDER_REQUEST_ID: request.request_id or effect_id,
            # Request is frozen only at the top level; provider-shaped message
            # mappings can still be mutable. The tape records submission-time
            # data, not later caller mutation.
            F_REQUEST:   deepcopy(request),
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

    def _fold_attempt(
        self,
        effect_id: str,
        request: Request,
        attempt: int,
        reply: Reply,
        kind: ErrorKind | None,
    ) -> None:
        """Persist each provider attempt, including usage on failed retries."""
        self.rowset.fold(Claim(
            tag="llm_attempt",
            fields={
                F_EFFECT_ID: effect_id,
                F_KIND: EffectKind.LLM_CALL.value,
                F_PROVIDER_REQUEST_ID: request.request_id or effect_id,
                "attempt": attempt,
                "status": "error" if reply.stop_reason == "error" else "ok",
                "error_kind": None if kind is None else kind.value,
                "usage": reply.usage,
                "billed": bool(reply.usage.total),
            },
            source="harness.retry",
            claim_id=f"{effect_id}:attempt:{attempt}",
        ))

    def _fold_result(self, effect_id: str, outcome: RetryOutcome) -> None:
        # Provider content commonly contains mutable dict blocks. Snapshot it
        # so an in-memory tape is as stable as a SQLite-serialized one.
        reply = deepcopy(outcome.reply)
        result_fields: dict = {
            F_EFFECT_ID: effect_id,
            F_KIND:      EffectKind.LLM_CALL.value,
            F_ATTEMPTS:  outcome.attempts,
            F_TOTAL_USAGE: outcome.billed_usage,
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

    def _fold_spend(
        self,
        effect_id: str,
        reply: Reply,
        *,
        pricing_model: str | None = None,
    ) -> None:
        buckets = self.bucket_extractor(reply)
        prices = getattr(self.adapter, "prices", None)
        if prices is not None and "cost_usd" not in buckets:
            try:
                price = prices.for_model(pricing_model or reply.model)
            except UnknownModelError:
                logger.warning(
                    "lipas: no price configured for model %r; token usage is "
                    "recorded but cost_usd is unavailable",
                    pricing_model or reply.model,
                )
            else:
                buckets = {
                    **buckets,
                    "cost_usd": float(price.cost(reply.usage)),
                }
        if not buckets:
            return

        cap = self._capability_row()
        proj = cap.project(self.rowset.store) if cap is not None else None

        for bucket, amount in buckets.items():
            if amount <= 0:
                continue

            claim_id = self._spend_claim_id(effect_id, bucket)
            if any(
                claim.claim_id == claim_id
                for tag in (TAG_RESOURCE_SPENT, TAG_BUDGET_OVERRUN)
                for claim in self.rowset.store.filter(tag=tag)
            ):
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
                    claim_id=claim_id,
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
                    claim_id=claim_id,
                ))

    @staticmethod
    def _spend_claim_id(effect_id: str, bucket: str) -> str:
        digest = hashlib.sha256(
            f"llm-spend:{effect_id}:{bucket}".encode("utf-8"),
        ).hexdigest()[:24]
        return f"spend_{digest}"

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
