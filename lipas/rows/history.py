"""
History Row — the epistemic projection.

What the agent has observed, interpreted, and learned.  Every
strategy in this row is a semilattice join, so re-delivery is harmless.

P3.2 — TAG_REPLAY_DECISION (RFC-001) is owned by HistoryRow because
a replay decision is, semantically, an observation about how the
session was reproduced (mode, frozen seq, per-call substitute /
re-execute / refuse / fail). It carries no invariant — duplicate
folds are harmless, all decision-claim fields use first-write-wins
under the default strategy. Membership in this namespace exists so
RowSet.fold has an explicit owner for the tag, not because Row-side
validation is required.

B3 — supervisor_retry / supervisor_terminate / supervisor_escalate
follow the same pattern: they are observations the agent made about
its own ongoing run (recommended retries, requested terminations,
human escalations). No invariant; first-write-wins under the default
strategy. The Supervisor module (lipas.supervisor) is the canonical
producer; readers consume them via store.filter(tag=...) or
HistoryRow.project()['event_count'].
"""

from __future__ import annotations
from dataclasses import dataclass, field

from ..calculus import (
    Claim, StrategyRegistry,
    strategy_append, strategy_counter_max, strategy_expectations_merge,
)
from ..replay_tools import TAG_REPLAY_DECISION
from ..supervisor import (
    TAG_SUPERVISOR_RETRY,
    TAG_SUPERVISOR_TERMINATE,
    TAG_SUPERVISOR_ESCALATE,
)
from ..store import ClaimStore


@dataclass
class HistoryRow:
    name: str = "history"
    namespace: frozenset[str] = field(
        default_factory=lambda: frozenset({
            "observation", "fact", "outcome",
            "recalled", "task", "reflection",
            # P3.2 — replay audit (RFC-001 §6.3).
            TAG_REPLAY_DECISION,
            # B3 — supervisor recommendations.  Pure audit; no
            # invariants.  Behaviours MAY honor terminate/escalate
            # for early loop exit; retry is informational at the
            # ReAct level (see docs/B3-NOTES.md).
            TAG_SUPERVISOR_RETRY,
            TAG_SUPERVISOR_TERMINATE,
            TAG_SUPERVISOR_ESCALATE,
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
        # Replay-decision and supervisor_* claims also need no
        # validation: their producers (ToolReplayer, Supervisor) build
        # them and they are pure audit.
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
