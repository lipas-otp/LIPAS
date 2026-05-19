"""
lipas.supervisor — Policy-driven supervisor (B3, first batch).

NORMATIVE summary
=================

A Supervisor is a pure component, **explicitly driven by an agent
behaviour** (e.g. ReActAgent), which observes an immutable snapshot of
``(EffectView, BeliefContext)`` and batches *recommendation-typed* claims
into the rowset. It performs no I/O and does not observe its own emitted
claims within the same tick.

Why behaviour-level, not loop-level
-----------------------------------
LIPAS has no single "agent loop": each ``AgentBehaviour`` (ReAct, Plan-
and-Execute, Critique-and-Revise, ...) owns its own loop shape. Supervisor
is therefore wired in at the behaviour boundary — ReActAgent calls
``supervisor.tick(view, ctx)`` at the end of each R-A-O cycle. Other
behaviours pick their own tick site. See ``docs/B3-NOTES.md``.

Contracts
---------

C1. **Drive site**. ``Supervisor.tick(view, ctx)`` MUST be called by an
    agent behaviour. It MUST NOT be called from within ``ClaimStore.fold``,
    from any merge strategy, or from a tool/LLM callback. During replay
    the behaviour SHOULD NOT call ``tick``: supervisor_* claims from the
    recorded run live in the source store and are not re-derived here.
    A future "supervisor replay cursor" (parallel to ReplayCursor /
    ToolReplayer) MAY mirror them into a target store; out of scope for B3.

C2. **Snapshot semantics**. Within a single ``tick``, all predicates
    observe the same frozen ``(view, ctx)``. The retry-cap tally is
    captured once at tick start; predicates registered later in the same
    tick see neither earlier predicates' emissions nor each other's. New
    supervisor_* claims become visible only on the next tick.

C3. **Recommendation semantics**. supervisor_* claims are *advisory*. The
    behaviour MAY decline to act on any of them. The required tape
    invariant is the converse: if a downstream effect cites a
    supervisor_* recommendation as its trigger, its lineage MUST carry
    the supervisor claim's ``idempotency_key`` / ``target_effect_id``.
    The forward direction ("every supervisor_retry produces an effect")
    is intentionally NOT required.

C4. **Determinism**. Predicates MUST be pure functions of ``(view, ctx)``
    under the A3 strategy-purity contract (lipas/calculus.py). Supervisor
    itself adds no nondeterminism: ``idempotency_key`` is hashed from
    ``(session_id, target_effect_id, attempt_index)`` and
    ``attempt_index`` is computed from the rowset's prior supervisor_retry
    count plus retries proposed earlier in the same tick.

The first batch of tactics is ``retry / terminate / escalate_human``.
The second batch (``degrade / circuit_break / compensate``) is deferred
until concrete use cases land.

Schema notes
------------
Each supervisor_* claim carries ``F_SUP_SCHEMA_VERSION`` in ``fields`` to
anticipate A2 (Claim Schema Evolution). When A2 lands, this field becomes
the canonical entry point for upcasters. Until then it is purely
informational.
"""

from __future__ import annotations

import enum
import hashlib
from dataclasses import dataclass, field
from typing import Callable, Optional, Union

from lipas.calculus import BeliefContext, Claim
from lipas.rows import RowSet
from lipas.rows.effect import EffectView


__all__ = [
    # Tags
    "TAG_SUPERVISOR_RETRY",
    "TAG_SUPERVISOR_TERMINATE",
    "TAG_SUPERVISOR_ESCALATE",
    # Schema versions
    "SUPERVISOR_RETRY_V",
    "SUPERVISOR_TERMINATE_V",
    "SUPERVISOR_ESCALATE_V",
    # Field-name constants
    "F_SUP_SCHEMA_VERSION",
    "F_SUP_TARGET_EFFECT_ID",
    "F_SUP_IDEMPOTENCY_KEY",
    "F_SUP_ATTEMPT_INDEX",
    "F_SUP_MAX_ATTEMPTS",
    "F_SUP_REASON",
    "F_SUP_PAYLOAD",
    # Action / Policy types
    "Tactic",
    "RetryAction",
    "TerminateAction",
    "EscalateAction",
    "SupervisorAction",
    "Predicate",
    "PolicyRule",
    "Policy",
    # Supervisor
    "Supervisor",
]


# ── Tactics & Actions ────────────────────────────────────────────────


class Tactic(enum.Enum):
    """First-batch tactics. Second batch (degrade / circuit_break /
    compensate) is intentionally absent until use cases surface."""
    RETRY = "retry"
    TERMINATE = "terminate"
    ESCALATE_HUMAN = "escalate_human"


@dataclass(frozen=True)
class RetryAction:
    target_effect_id: str
    max_attempts: int
    reason: str
    # ``attempt_index`` is computed by Supervisor from the rowset, not by
    # predicates. Predicates declare "I want a retry"; Supervisor decides
    # "this is the Nth one" and enforces the cap.
    tactic: Tactic = field(default=Tactic.RETRY, init=False)


@dataclass(frozen=True)
class TerminateAction:
    reason: str
    tactic: Tactic = field(default=Tactic.TERMINATE, init=False)


@dataclass(frozen=True)
class EscalateAction:
    reason: str
    payload: dict = field(default_factory=dict)  # JSON-serializable
    tactic: Tactic = field(default=Tactic.ESCALATE_HUMAN, init=False)


SupervisorAction = Union[RetryAction, TerminateAction, EscalateAction]


# ── Predicate signature & Policy ────────────────────────────────────


Predicate = Callable[[EffectView, BeliefContext], Optional[SupervisorAction]]


@dataclass(frozen=True)
class PolicyRule:
    """Named predicate. The name appears in emitted claims' ``reason`` for
    audit traceability — readers of the tape should not need access to
    source code to identify which predicate fired."""
    name: str
    predicate: Predicate


@dataclass(frozen=True)
class Policy:
    rules: tuple[PolicyRule, ...]

    @classmethod
    def of(cls, *rules: PolicyRule) -> "Policy":
        return cls(rules=tuple(rules))


# ── Tags, schema versions, field-name constants ─────────────────────


TAG_SUPERVISOR_RETRY     = "supervisor_retry"
TAG_SUPERVISOR_TERMINATE = "supervisor_terminate"
TAG_SUPERVISOR_ESCALATE  = "supervisor_escalate"

SUPERVISOR_RETRY_V     = 1
SUPERVISOR_TERMINATE_V = 1
SUPERVISOR_ESCALATE_V  = 1

F_SUP_SCHEMA_VERSION   = "schema_version"
F_SUP_TARGET_EFFECT_ID = "target_effect_id"
F_SUP_IDEMPOTENCY_KEY  = "idempotency_key"
F_SUP_ATTEMPT_INDEX    = "attempt_index"
F_SUP_MAX_ATTEMPTS     = "max_attempts"
F_SUP_REASON           = "reason"
F_SUP_PAYLOAD          = "payload"


# ── Snapshot ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _TickSnapshot:
    """Frozen ``(view, ctx)`` handed to all predicates within one tick.

    EffectView and BeliefContext are read-only by their own contracts;
    the dataclass(frozen=True) wrapper documents that the *snapshot
    itself* is not replaceable mid-tick.
    """
    view: EffectView
    ctx: BeliefContext


# ── Helpers ─────────────────────────────────────────────────────────


def _gen_idempotency_key(
    session_id: str, target_effect_id: str, attempt_index: int
) -> str:
    """Deterministic idempotency key for the recommended retry.

    Keyed on session so two distinct supervisors over the same effect do
    not collide; on attempt_index so each recommendation is unique;
    truncated to 16 bytes (32 hex chars) — small enough for log lines,
    wide enough to avoid collision in any plausible session.
    """
    h = hashlib.sha256()
    h.update(session_id.encode("utf-8"))
    h.update(b"\x00")
    h.update(target_effect_id.encode("utf-8"))
    h.update(b"\x00")
    h.update(str(attempt_index).encode("utf-8"))
    return h.hexdigest()[:32]


# ── Supervisor ──────────────────────────────────────────────────────


class Supervisor:
    """See module docstring for normative contracts C1–C4.

    Construction
    ------------
    policy:
        Ordered ``(name, predicate)`` pairs. Order is for audit
        determinism only; predicates MUST NOT depend on it.
    rowset:
        Sink for supervisor_* claims; also the source of truth for the
        retry-cap tally. The rowset's HistoryRow SHOULD list
        ``TAG_SUPERVISOR_*`` in its namespace (see ``rows/history.py``).
        Folding into a rowset whose HistoryRow lacks them is permitted
        — RowSet silently accepts tags outside any namespace — but
        ``HistoryRow.event_count`` will under-report.
    session_id:
        Opaque scope for idempotency-key hashing. Two Supervisors with
        different session_ids over the same target produce different
        keys — by design, since they represent distinct supervisory
        authorities.

    Driving
    -------
    The behaviour passes ``(view, ctx)`` derived from its rowset:

        eff_row = next(r for r in rowset.rows if isinstance(r, EffectRow))
        view    = eff_row.project(rowset.store)
        ctx     = rowset.store.ctx
        emitted = supervisor.tick(view, ctx)

    The returned list is the claims folded in this tick, in the order
    their predicates fired. Behaviours MAY scan it for terminate /
    escalate to perform early loop exit (see ReActAgent).
    """

    def __init__(
        self,
        policy: Policy,
        rowset: RowSet,
        session_id: str,
    ) -> None:
        if not isinstance(session_id, str) or not session_id:
            raise ValueError("session_id must be a non-empty string")
        self._policy = policy
        self._rowset = rowset
        self._session_id = session_id

    # ----- public API -----

    def tick(self, view: EffectView, ctx: BeliefContext) -> list[Claim]:
        """One supervisor pass.

        Returns the claims that were folded, in firing order. Source of
        truth is still the rowset's store; the return value is a
        convenience for behaviours that want to react to terminate /
        escalate without re-querying.
        """
        snapshot = _TickSnapshot(view=view, ctx=ctx)

        # Captured once at tick start (C2). Predicates emitted earlier
        # in this tick are accounted for via ``in_tick_retries`` below,
        # not by re-reading the store.
        prior_retries: dict[str, int] = self._tally_prior_retries()

        # Phase 1 — evaluate all predicates against the same snapshot.
        # No fold yet; predicates do not see each other.
        pending: list[tuple[SupervisorAction, str, int]] = []
        in_tick_retries: dict[str, int] = {}

        for rule in self._policy.rules:
            action = rule.predicate(snapshot.view, snapshot.ctx)
            if action is None:
                continue
            self._validate_action(action, rule.name)

            attempt_index = 0  # only meaningful for RETRY

            if isinstance(action, RetryAction):
                target = action.target_effect_id
                already = (
                    prior_retries.get(target, 0)
                    + in_tick_retries.get(target, 0)
                )
                if already >= action.max_attempts:
                    # Cap reached — silent skip. The absence of a claim
                    # IS the audit signal: tape readers can observe
                    # N == max_attempts retries and infer the cap.
                    continue
                attempt_index = already + 1
                in_tick_retries[target] = (
                    in_tick_retries.get(target, 0) + 1
                )

            pending.append((action, rule.name, attempt_index))

        # Phase 2 — convert to claims and fold.
        claims = [
            self._action_to_claim(a, name, idx)
            for (a, name, idx) in pending
        ]
        self._emit_batch(claims)
        return claims

    # ----- private -----

    def _tally_prior_retries(self) -> dict[str, int]:
        """Count of supervisor_retry claims per target_effect_id, taken
        from the rowset's store. Stateless across ticks: two Supervisors
        fed the same log produce the same recommendations.
        """
        out: dict[str, int] = {}
        for c in self._rowset.store.filter(tag=TAG_SUPERVISOR_RETRY):
            tgt = c.fields.get(F_SUP_TARGET_EFFECT_ID)
            if isinstance(tgt, str):
                out[tgt] = out.get(tgt, 0) + 1
        return out

    def _emit_batch(self, claims: list[Claim]) -> None:
        # Atomicity caveat: ``RowSet.fold`` is per-claim. A crash mid-loop
        # leaves a partial batch. B1 (durable storage) collapses this to
        # an atomic write; until then, recovery from the log is sufficient
        # because every supervisor_* claim is independently meaningful
        # (no cross-claim invariants within a single tick's emissions).
        for c in claims:
            self._rowset.fold(c)

    @staticmethod
    def _validate_action(action: object, rule_name: str) -> None:
        if not isinstance(action, (RetryAction, TerminateAction, EscalateAction)):
            raise TypeError(
                f"predicate {rule_name!r} returned "
                f"{type(action).__name__}, expected SupervisorAction "
                f"(RetryAction | TerminateAction | EscalateAction) or None"
            )

    def _action_to_claim(
        self, action: SupervisorAction, rule_name: str, attempt_index: int
    ) -> Claim:
        reason = f"[{rule_name}] {action.reason}"
        src = f"supervisor:{self._session_id}"

        if isinstance(action, RetryAction):
            idem = _gen_idempotency_key(
                self._session_id, action.target_effect_id, attempt_index
            )
            return Claim(
                tag=TAG_SUPERVISOR_RETRY,
                fields={
                    F_SUP_SCHEMA_VERSION:   SUPERVISOR_RETRY_V,
                    F_SUP_TARGET_EFFECT_ID: action.target_effect_id,
                    F_SUP_IDEMPOTENCY_KEY:  idem,
                    F_SUP_ATTEMPT_INDEX:    attempt_index,
                    F_SUP_MAX_ATTEMPTS:     action.max_attempts,
                    F_SUP_REASON:           reason,
                },
                source=src,
            )

        if isinstance(action, TerminateAction):
            return Claim(
                tag=TAG_SUPERVISOR_TERMINATE,
                fields={
                    F_SUP_SCHEMA_VERSION: SUPERVISOR_TERMINATE_V,
                    F_SUP_REASON:         reason,
                },
                source=src,
            )

        # EscalateAction — exhaustively typed by SupervisorAction.
        return Claim(
            tag=TAG_SUPERVISOR_ESCALATE,
            fields={
                F_SUP_SCHEMA_VERSION: SUPERVISOR_ESCALATE_V,
                F_SUP_REASON:         reason,
                F_SUP_PAYLOAD:        dict(action.payload),
            },
            source=src,
        )
