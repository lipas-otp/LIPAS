"""
History Row — the epistemic projection.

What the agent has observed, interpreted, and learned. History includes
ordered append-only events; re-delivery is harmless because ClaimStore admits
one stable claim id only once.

TAG_REPLAY_DECISION is owned by HistoryRow because a replay decision is,
semantically, an observation about how the session was reproduced (mode,
frozen seq, per-call substitute / re-execute / refuse / fail). It carries no
row-specific invariant. Membership in this namespace exists so
RowSet.fold has an explicit owner for the tag, not because Row-side
validation is required.

supervisor_retry / supervisor_terminate / supervisor_escalate follow the same
pattern: they are observations the agent made about
its own ongoing run (recommended retries, requested terminations,
human escalations). No invariant; field conflicts follow the registry's
ordered default strategy. The Supervisor module (lipas.supervisor) is the canonical
producer; readers consume them via store.filter(tag=...) or
HistoryRow.project()['event_count'].

goal_blocked is the structural pair to supervisor_terminate / supervisor_escalate:
every terminate/escalate is followed by a
goal_blocked in the same _emit_batch call (see Supervisor C5). It is
also pure audit; row-side validation lives in lipas.lint
(``goal_blocked_pairing``) rather than here, because the invariant is
cross-claim (next-seq adjacency + back-reference equality) and
HistoryRow's check_invariants signature only sees one claim at a time.
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
    TAG_GOAL_BLOCKED,
)
from ..store import ClaimStore


@dataclass
class HistoryRow:
    name: str = "history"
    namespace: frozenset[str] = field(
        default_factory=lambda: frozenset({
            "observation", "fact", "outcome",
            "recalled", "task", "reflection",
            # Replay audit.
            TAG_REPLAY_DECISION,
            # Supervisor recommendations. Behaviours may honor
            # terminate/escalate for early loop exit; retry remains
            # informational at the ReAct level.
            TAG_SUPERVISOR_RETRY,
            TAG_SUPERVISOR_TERMINATE,
            TAG_SUPERVISOR_ESCALATE,
            # goal_blocked: structural pair to terminate/escalate.
            # Lives here so it counts toward event_count and is visible
            # to readers walking the history namespace; cross-claim
            # pairing invariant is enforced by lipas.lint, not by
            # this row.
            TAG_GOAL_BLOCKED,
            # Behaviour-neutral observers record advisory output here. The
            # recommendation is evidence, never an approval or capability.
            "observer_recommendation",
            # Team coordination is audit history, not a separate workflow
            # state machine. These tags link mailbox lifecycle to a Team's
            # durable claim session.
            "agent_handoff", "agent_mail_claim", "agent_mail_ack",
            "agent_mail_released", "agent_mail_recovered",
            # OperationJournal transition audit. The journal deliberately
            # lives at the external boundary; these claims associate its
            # durable idempotency state with an effect/tape when supplied.
            "operation_prepared", "operation_uncertain",
            "operation_succeeded", "operation_failed",
            # ExecutionStore transition audit. The execution database remains
            # authoritative for leases/checkpoints; these outbox mirrors make
            # its control history visible in the shared evidence projection.
            "execution_task_created", "execution_task_completed",
            "execution_task_cancelled", "execution_run_created",
            "execution_run_claimed", "execution_lease_renewed",
            "execution_checkpoint_saved", "execution_interrupt_requested",
            "execution_interrupt_resolved", "execution_cancel_requested",
            "execution_run_completed", "execution_run_failed",
            "execution_run_cancelled",
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
        # The epistemic row has no hard gates. ClaimStore makes an identical
        # re-delivery a no-op before this projection can record it twice.
        # Replay-decision / supervisor_* / goal_blocked claims also
        # need no validation: their producers (ToolReplayer,
        # Supervisor) build them and they are pure audit.  Cross-claim
        # invariants (e.g. terminate ↔ goal_blocked pairing) are
        # checked by lipas.lint, not here.
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
