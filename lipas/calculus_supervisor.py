"""
lipas.calculus_supervisor — fold strategies for supervisor_* claims.

These strategies are A3-pure. They maintain a small projection that the
agent loop can query to decide whether to honor a supervisor recommendation.

The projection itself is NOT the source of truth. The log is the source
of truth. The projection is a convenience: equivalent to refolding from
log on every read, but cheaper.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Optional

# TODO(B3-api): align registration mechanism with calculus.py.
from lipas.calculus import register_strategy      # type: ignore

from lipas.supervisor import (
    TAG_SUPERVISOR_RETRY,
    TAG_SUPERVISOR_TERMINATE,
    TAG_SUPERVISOR_ESCALATE,
)


# --- Projection ------------------------------------------------------------

@dataclass(frozen=True)
class RetryRec:
    target_effect_id: str
    idempotency_key:  str
    attempt_index:    int
    max_attempts:     int
    reason:           str


@dataclass(frozen=True)
class EscalationRec:
    reason:  str
    payload: dict = field(default_factory=dict)


@dataclass(frozen=True)
class SupervisorState:
    """Rolling projection of supervisor recommendations.

    Field semantics:
      - `pending_retries` is append-only across folds. The loop is
        responsible for matching honored retries to their originating
        recommendations via lineage; this projection does not mutate
        once an entry is in.
      - `terminated` is monotone — once true, stays true. v0.1 does not
        support un-terminate (same rationale as A4 §4.4 on un-revoke).
      - `escalations` is append-only.
    """
    pending_retries: tuple[RetryRec, ...] = ()
    terminated:      bool                  = False
    terminate_reason: Optional[str]        = None
    escalations:     tuple[EscalationRec, ...] = ()


# --- Strategies ------------------------------------------------------------

# All three strategies are pure of (state, claim, ctx, registry). They
# satisfy A3 trivially: only tuple/dataclass replacement, no I/O, no
# clock, no env.

@register_strategy(TAG_SUPERVISOR_RETRY)
def _fold_supervisor_retry(state, claim, ctx, registry):
    s: SupervisorState = state if state is not None else SupervisorState()
    f = claim.fields
    rec = RetryRec(
        target_effect_id=f["target_effect_id"],
        idempotency_key=f["idempotency_key"],
        attempt_index=f["attempt_index"],
        max_attempts=f["max_attempts"],
        reason=f["reason"],
    )
    return replace(s, pending_retries=s.pending_retries + (rec,))


@register_strategy(TAG_SUPERVISOR_TERMINATE)
def _fold_supervisor_terminate(state, claim, ctx, registry):
    s: SupervisorState = state if state is not None else SupervisorState()
    if s.terminated:
        # idempotent: first terminate wins. Multiple terminate claims
        # are tolerated (predicates may double-fire across ticks); only
        # the first reason is preserved for audit clarity.
        return s
    return replace(
        s,
        terminated=True,
        terminate_reason=claim.fields["reason"],
    )


@register_strategy(TAG_SUPERVISOR_ESCALATE)
def _fold_supervisor_escalate(state, claim, ctx, registry):
    s: SupervisorState = state if state is not None else SupervisorState()
    f = claim.fields
    rec = EscalationRec(reason=f["reason"], payload=f.get("payload", {}))
    return replace(s, escalations=s.escalations + (rec,))
