"""
lipas.calculus_supervisor — DEFERRED to Phase 5β.

This module is INTENTIONALLY non-functional in v0.1. Its original goal
was to register tag-based fold strategies for supervisor_* claims and
maintain a ``SupervisorState`` projection. But ``lipas.calculus``
registers strategies by FIELD NAME, not by TAG — the two registration
machineries are not aligned. Reconciling them is a Phase 5β design
question (see B3-NOTES.md): it touches the meaning of "projection" as
a primitive of the calculus.

Until that reconciliation lands:

  - This module RAISES on import (tripwire). Any code that imports it,
    including a future accidental ``__init__.py`` re-export, fails
    loudly rather than silently registering nothing.

  - The projection dataclasses (``RetryRec`` / ``EscalationRec`` /
    ``SupervisorState``) are preserved BELOW the raise as future
    reference. They are unreachable in v0.1 but stable as a type
    sketch for 5β.

  - Source of truth for supervisor recommendations remains the log
    itself: ``store.filter(tag=TAG_SUPERVISOR_RETRY)`` etc. This is
    O(N) per query but acceptable in v0.1 (tick frequency is low and
    log sizes are bounded by the agent run).

History
-------
Earlier drafts had:

    from lipas.calculus import register_strategy   # symbol does not exist

which made this module dead-on-arrival. The broken import is now
removed; the raise below replaces it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


# ── Tripwire ────────────────────────────────────────────────────────

raise NotImplementedError(
    "lipas.calculus_supervisor is deferred to Phase 5β: tag-based "
    "projection registration. See module docstring and "
    "docs/B3-NOTES.md. Use store.filter(tag=...) directly until then."
)


# ── Preserved for future use (unreachable in v0.1) ─────────────────
# Anything below this point is documentation, not live code. The
# raise above unconditionally aborts module execution.


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

    Field semantics (for 5β reference):
      - ``pending_retries`` is append-only across folds.
      - ``terminated`` is monotone — once true, stays true.
      - ``escalations`` is append-only.
    """
    pending_retries:  tuple[RetryRec, ...]    = ()
    terminated:       bool                     = False
    terminate_reason: Optional[str]            = None
    escalations:      tuple[EscalationRec, ...] = ()
