"""
History Row — the epistemic projection.

What the agent has observed, interpreted, and learned.  Every
strategy in this row is a semilattice join, so re-delivery is harmless.
"""

from __future__ import annotations
from dataclasses import dataclass, field

from ..calculus import (
    Claim, StrategyRegistry,
    strategy_append, strategy_counter_max, strategy_expectations_merge,
)
from ..store import ClaimStore


@dataclass
class HistoryRow:
    name: str = "history"
    namespace: frozenset[str] = field(
        default_factory=lambda: frozenset({
            "observation", "fact", "outcome",
            "recalled", "task", "reflection",
        })
    )

    def register_strategies(self, registry: StrategyRegistry) -> None:
        # Idempotent: if these are already registered (e.g. via
        # make_default_registry), re-registering with the same function
        # is a harmless overwrite.
        registry.register("_history",      strategy_append)
        registry.register("_fail_log",     strategy_append)
        registry.register("_fail_counts",  strategy_counter_max)
        registry.register("_expectations", strategy_expectations_merge)

    def check_invariants(self, claim: Claim, store: ClaimStore) -> list[str]:
        # The epistemic row has no hard gates.  Under semilattice
        # semantics, accepting a duplicate observation is a no-op.
        return []

    def project(self, store: ClaimStore) -> dict:
        owned = [c for c in store if c.tag in self.namespace]
        fields = dict(store.merged.fields)
        domain = {k: v for k, v in fields.items() if not k.startswith("_")}
        return {
            "domain":       domain,
            "event_count":  len(owned),
            "last_seq":     max((c.seq for c in owned), default=-1),
            "fail_counts":  fields.get("_fail_counts", {}),
            "expectations": fields.get("_expectations", {}),
        }

    def __repr__(self) -> str:
        return f"HistoryRow(namespace={sorted(self.namespace)})"
