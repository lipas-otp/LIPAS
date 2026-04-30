"""
LIPAS · Rows — the canonical projections over a ClaimStore.

A Row is an operational unit above Layer 0.5 (store.py).  Each row
declares which Claim tags it owns, which field-level merge strategies
it needs, and how to project its view over the store.

The four rows are independent: a read-only research agent can run with
HistoryRow alone.  Add CapabilityRow when budgets matter; IdentityRow
when trust tracking matters; EffectRow when causal lineage / replay /
compensation matter.  None is a prerequisite for the others.

Axiom restated: Layer 0 is unified (one merge, one Claim).  The four
rows are not algebraic extensions — they are operational differentiations
of the same substrate.
"""

from .base       import Row, RowSet, InvariantViolation
from .history    import HistoryRow
from .capability import CapabilityRow
from .identity   import IdentityRow
from .effect     import EffectRow, EffectView, CallNode


__all__ = [
    "Row", "RowSet", "InvariantViolation",
    "HistoryRow", "CapabilityRow", "IdentityRow",
    "EffectRow", "EffectView", "CallNode",
]
