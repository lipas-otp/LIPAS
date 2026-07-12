"""
LIPAS · Layer 0.5 — Claim Store
===============================

The append-only substrate that Rows project over.

A ClaimStore holds:
  - an ordered log of Claims (the "facts that have happened")
  - a materialized incremental join (the merged Claim)
  - the StrategyRegistry that resolves field conflicts

That is the entirety of its responsibility.  It knows nothing of
history, capability, or effect lineage — those are Row-level concerns.

This is the concretization of the design axiom:
    The system is, at bottom, a single monotone join-semilattice
    over a set of causally-identified claims.  Everything else is
    a projection.
"""

from __future__ import annotations
from dataclasses import replace
from typing import Iterator

from .calculus import (
    Claim, BOTTOM,
    BeliefContext, StrategyRegistry,
    merge, make_default_registry,
)
from .exceptions import ClaimIdConflict


def _same_claim_payload(left: Claim, right: Claim) -> bool:
    """Compare logical claim content while ignoring store-assigned sequence."""
    return (
        left.tag == right.tag
        and left.fields == right.fields
        and left.kind == right.kind
        and left.priority == right.priority
        and left.source == right.source
    )


class ClaimStore:
    """Append-only, claim-idempotent set with an incremental projection."""

    __slots__ = (
        "_registry", "_ctx", "_merged", "_log", "_seq", "_by_tag", "_by_id",
    )

    def __init__(
        self,
        registry: StrategyRegistry | None = None,
        ctx: BeliefContext | None = None,
    ) -> None:
        self._registry = registry or make_default_registry()
        self._ctx      = ctx or BeliefContext()
        self._merged: Claim = BOTTOM
        self._log:   list[Claim] = []
        self._seq:   int = 0
        self._by_tag: dict[str, list[int]] = {}
        self._by_id: dict[str, Claim] = {}

    # ── writes ────────────────────────────────────────────────

    def fold(self, claim: Claim) -> Claim:
        """Fold one logical claim exactly once; return the merged state.

        Re-delivery with the same ``claim_id`` and payload is a no-op. Reusing
        that id for a different payload is rejected because it would corrupt
        the audit tape's causal identity.
        """
        existing = self._by_id.get(claim.claim_id)
        if existing is not None:
            if _same_claim_payload(existing, claim):
                return self._merged
            raise ClaimIdConflict(
                f"claim_id={claim.claim_id!r} was reused for a different claim"
            )
        if claim.seq < 0:
            claim = replace(claim, seq=self._seq)
        idx = len(self._log)
        self._log.append(claim)
        self._by_tag.setdefault(claim.tag, []).append(idx)
        self._by_id[claim.claim_id] = claim
        self._seq += 1
        self._merged = merge(self._merged, claim, self._ctx, self._registry)
        return self._merged

    # ── reads ─────────────────────────────────────────────────

    # ClaimStore.merged is an aggregated summary of all the fields of the folded Claims,
    # used for quick reading of the current "latest status". Semantic distinction across
    # tags is accomplished through filter(tag=...) and Row projection, not through merged.
    @property
    def merged(self) -> Claim:        return self._merged
    @property
    def log(self) -> tuple[Claim, ...]: return tuple(self._log)
    @property
    def registry(self) -> StrategyRegistry: return self._registry
    @property
    def ctx(self) -> BeliefContext:   return self._ctx
    @property
    def seq(self) -> int:             return self._seq

    def __len__(self) -> int:               return len(self._log)
    def __iter__(self) -> Iterator[Claim]:  return iter(self._log)

    def filter(
        self, *,
        tag:    str | None = None,
        kind:   str | None = None,
        source: str | None = None,
    ) -> list[Claim]:
        if tag is not None and kind is None and source is None:
            return [self._log[i] for i in self._by_tag.get(tag, ())]
        out = []
        for c in self._log:
            if tag    is not None and c.tag    != tag:    continue
            if kind   is not None and c.kind   != kind:   continue
            if source is not None and c.source != source: continue
            out.append(c)
        return out

    def __repr__(self) -> str:
        return f"ClaimStore(size={len(self._log)}, tags={sorted(self._by_tag)})"
