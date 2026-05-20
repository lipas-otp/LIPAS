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

C5. **Goal-blocked pairing (P4)**. Every ``supervisor_terminate`` and
    ``supervisor_escalate`` claim folded by ``_emit_batch`` MUST be
    immediately followed by a ``goal_blocked`` claim within the same
    batch (i.e. the next ``seq``) whose ``source_claim_seq`` equals the
    triggering claim's seq and whose ``source_tactic`` matches the
    triggering tactic. This is a STRUCTURAL invariant, not a
    convention — it is the substrate the ``goal_blocked_pairing`` lint
    relies on, and it can only be enforced at the fold site (``_emit_batch``)
    because no loop-layer ordering guarantees can recover it post-hoc.

The first batch of tactics is ``retry / terminate / escalate_human``.
The second batch (``degrade / circuit_break / compensate``) is deferred
until concrete use cases land.

Schema notes
------------
Each supervisor_* / goal_blocked claim carries ``schema_version`` in
``fields`` to anticipate A2 (Claim Schema Evolution). When A2 lands,
this field becomes the canonical entry point for upcasters. Until then
it is purely informational.
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
    "TAG_GOAL_BLOCKED",
    # Schema versions
    "SUPERVISOR_RETRY_V",
    "SUPERVISOR_TERMINATE_V",
    "SUPERVISOR_ESCALATE_V",
    "GOAL_BLOCKED_V",
    # supervisor_* field-name constants
    "F_SUP_SCHEMA_VERSION",
    "F_SUP_TARGET_EFFECT_ID",
    "F_SUP_IDEMPOTENCY_KEY",
    "F_SUP_ATTEMPT_INDEX",
    "F_SUP_MAX_ATTEMPTS",
    "F_SUP_REASON",
    "F_SUP_PAYLOAD",
    # goal_blocked field-name constants
    "F_GB_SCHEMA_VERSION",
    "F_GB_SOURCE_TACTIC",
    "F_GB_SOURCE_CLAIM_SEQ",
    "F_GB_REASON",
    "F_GB_PAYLOAD",
    # goal_blocked tactic values
    "GB_TACTIC_TERMINATE",
    "GB_TACTIC_ESCALATE_HUMAN",
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
TAG_GOAL_BLOCKED         = "goal_blocked"

SUPERVISOR_RETRY_V     = 1
SUPERVISOR_TERMINATE_V = 1
SUPERVISOR_ESCALATE_V  = 1
GOAL_BLOCKED_V         = 1

# supervisor_* fields
F_SUP_SCHEMA_VERSION   = "schema_version"
F_SUP_TARGET_EFFECT_ID = "target_effect_id"
F_SUP_IDEMPOTENCY_KEY  = "idempotency_key"
F_SUP_ATTEMPT_INDEX    = "attempt_index"
F_SUP_MAX_ATTEMPTS     = "max_attempts"
F_SUP_REASON           = "reason"
F_SUP_PAYLOAD          = "payload"

# goal_blocked fields
F_GB_SCHEMA_VERSION   = "schema_version"
F_GB_SOURCE_TACTIC    = "source_tactic"
F_GB_SOURCE_CLAIM_SEQ = "source_claim_seq"
F_GB_REASON           = "reason"
F_GB_PAYLOAD          = "payload"

# goal_blocked tactic value-set (mirrors Tactic.*.value for the two
# tactics that can produce a goal-block; kept as plain constants to
# avoid coupling tape readers to the Tactic enum).
GB_TACTIC_TERMINATE      = "terminate"
GB_TACTIC_ESCALATE_HUMAN = "escalate_human"


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
    """See module docstring for normative contracts C1–C5.

    Construction
    ------------
    policy:
        Ordered ``(name, predicate)`` pairs. Order is for audit
        determinism only; predicates MUST NOT depend on it.
    rowset:
        Sink for supervisor_* / goal_blocked claims; also the source of
        truth for the retry-cap tally. The rowset's HistoryRow SHOULD
        list ``TAG_SUPERVISOR_*`` and ``TAG_GOAL_BLOCKED`` in its
        namespace (see ``rows/history.py``).
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

    The returned list is the claims folded in this tick, in fold order.
    It INCLUDES auto-paired ``goal_blocked`` claims that immediately
    follow ``supervisor_terminate`` / ``supervisor_escalate``.
    Behaviours MAY scan it for terminate / escalate to perform early
    loop exit (see ReActAgent) — goal_blocked claims appear after their
    triggers and are transparent to loop-exit logic.
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

        Returns the claims that were folded, in fold order, including
        auto-paired goal_blocked claims. Source of truth is still the
        rowset's store; the return value is a convenience for
        behaviours that want to react to terminate / escalate without
        re-querying.
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

        # Phase 2 — convert to claims and fold (with goal_blocked pairing).
        claims = [
            self._action_to_claim(a, name, idx)
            for (a, name, idx) in pending
        ]
        return self._emit_batch(claims)

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

    def _emit_batch(self, claims: list[Claim]) -> list[Claim]:
        """Fold all claims; auto-pair terminate/escalate with goal_blocked.

        STRUCTURAL INVARIANT (C5)
        -------------------------
        For every terminate/escalate claim folded here, the very next
        claim folded MUST be a ``goal_blocked`` whose
        ``source_claim_seq`` equals the triggering claim's seq.

        This is the only fold site that can guarantee adjacency-by-seq
        between the trigger and its goal_blocked. Other layers (loops,
        replay) MUST NOT inject claims between them; doing so will
        trip the ``goal_blocked_pairing`` lint.

        Atomicity caveat: ``RowSet.fold`` is per-claim. A crash mid-loop
        between a terminate and its goal_blocked leaves an unpaired
        terminate. B1 (durable storage) will collapse this to an atomic
        write; until then, the lint flags such orphans on recovery.

        Implementation note
        -------------------
        ``Claim`` is frozen; ``RowSet.fold`` does NOT write ``seq`` back
        into the caller's instance — it stores a seq-assigned copy in
        ``store.log``. We therefore re-fetch from ``store.log[-1]``
        after each fold so both (a) the goal_blocked's
        ``source_claim_seq`` and (b) the returned list see the correct seq.
        """
        out: list[Claim] = []
        for c in claims:
            self._rowset.fold(c)
            folded = self._rowset.store.log[-1]
            out.append(folded)
            if folded.tag in (TAG_SUPERVISOR_TERMINATE, TAG_SUPERVISOR_ESCALATE):
                gb = self._build_goal_blocked(folded, folded.seq)
                self._rowset.fold(gb)
                out.append(self._rowset.store.log[-1])
        return out

    # def _last_folded_seq(self) -> int:
    #     """Return the seq of the most-recently-folded claim.
    #
    #     Reads from ``store.log[-1]`` so we don't depend on whether
    #     ``Claim`` instances are mutated in place by fold.
    #     """
    #     return self._rowset.store.log[-1].seq

    def _build_goal_blocked(self, source: Claim, source_seq: int) -> Claim:
        """Construct the goal_blocked claim paired to a terminate/escalate.

        Field choices (P4, locked):
          - source_tactic    : the tactic string of the triggering claim;
                               used by lint to verify trigger/pair tag
                               consistency.
          - source_claim_seq : the seq of the triggering claim; the
                               primary back-reference. Mandatory.
          - reason           : copied verbatim from the triggering claim
                               so tape readers don't have to cross-ref.
          - payload          : present iff the trigger is escalate;
                               carries the same payload (defensively
                               copied through the trigger already).
        """
        if source.tag == TAG_SUPERVISOR_TERMINATE:
            tactic = GB_TACTIC_TERMINATE
            payload = None
        elif source.tag == TAG_SUPERVISOR_ESCALATE:
            tactic = GB_TACTIC_ESCALATE_HUMAN
            payload = dict(source.fields.get(F_SUP_PAYLOAD, {}))
        else:
            # Defensive — only the two tags above reach this builder.
            raise AssertionError(
                f"_build_goal_blocked called with unexpected tag "
                f"{source.tag!r}"
            )

        fields: dict = {
            F_GB_SCHEMA_VERSION:   GOAL_BLOCKED_V,
            F_GB_SOURCE_TACTIC:    tactic,
            F_GB_SOURCE_CLAIM_SEQ: source_seq,
            F_GB_REASON:           source.fields.get(F_SUP_REASON, ""),
        }
        if payload is not None:
            fields[F_GB_PAYLOAD] = payload

        return Claim(
            tag=TAG_GOAL_BLOCKED,
            fields=fields,
            source=f"supervisor:{self._session_id}",
        )

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
