"""
Capability Row — the linear/resource projection.

Resources spent, quotas consumed, rate-limit events.  The fold gate
here is a hard budget check: a resource_spent claim whose amount
would exceed the bucket's limit is rejected.

Idempotency is preserved at Layer 0 (each claim has a claim_id);
linearity lives in the projection (we sum spent amounts, deduplicated
by claim_id).  This is the concrete form of the "PN-counter" pattern.

Overrun tag
-----------
``TAG_BUDGET_OVERRUN`` is a sibling tag to ``TAG_RESOURCE_SPENT`` that
records spend WHICH WOULD EXCEED THE BUDGET. Pre-flight checks in the
LLMHarness are meant to keep this near zero; when it fires anyway —
adapter mis-estimation, PriceTable drift, an estimator that rounds
down — the harness routes the actual amount here instead of into the
gated bucket.  Net effect:

  - ``spent <= limit`` remains a hard invariant (gate untouched).
  - ``true_spent = spent + overrun`` reflects reality and is what
    monitoring/billing should consume.
  - ``overrun > 0`` is a CONFIGURATION ALARM, not a soft-budget
    feature.  Operators should investigate, not lean on it.
"""

from __future__ import annotations
from dataclasses import dataclass, field
import math

from ..calculus import (
    Claim, StrategyRegistry,
    strategy_counter_max, strategy_append,
)
from ..store import ClaimStore


# ── tags owned by this row (exported for harness/test use) ──────────
TAG_RESOURCE_SPENT = "resource_spent"
TAG_QUOTA_USED     = "quota_used"
TAG_RATE_EVENT     = "rate_event"
TAG_BUDGET_OVERRUN = "budget_overrun"

# ── field names on resource_spent / budget_overrun claims ───────────
F_BUCKET = "bucket"   # str — bucket identifier (e.g. "tokens_in", "cost_usd")
F_AMOUNT = "amount"   # int | float — non-negative amount spent / overrun


@dataclass
class CapabilityRow:
    name: str = "capability"
    namespace: frozenset[str] = field(
        default_factory=lambda: frozenset({
            TAG_RESOURCE_SPENT, TAG_QUOTA_USED, TAG_RATE_EVENT,
            TAG_BUDGET_OVERRUN,
        })
    )
    # bucket_name -> hard limit
    budgets: dict[str, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Reject budget shapes that would turn a hard gate into a no-op."""
        normalized: dict[str, float] = {}
        for bucket, limit in self.budgets.items():
            if not isinstance(bucket, str) or not bucket:
                raise ValueError(f"budget name must be a non-empty string, got {bucket!r}")
            if (
                isinstance(limit, bool)
                or not isinstance(limit, (int, float))
                or not math.isfinite(float(limit))
                or limit < 0
            ):
                raise ValueError(
                    f"budget {bucket!r} must be a finite non-negative number, got {limit!r}"
                )
            normalized[bucket] = float(limit)
        self.budgets = normalized

    def register_strategies(self, registry: StrategyRegistry) -> None:
        registry.register("_quota_totals", strategy_counter_max)
        registry.register("_rate_events",  strategy_append)

    # ── invariants ────────────────────────────────────────────

    def check_invariants(self, claim: Claim, store: ClaimStore) -> list[str]:
        if claim.tag == TAG_RESOURCE_SPENT:
            return self._check_spent(claim, store)
        if claim.tag == TAG_BUDGET_OVERRUN:
            return self._check_overrun(claim, store)
        return []

    def _check_spent(self, claim: Claim, store: ClaimStore) -> list[str]:
        msgs: list[str] = []
        bucket = claim.fields.get(F_BUCKET)
        amount = claim.fields.get(F_AMOUNT, 0)
        if not self._valid_amount(bucket, amount):
            msgs.append(f"{TAG_RESOURCE_SPENT}: missing/invalid bucket or amount")
            return msgs

        limit = self.budgets.get(bucket)
        if limit is None:
            return msgs  # unbudgeted bucket → no gate

        # Idempotent delivery: replay is a no-op.
        for c in store.filter(tag=TAG_RESOURCE_SPENT):
            if c.claim_id == claim.claim_id:
                return msgs

        current = self._spent(store, bucket)
        if current + amount > limit:
            msgs.append(
                f"budget exhausted for {bucket!r}: "
                f"{current}+{amount} > {limit}"
            )
        return msgs

    def _check_overrun(self, claim: Claim, store: ClaimStore) -> list[str]:
        # Overrun is structurally validated but NEVER gated against
        # the budget — that is the point: it records what already
        # happened in the world without falsifying the ledger.
        msgs: list[str] = []
        bucket = claim.fields.get(F_BUCKET)
        amount = claim.fields.get(F_AMOUNT, 0)
        if not self._valid_amount(bucket, amount):
            msgs.append(f"{TAG_BUDGET_OVERRUN}: missing/invalid bucket or amount")
            return msgs
        return msgs

    @staticmethod
    def _valid_amount(bucket: object, amount: object) -> bool:
        return (
            isinstance(bucket, str)
            and bool(bucket)
            and isinstance(amount, (int, float))
            and not isinstance(amount, bool)
            and math.isfinite(float(amount))
            and amount >= 0
        )

    # ── projection helpers ────────────────────────────────────

    def _spent(self, store: ClaimStore, bucket: str) -> float:
        """Deduplicated sum of spent amounts for *bucket*."""
        seen: set[str] = set()
        total: float = 0.0
        for c in store.filter(tag=TAG_RESOURCE_SPENT):
            if c.fields.get(F_BUCKET) != bucket: continue
            if c.claim_id in seen:               continue
            seen.add(c.claim_id)
            total += float(c.fields.get(F_AMOUNT, 0))
        return total

    def _overrun(self, store: ClaimStore, bucket: str) -> float:
        """Deduplicated sum of overrun amounts for *bucket*."""
        seen: set[str] = set()
        total: float = 0.0
        for c in store.filter(tag=TAG_BUDGET_OVERRUN):
            if c.fields.get(F_BUCKET) != bucket: continue
            if c.claim_id in seen:               continue
            seen.add(c.claim_id)
            total += float(c.fields.get(F_AMOUNT, 0))
        return total

    def project(self, store: ClaimStore) -> dict:
        out: dict[str, dict] = {}
        for bucket, limit in self.budgets.items():
            spent   = self._spent(store, bucket)
            overrun = self._overrun(store, bucket)
            out[bucket] = {
                "limit":      limit,
                "spent":      spent,                 # ledgered, ≤ limit
                "remaining":  limit - spent,
                "exhausted":  spent >= limit,
                "overrun":    overrun,               # off-ledger reality
                "true_spent": spent + overrun,       # for billing/monitoring
            }
        return out

    def __repr__(self) -> str:
        return (f"CapabilityRow(budgets={list(self.budgets)}, "
                f"namespace={sorted(self.namespace)})")
