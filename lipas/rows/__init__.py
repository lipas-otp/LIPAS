"""
LIPAS · Rows — the canonical projections over a ClaimStore.

A Row is an operational unit above Layer 0.5 (store.py).  Each row
declares which Claim tags it owns, which field-level merge strategies
it needs, and how to project its view over the store.

The three rows are independent: HistoryRow records observations and
coordination, CapabilityRow owns budgets, and EffectRow owns causal lineage /
replay.  Normal Agent sessions use all three.

Axiom restated: Layer 0 is unified (one merge, one Claim). The three
rows are not algebraic extensions — they are operational differentiations
of the same substrate.
"""

from .base       import Row, RowSet, InvariantViolation
from .history    import HistoryRow
from .capability import CapabilityRow
from .effect     import EffectRow, EffectView


__all__ = [
    "Row", "RowSet", "InvariantViolation",
    "HistoryRow", "CapabilityRow",
    "EffectRow", "EffectView",
]
