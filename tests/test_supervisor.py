"""B3 — Supervisor tests.

Scope:
  - Tactic shapes: retry / terminate / escalate_human end-to-end.
  - Retry cap enforcement across ticks AND within a single tick
    (multiple predicates targeting the same effect).
  - Idempotency-key determinism / per-attempt uniqueness.
  - Snapshot isolation: predicates within one tick do not see each
    other's emissions.
  - HistoryRow ownership of supervisor_* tags + projection counts.
  - Construction validation.
  - P4: goal_blocked auto-pairing of terminate / escalate emissions.
"""
from __future__ import annotations

import pytest

from lipas.calculus import Claim
from lipas.rows.effect import EffectRow
from lipas.rows.history import HistoryRow
from lipas.supervisor import (
    EscalateAction,
    F_GB_PAYLOAD,
    F_GB_REASON,
    F_GB_SCHEMA_VERSION,
    F_GB_SOURCE_CLAIM_SEQ,
    F_GB_SOURCE_TACTIC,
    F_SUP_ATTEMPT_INDEX,
    F_SUP_IDEMPOTENCY_KEY,
    F_SUP_MAX_ATTEMPTS,
    F_SUP_PAYLOAD,
    F_SUP_REASON,
    F_SUP_SCHEMA_VERSION,
    F_SUP_TARGET_EFFECT_ID,
    GB_TACTIC_ESCALATE_HUMAN,
    GB_TACTIC_TERMINATE,
    GOAL_BLOCKED_V,
    Policy,
    PolicyRule,
    RetryAction,
    Supervisor,
    SUPERVISOR_ESCALATE_V,
    SUPERVISOR_RETRY_V,
    SUPERVISOR_TERMINATE_V,
    TAG_GOAL_BLOCKED,
    TAG_SUPERVISOR_ESCALATE,
    TAG_SUPERVISOR_RETRY,
    TAG_SUPERVISOR_TERMINATE,
    TerminateAction,
)


# ── helpers ──────────────────────────────────────────────────────────


def _view_and_ctx(rs):
    eff = next(r for r in rs.rows if isinstance(r, EffectRow))
    return eff.project(rs.store), rs.store.ctx


def _claims_with_tag(rs, tag):
    return rs.store.filter(tag=tag)


def _make_supervisor(rs, *rules, session_id="s-test"):
    return Supervisor(
        policy=Policy.of(*rules),
        rowset=rs,
        session_id=session_id,
    )


# ── construction ─────────────────────────────────────────────────────


class TestConstruction:

    def test_session_id_required(self, fresh_rowset):
        rs = fresh_rowset()
        with pytest.raises(ValueError):
            Supervisor(policy=Policy.of(), rowset=rs, session_id="")

    def test_session_id_must_be_str(self, fresh_rowset):
        rs = fresh_rowset()
        with pytest.raises(ValueError):
            Supervisor(policy=Policy.of(), rowset=rs, session_id=None)  # type: ignore[arg-type]

    def test_empty_policy_is_valid(self, fresh_rowset):
        rs = fresh_rowset()
        sup = _make_supervisor(rs)  # no rules
        view, ctx = _view_and_ctx(rs)
        emitted = sup.tick(view, ctx)
        assert emitted == []
        assert len(rs.store) == 0


# ── tick basics ──────────────────────────────────────────────────────


class TestTickBasics:

    def test_predicate_returns_none_emits_nothing(self, fresh_rowset):
        rs = fresh_rowset()
        sup = _make_supervisor(
            rs, PolicyRule("noop", lambda v, c: None),
        )
        view, ctx = _view_and_ctx(rs)
        assert sup.tick(view, ctx) == []
        assert len(rs.store) == 0

    def test_predicate_invalid_return_type_raises(self, fresh_rowset):
        rs = fresh_rowset()
        sup = _make_supervisor(
            rs, PolicyRule("bad", lambda v, c: "not an action"),
        )
        view, ctx = _view_and_ctx(rs)
        with pytest.raises(TypeError):
            sup.tick(view, ctx)

    def test_predicate_receives_view_and_ctx(self, fresh_rowset):
        rs = fresh_rowset()
        seen = {}

        def pred(view, ctx):
            seen["view"] = view
            seen["ctx"] = ctx
            return None

        sup = _make_supervisor(rs, PolicyRule("snoop", pred))
        view, ctx = _view_and_ctx(rs)
        sup.tick(view, ctx)
        assert seen["view"] is view
        assert seen["ctx"] is ctx


# ── retry tactic ─────────────────────────────────────────────────────


class TestRetry:

    def test_retry_emits_supervisor_retry_claim(self, fresh_rowset):
        rs = fresh_rowset()
        sup = _make_supervisor(
            rs,
            PolicyRule(
                "always_retry",
                lambda v, c: RetryAction(
                    target_effect_id="call_aaaaaaaaaaaa",
                    max_attempts=3,
                    reason="testing",
                ),
            ),
        )
        view, ctx = _view_and_ctx(rs)
        emitted = sup.tick(view, ctx)

        # Retry does NOT trigger a goal_blocked pair.
        assert len(emitted) == 1
        c = emitted[0]
        assert c.tag == TAG_SUPERVISOR_RETRY
        assert c.fields[F_SUP_TARGET_EFFECT_ID] == "call_aaaaaaaaaaaa"
        assert c.fields[F_SUP_MAX_ATTEMPTS] == 3
        assert c.fields[F_SUP_ATTEMPT_INDEX] == 1
        assert c.fields[F_SUP_SCHEMA_VERSION] == SUPERVISOR_RETRY_V
        assert "[always_retry]" in c.fields[F_SUP_REASON]
        assert isinstance(c.fields[F_SUP_IDEMPOTENCY_KEY], str)
        assert len(c.fields[F_SUP_IDEMPOTENCY_KEY]) == 32

    def test_retry_does_not_emit_goal_blocked(self, fresh_rowset):
        rs = fresh_rowset()
        sup = _make_supervisor(
            rs,
            PolicyRule("r", lambda v, c: RetryAction(
                target_effect_id="call_aaaaaaaaaaaa",
                max_attempts=2, reason="t",
            )),
        )
        view, ctx = _view_and_ctx(rs)
        sup.tick(view, ctx)
        # goal_blocked is paired ONLY with terminate / escalate.
        assert _claims_with_tag(rs, TAG_GOAL_BLOCKED) == []

    def test_retry_attempt_index_monotonic_across_ticks(self, fresh_rowset):
        rs = fresh_rowset()
        sup = _make_supervisor(
            rs,
            PolicyRule(
                "r",
                lambda v, c: RetryAction(
                    target_effect_id="call_aaaaaaaaaaaa",
                    max_attempts=10,
                    reason="t",
                ),
            ),
        )
        for _ in range(3):
            view, ctx = _view_and_ctx(rs)
            sup.tick(view, ctx)

        claims = _claims_with_tag(rs, TAG_SUPERVISOR_RETRY)
        indices = [c.fields[F_SUP_ATTEMPT_INDEX] for c in claims]
        assert indices == [1, 2, 3]

    def test_retry_cap_enforced_across_ticks(self, fresh_rowset):
        rs = fresh_rowset()
        sup = _make_supervisor(
            rs,
            PolicyRule(
                "r",
                lambda v, c: RetryAction(
                    target_effect_id="call_aaaaaaaaaaaa",
                    max_attempts=2,
                    reason="t",
                ),
            ),
        )
        for _ in range(5):
            view, ctx = _view_and_ctx(rs)
            sup.tick(view, ctx)

        # Only 2 retries should ever be recorded.
        claims = _claims_with_tag(rs, TAG_SUPERVISOR_RETRY)
        assert len(claims) == 2

    def test_retry_cap_enforced_within_single_tick(self, fresh_rowset):
        """Two predicates in the same tick both want to retry the same
        target. The cap must limit total emissions, not per-predicate."""
        rs = fresh_rowset()
        target = "call_aaaaaaaaaaaa"

        def mk_action(name):
            return lambda v, c: RetryAction(
                target_effect_id=target, max_attempts=2, reason=name,
            )

        sup = _make_supervisor(
            rs,
            PolicyRule("p1", mk_action("p1")),
            PolicyRule("p2", mk_action("p2")),
            PolicyRule("p3", mk_action("p3")),
        )
        view, ctx = _view_and_ctx(rs)
        emitted = sup.tick(view, ctx)

        assert len(emitted) == 2  # capped at max_attempts
        indices = [c.fields[F_SUP_ATTEMPT_INDEX] for c in emitted]
        assert indices == [1, 2]

    def test_retry_cap_distinct_targets_independent(self, fresh_rowset):
        rs = fresh_rowset()
        sup = _make_supervisor(
            rs,
            PolicyRule("a", lambda v, c: RetryAction(
                target_effect_id="call_aaaaaaaaaaaa",
                max_attempts=2, reason="a")),
            PolicyRule("b", lambda v, c: RetryAction(
                target_effect_id="call_bbbbbbbbbbbb",
                max_attempts=2, reason="b")),
        )
        for _ in range(3):
            view, ctx = _view_and_ctx(rs)
            sup.tick(view, ctx)

        a_claims = [
            c for c in _claims_with_tag(rs, TAG_SUPERVISOR_RETRY)
            if c.fields[F_SUP_TARGET_EFFECT_ID] == "call_aaaaaaaaaaaa"
        ]
        b_claims = [
            c for c in _claims_with_tag(rs, TAG_SUPERVISOR_RETRY)
            if c.fields[F_SUP_TARGET_EFFECT_ID] == "call_bbbbbbbbbbbb"
        ]
        assert len(a_claims) == 2
        assert len(b_claims) == 2

    def test_retry_idempotency_key_deterministic(self, fresh_rowset):
        rs1 = fresh_rowset()
        rs2 = fresh_rowset()

        def make(rs):
            return _make_supervisor(
                rs,
                PolicyRule("r", lambda v, c: RetryAction(
                    target_effect_id="call_aaaaaaaaaaaa",
                    max_attempts=1, reason="t",
                )),
                session_id="s-fixed",
            )

        for rs in (rs1, rs2):
            sup = make(rs)
            view, ctx = _view_and_ctx(rs)
            sup.tick(view, ctx)

        k1 = _claims_with_tag(rs1, TAG_SUPERVISOR_RETRY)[0].fields[F_SUP_IDEMPOTENCY_KEY]
        k2 = _claims_with_tag(rs2, TAG_SUPERVISOR_RETRY)[0].fields[F_SUP_IDEMPOTENCY_KEY]
        assert k1 == k2

    def test_retry_idempotency_key_varies_with_attempt(self, fresh_rowset):
        rs = fresh_rowset()
        sup = _make_supervisor(
            rs,
            PolicyRule("r", lambda v, c: RetryAction(
                target_effect_id="call_aaaaaaaaaaaa",
                max_attempts=3, reason="t",
            )),
        )
        for _ in range(3):
            view, ctx = _view_and_ctx(rs)
            sup.tick(view, ctx)

        keys = [
            c.fields[F_SUP_IDEMPOTENCY_KEY]
            for c in _claims_with_tag(rs, TAG_SUPERVISOR_RETRY)
        ]
        assert len(set(keys)) == 3

    def test_retry_idempotency_key_varies_with_session(self, fresh_rowset):
        rs1 = fresh_rowset()
        rs2 = fresh_rowset()

        for rs, sid in [(rs1, "session-A"), (rs2, "session-B")]:
            sup = _make_supervisor(
                rs,
                PolicyRule("r", lambda v, c: RetryAction(
                    target_effect_id="call_aaaaaaaaaaaa",
                    max_attempts=1, reason="t",
                )),
                session_id=sid,
            )
            view, ctx = _view_and_ctx(rs)
            sup.tick(view, ctx)

        k1 = _claims_with_tag(rs1, TAG_SUPERVISOR_RETRY)[0].fields[F_SUP_IDEMPOTENCY_KEY]
        k2 = _claims_with_tag(rs2, TAG_SUPERVISOR_RETRY)[0].fields[F_SUP_IDEMPOTENCY_KEY]
        assert k1 != k2


# ── terminate tactic ─────────────────────────────────────────────────


class TestTerminate:

    def test_terminate_emits_correct_claim(self, fresh_rowset):
        rs = fresh_rowset()
        sup = _make_supervisor(
            rs,
            PolicyRule(
                "stop_now",
                lambda v, c: TerminateAction(reason="budget tight"),
            ),
        )
        view, ctx = _view_and_ctx(rs)
        emitted = sup.tick(view, ctx)

        # P4: terminate now auto-pairs with goal_blocked.
        assert len(emitted) == 2
        term, gb = emitted
        assert term.tag == TAG_SUPERVISOR_TERMINATE
        assert gb.tag   == TAG_GOAL_BLOCKED
        assert term.fields[F_SUP_SCHEMA_VERSION] == SUPERVISOR_TERMINATE_V
        assert "[stop_now]" in term.fields[F_SUP_REASON]
        assert "budget tight" in term.fields[F_SUP_REASON]

    def test_terminate_no_attempt_or_idempotency_fields(self, fresh_rowset):
        rs = fresh_rowset()
        sup = _make_supervisor(
            rs,
            PolicyRule("t", lambda v, c: TerminateAction(reason="x")),
        )
        view, ctx = _view_and_ctx(rs)
        sup.tick(view, ctx)
        c = _claims_with_tag(rs, TAG_SUPERVISOR_TERMINATE)[0]
        assert F_SUP_ATTEMPT_INDEX not in c.fields
        assert F_SUP_IDEMPOTENCY_KEY not in c.fields


# ── escalate tactic ──────────────────────────────────────────────────


class TestEscalate:

    def test_escalate_with_payload(self, fresh_rowset):
        rs = fresh_rowset()
        sup = _make_supervisor(
            rs,
            PolicyRule(
                "esc",
                lambda v, c: EscalateAction(
                    reason="ambiguous instruction",
                    payload={"channel": "slack", "priority": "P2"},
                ),
            ),
        )
        view, ctx = _view_and_ctx(rs)
        emitted = sup.tick(view, ctx)

        # P4: escalate now auto-pairs with goal_blocked.
        assert len(emitted) == 2
        esc, gb = emitted
        assert esc.tag == TAG_SUPERVISOR_ESCALATE
        assert gb.tag  == TAG_GOAL_BLOCKED
        assert esc.fields[F_SUP_SCHEMA_VERSION] == SUPERVISOR_ESCALATE_V
        assert esc.fields[F_SUP_PAYLOAD] == {
            "channel": "slack", "priority": "P2",
        }

    def test_escalate_payload_defensively_copied(self, fresh_rowset):
        rs = fresh_rowset()
        original_payload = {"a": 1}
        sup = _make_supervisor(
            rs,
            PolicyRule(
                "e",
                lambda v, c: EscalateAction(
                    reason="x", payload=original_payload,
                ),
            ),
        )
        view, ctx = _view_and_ctx(rs)
        sup.tick(view, ctx)

        # Mutating the source must not leak into the recorded claim.
        original_payload["a"] = 999
        c = _claims_with_tag(rs, TAG_SUPERVISOR_ESCALATE)[0]
        assert c.fields[F_SUP_PAYLOAD] == {"a": 1}


# ── P4: goal_blocked auto-pairing ────────────────────────────────────


class TestGoalBlockedPairing:
    """Phase 4 — every terminate / escalate is paired with a
    goal_blocked claim at seq+1, with source_claim_seq pointing back
    and source_tactic matching the trigger."""

    def test_terminate_pairs_with_goal_blocked(self, fresh_rowset):
        rs = fresh_rowset()
        sup = _make_supervisor(
            rs, PolicyRule("t", lambda v, c: TerminateAction(reason="why")),
        )
        view, ctx = _view_and_ctx(rs)
        emitted = sup.tick(view, ctx)

        assert len(emitted) == 2
        term, gb = emitted
        assert term.tag == TAG_SUPERVISOR_TERMINATE
        assert gb.tag   == TAG_GOAL_BLOCKED

        # Adjacency: gb is immediately after term in the store.
        assert gb.seq == term.seq + 1

        # Back-reference + tactic + version.
        assert gb.fields[F_GB_SOURCE_CLAIM_SEQ] == term.seq
        assert gb.fields[F_GB_SOURCE_TACTIC]    == GB_TACTIC_TERMINATE
        assert gb.fields[F_GB_SCHEMA_VERSION]   == GOAL_BLOCKED_V

        # Reason copied verbatim from the trigger.
        assert gb.fields[F_GB_REASON] == term.fields[F_SUP_REASON]

        # No payload on terminate-paired goal_blocked.
        assert F_GB_PAYLOAD not in gb.fields

    def test_stable_tick_repairs_crash_between_terminate_and_pair(
        self, fresh_rowset,
    ):
        class SimulatedCrash(BaseException):
            pass

        rs = fresh_rowset()
        sup = _make_supervisor(
            rs, PolicyRule("t", lambda v, c: TerminateAction(reason="why")),
        )
        view, ctx = _view_and_ctx(rs)
        original_fold = rs.fold

        def crash_on_goal_blocked(claim):
            if claim.tag == TAG_GOAL_BLOCKED:
                raise SimulatedCrash()
            return original_fold(claim)

        rs.fold = crash_on_goal_blocked
        with pytest.raises(SimulatedCrash):
            sup.tick(view, ctx, claim_id_prefix="durable:tick:0")
        rs.fold = original_fold

        repaired = sup.tick(
            *_view_and_ctx(rs), claim_id_prefix="durable:tick:0",
        )
        terminations = _claims_with_tag(rs, TAG_SUPERVISOR_TERMINATE)
        blocked = _claims_with_tag(rs, TAG_GOAL_BLOCKED)

        assert len(terminations) == len(blocked) == 1
        assert [claim.tag for claim in repaired] == [
            TAG_SUPERVISOR_TERMINATE, TAG_GOAL_BLOCKED,
        ]
        assert blocked[0].seq == terminations[0].seq + 1
        assert blocked[0].fields[F_GB_SOURCE_CLAIM_SEQ] == terminations[0].seq

    def test_escalate_pairs_with_goal_blocked_carrying_payload(
        self, fresh_rowset,
    ):
        rs = fresh_rowset()
        payload = {"channel": "ops", "priority": "P1"}
        sup = _make_supervisor(
            rs,
            PolicyRule("e", lambda v, c: EscalateAction(
                reason="x", payload=payload,
            )),
        )
        view, ctx = _view_and_ctx(rs)
        emitted = sup.tick(view, ctx)

        assert len(emitted) == 2
        esc, gb = emitted
        assert esc.tag == TAG_SUPERVISOR_ESCALATE
        assert gb.tag  == TAG_GOAL_BLOCKED

        assert gb.seq == esc.seq + 1
        assert gb.fields[F_GB_SOURCE_CLAIM_SEQ] == esc.seq
        assert gb.fields[F_GB_SOURCE_TACTIC]    == GB_TACTIC_ESCALATE_HUMAN
        assert gb.fields[F_GB_PAYLOAD]          == payload

    def test_escalate_goal_blocked_payload_independent_of_source(
        self, fresh_rowset,
    ):
        """The payload on goal_blocked is defensively copied — mutating
        the EscalateAction's payload post-fold MUST not leak in."""
        rs = fresh_rowset()
        payload = {"k": 1}
        sup = _make_supervisor(
            rs,
            PolicyRule("e", lambda v, c: EscalateAction(
                reason="x", payload=payload,
            )),
        )
        view, ctx = _view_and_ctx(rs)
        sup.tick(view, ctx)

        payload["k"] = 999
        gb = _claims_with_tag(rs, TAG_GOAL_BLOCKED)[0]
        assert gb.fields[F_GB_PAYLOAD] == {"k": 1}

    def test_mixed_batch_pairing(self, fresh_rowset):
        """A tick with [retry, terminate, escalate] should fold:
            retry, terminate, gb_for_terminate, escalate, gb_for_escalate
        — and the two gb claims point at the right triggers."""
        rs = fresh_rowset()
        sup = _make_supervisor(
            rs,
            PolicyRule("r", lambda v, c: RetryAction(
                target_effect_id="call_aaaaaaaaaaaa",
                max_attempts=3, reason="r",
            )),
            PolicyRule("t", lambda v, c: TerminateAction(reason="t")),
            PolicyRule("e", lambda v, c: EscalateAction(
                reason="e", payload={"x": 1},
            )),
        )
        view, ctx = _view_and_ctx(rs)
        emitted = sup.tick(view, ctx)

        assert [c.tag for c in emitted] == [
            TAG_SUPERVISOR_RETRY,
            TAG_SUPERVISOR_TERMINATE,
            TAG_GOAL_BLOCKED,
            TAG_SUPERVISOR_ESCALATE,
            TAG_GOAL_BLOCKED,
        ]

        # Verify pairing references.
        _, term, gb_t, esc, gb_e = emitted
        assert gb_t.seq == term.seq + 1
        assert gb_t.fields[F_GB_SOURCE_CLAIM_SEQ] == term.seq
        assert gb_t.fields[F_GB_SOURCE_TACTIC]    == GB_TACTIC_TERMINATE

        assert gb_e.seq == esc.seq + 1
        assert gb_e.fields[F_GB_SOURCE_CLAIM_SEQ] == esc.seq
        assert gb_e.fields[F_GB_SOURCE_TACTIC]    == GB_TACTIC_ESCALATE_HUMAN

    def test_two_terminates_in_one_tick_each_get_pair(self, fresh_rowset):
        """Two predicates both fire TerminateAction. Both should be
        folded, and EACH should get its own goal_blocked."""
        rs = fresh_rowset()
        sup = _make_supervisor(
            rs,
            PolicyRule("t1", lambda v, c: TerminateAction(reason="r1")),
            PolicyRule("t2", lambda v, c: TerminateAction(reason="r2")),
        )
        view, ctx = _view_and_ctx(rs)
        emitted = sup.tick(view, ctx)

        assert [c.tag for c in emitted] == [
            TAG_SUPERVISOR_TERMINATE,
            TAG_GOAL_BLOCKED,
            TAG_SUPERVISOR_TERMINATE,
            TAG_GOAL_BLOCKED,
        ]
        t1, gb1, t2, gb2 = emitted
        assert gb1.seq == t1.seq + 1
        assert gb2.seq == t2.seq + 1
        assert gb1.fields[F_GB_SOURCE_CLAIM_SEQ] == t1.seq
        assert gb2.fields[F_GB_SOURCE_CLAIM_SEQ] == t2.seq

    def test_pairing_passes_lint(self, fresh_rowset):
        """Sanity: claims produced by Supervisor are lint-clean."""
        from lipas.lint import lint_store
        rs = fresh_rowset()
        sup = _make_supervisor(
            rs,
            PolicyRule("t", lambda v, c: TerminateAction(reason="x")),
            PolicyRule("e", lambda v, c: EscalateAction(
                reason="y", payload={"k": 1},
            )),
        )
        view, ctx = _view_and_ctx(rs)
        sup.tick(view, ctx)
        assert lint_store(rs.store) == []


# ── snapshot isolation (C2) ──────────────────────────────────────────


class TestSnapshotIsolation:

    def test_predicate_does_not_see_earlier_predicate_emission(
        self, fresh_rowset,
    ):
        """In a single tick, predicate B fires AFTER predicate A. B's
        view of the store must NOT include A's just-emitted claim."""
        rs = fresh_rowset()
        seen_by_b = {"saw_a_claim": False}

        def pred_a(view, ctx):
            return RetryAction(
                target_effect_id="call_aaaaaaaaaaaa",
                max_attempts=5, reason="a",
            )

        def pred_b(view, ctx):
            # B observes via the rowset directly — the canonical way
            # users WOULD detect a contract bug.  Supervisor must have
            # not folded A's claim yet at this point.
            seen_by_b["saw_a_claim"] = bool(
                rs.store.filter(tag=TAG_SUPERVISOR_RETRY)
            )
            return None

        sup = _make_supervisor(
            rs, PolicyRule("a", pred_a), PolicyRule("b", pred_b),
        )
        view, ctx = _view_and_ctx(rs)
        sup.tick(view, ctx)

        assert seen_by_b["saw_a_claim"] is False
        assert len(_claims_with_tag(rs, TAG_SUPERVISOR_RETRY)) == 1


# ── HistoryRow integration ──────────────────────────────────────────


class TestHistoryRowIntegration:

    def test_namespace_owns_supervisor_and_goal_blocked_tags(self):
        ns = HistoryRow().namespace
        assert TAG_SUPERVISOR_RETRY     in ns
        assert TAG_SUPERVISOR_TERMINATE in ns
        assert TAG_SUPERVISOR_ESCALATE  in ns
        assert TAG_GOAL_BLOCKED         in ns

    def test_supervisor_claims_visible_in_event_count(self, fresh_rowset):
        rs = fresh_rowset()
        sup = _make_supervisor(
            rs,
            PolicyRule("r", lambda v, c: RetryAction(
                target_effect_id="call_aaaaaaaaaaaa",
                max_attempts=2, reason="t",
            )),
            PolicyRule("t", lambda v, c: TerminateAction(reason="t")),
        )
        view, ctx = _view_and_ctx(rs)
        sup.tick(view, ctx)

        hist = next(r for r in rs.rows if isinstance(r, HistoryRow))
        proj = hist.project(rs.store)
        # 1 retry + 1 terminate + 1 goal_blocked = 3 owned events.
        assert proj["event_count"] >= 3

    def test_fold_succeeds_through_rowset(self, fresh_rowset):
        """Sanity: RowSet.fold accepts supervisor_* + goal_blocked
        without invariant violations."""
        rs = fresh_rowset()
        sup = _make_supervisor(
            rs, PolicyRule("t", lambda v, c: TerminateAction(reason="x")),
        )
        view, ctx = _view_and_ctx(rs)
        # Must not raise InvariantViolation.
        sup.tick(view, ctx)


# ── reading EffectView in predicates ────────────────────────────────


class TestPredicateReadsView:
    """Sanity: predicates can in fact branch on EffectView contents."""

    def test_predicate_branches_on_view_node_count(self, fresh_rowset):
        rs = fresh_rowset()
        decisions: list[bool] = []

        def pred(view, ctx):
            decisions.append(bool(view.orphans))
            if view.orphans:
                return TerminateAction(reason=f"orphans={len(view.orphans)}")
            return None

        sup = _make_supervisor(rs, PolicyRule("orphan_watch", pred))

        # Tick 1 — empty store, no orphans.
        view, ctx = _view_and_ctx(rs)
        emitted = sup.tick(view, ctx)
        assert emitted == []
        assert decisions[-1] is False

        # Manually fold an orphan effect_intent.
        from lipas.effect import EffectKind
        from lipas.rows.effect import (
            F_EFFECT_ID, F_KIND, F_MODEL, F_REQUEST,
            TAG_EFFECT_INTENT,
        )
        rs.fold(Claim(
            tag=TAG_EFFECT_INTENT,
            fields={
                F_EFFECT_ID: "call_111111111111",
                F_KIND:      EffectKind.LLM_CALL.value,
                F_MODEL:     "test-model",
                F_REQUEST:   {"messages": []},
            },
            source="test",
        ))

        # Tick 2 — orphan present, predicate fires.
        view, ctx = _view_and_ctx(rs)
        emitted = sup.tick(view, ctx)
        assert decisions[-1] is True
        # Terminate + goal_blocked.
        assert len(emitted) == 2
        assert emitted[0].tag == TAG_SUPERVISOR_TERMINATE
        assert emitted[1].tag == TAG_GOAL_BLOCKED
