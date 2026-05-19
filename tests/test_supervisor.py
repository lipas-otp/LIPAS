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
"""
from __future__ import annotations

import pytest

from lipas.calculus import Claim
from lipas.rows.effect import EffectRow
from lipas.rows.history import HistoryRow
from lipas.supervisor import (
    EscalateAction,
    F_SUP_ATTEMPT_INDEX,
    F_SUP_IDEMPOTENCY_KEY,
    F_SUP_MAX_ATTEMPTS,
    F_SUP_PAYLOAD,
    F_SUP_REASON,
    F_SUP_SCHEMA_VERSION,
    F_SUP_TARGET_EFFECT_ID,
    Policy,
    PolicyRule,
    RetryAction,
    Supervisor,
    SUPERVISOR_ESCALATE_V,
    SUPERVISOR_RETRY_V,
    SUPERVISOR_TERMINATE_V,
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

        assert len(emitted) == 1
        c = emitted[0]
        assert c.tag == TAG_SUPERVISOR_TERMINATE
        assert c.fields[F_SUP_SCHEMA_VERSION] == SUPERVISOR_TERMINATE_V
        assert "[stop_now]" in c.fields[F_SUP_REASON]
        assert "budget tight" in c.fields[F_SUP_REASON]

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

        assert len(emitted) == 1
        c = emitted[0]
        assert c.tag == TAG_SUPERVISOR_ESCALATE
        assert c.fields[F_SUP_SCHEMA_VERSION] == SUPERVISOR_ESCALATE_V
        assert c.fields[F_SUP_PAYLOAD] == {
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
            # Look directly at the rowset's store via ctx? No — view is
            # the snapshot.  We want the EffectView path to be clean,
            # but B can still trivially observe the violation by
            # checking the underlying store.  However, supervisor
            # contract is "predicates use (view, ctx) only".  So we
            # check via the rowset reference snuck through closure as
            # the canonical way users WOULD detect a bug.
            seen_by_b["saw_a_claim"] = bool(
                rs.store.filter(tag=TAG_SUPERVISOR_RETRY)
            )
            return None

        sup = _make_supervisor(
            rs, PolicyRule("a", pred_a), PolicyRule("b", pred_b),
        )
        view, ctx = _view_and_ctx(rs)
        sup.tick(view, ctx)

        # A's claim must have been folded ONLY in phase 2 (after all
        # predicates ran). B saw an empty store.
        assert seen_by_b["saw_a_claim"] is False
        # And A's claim is in fact present after the tick.
        assert len(_claims_with_tag(rs, TAG_SUPERVISOR_RETRY)) == 1


# ── HistoryRow integration ──────────────────────────────────────────


class TestHistoryRowIntegration:

    def test_namespace_owns_supervisor_tags(self):
        ns = HistoryRow().namespace
        assert TAG_SUPERVISOR_RETRY in ns
        assert TAG_SUPERVISOR_TERMINATE in ns
        assert TAG_SUPERVISOR_ESCALATE in ns

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
        # 1 retry + 1 terminate = 2 owned events.
        assert proj["event_count"] >= 2

    def test_fold_succeeds_through_rowset(self, fresh_rowset):
        """Sanity: RowSet.fold accepts supervisor_* without invariants."""
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
        # No effect nodes → don't fire.
        # ≥1 orphan → fire.
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
        assert len(emitted) == 1
        assert emitted[0].tag == TAG_SUPERVISOR_TERMINATE
