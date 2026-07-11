"""Read-only, tag-aware projection for supervisor recommendations.

Unlike the old deferred calculus module this is not a merge strategy: it is a
plain projection over ClaimStore's tag index, so it adds no hidden fold rules.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .store import ClaimStore
from .supervisor import (
    F_SUP_ATTEMPT_INDEX, F_SUP_IDEMPOTENCY_KEY, F_SUP_MAX_ATTEMPTS,
    F_SUP_REASON, F_SUP_TARGET_EFFECT_ID, TAG_SUPERVISOR_ESCALATE,
    TAG_SUPERVISOR_RETRY, TAG_SUPERVISOR_TERMINATE,
)

__all__ = ["RetryRecommendation", "SupervisorProjection", "project_supervisor"]


@dataclass(frozen=True, slots=True)
class RetryRecommendation:
    target_effect_id: str
    idempotency_key: str
    attempt_index: int
    max_attempts: int
    reason: str


@dataclass(frozen=True, slots=True)
class SupervisorProjection:
    retries: tuple[RetryRecommendation, ...]
    terminated: bool
    terminate_reason: str | None
    escalations: tuple[dict[str, Any], ...]


def project_supervisor(store: ClaimStore) -> SupervisorProjection:
    """Project supervisor state using ClaimStore's indexed ``filter(tag=)``."""
    retries = tuple(
        RetryRecommendation(
            target_effect_id=str(c.fields.get(F_SUP_TARGET_EFFECT_ID, "")),
            idempotency_key=str(c.fields.get(F_SUP_IDEMPOTENCY_KEY, "")),
            attempt_index=int(c.fields.get(F_SUP_ATTEMPT_INDEX, 0)),
            max_attempts=int(c.fields.get(F_SUP_MAX_ATTEMPTS, 0)),
            reason=str(c.fields.get(F_SUP_REASON, "")),
        ) for c in store.filter(tag=TAG_SUPERVISOR_RETRY)
    )
    terminations = store.filter(tag=TAG_SUPERVISOR_TERMINATE)
    escalations = tuple(dict(c.fields) for c in store.filter(tag=TAG_SUPERVISOR_ESCALATE))
    return SupervisorProjection(
        retries=retries,
        terminated=bool(terminations),
        terminate_reason=str(terminations[-1].fields.get(F_SUP_REASON, "")) if terminations else None,
        escalations=escalations,
    )
