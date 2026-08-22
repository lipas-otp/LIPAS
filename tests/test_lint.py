"""P4 — lint tests.

Scope:
  - goal_blocked_pairing rule: catches missing pair, wrong tag at
    seq+1, wrong source_claim_seq, wrong source_tactic.
  - LintViolation shape: anchor seq + related_seqs.
  - lint_store: deterministic ordering, empty list = clean.

These tests fold claims DIRECTLY into the store (not via Supervisor)
so the lint can be exercised against malformed inputs that Supervisor
itself would never produce.
"""
from __future__ import annotations

import pytest

from lipas.calculus import Claim
from lipas.lint import (
    LintViolation,
    lint_goal_blocked_pairing,
    lint_store,
)
from lipas.supervisor import (
    F_GB_SCHEMA_VERSION,
    F_GB_SOURCE_CLAIM_SEQ,
    F_GB_SOURCE_TACTIC,
    F_SUP_SCHEMA_VERSION,
    F_SUP_REASON,
    GB_TACTIC_ESCALATE_HUMAN,
    GB_TACTIC_TERMINATE,
    GOAL_BLOCKED_V,
    Policy,
    PolicyRule,
    SUPERVISOR_ESCALATE_V,
    SUPERVISOR_TERMINATE_V,
    Supervisor,
    TAG_GOAL_BLOCKED,
    TAG_SUPERVISOR_ESCALATE,
    TAG_SUPERVISOR_TERMINATE,
    TerminateAction,
)


# ── helpers: hand-built claims ───────────────────────────────────────


def _fold_term(rs, reason: str = "r") -> int:
    """Fold a bare supervisor_terminate directly into the store.
    Returns the seq it was assigned."""
    rs.fold(Claim(
        tag=TAG_SUPERVISOR_TERMINATE,
        fields={
            F_SUP_SCHEMA_VERSION: SUPERVISOR_TERMINATE_V,
            F_SUP_REASON:         reason,
        },
        source="test",
    ))
    return rs.store.log[-1].seq


def _fold_esc(rs, reason: str = "r") -> int:
    rs.fold(Claim(
        tag=TAG_SUPERVISOR_ESCALATE,
        fields={
            F_SUP_SCHEMA_VERSION: SUPERVISOR_ESCALATE_V,
            F_SUP_REASON:         reason,
            "payload":            {},
        },
        source="test",
    ))
    return rs.store.log[-1].seq


def _fold_gb(
    rs,
    *,
    source_claim_seq: int,
    source_tactic:   str,
    reason:          str = "r",
) -> int:
    rs.fold(Claim(
        tag=TAG_GOAL_BLOCKED,
        fields={
            F_GB_SCHEMA_VERSION:   GOAL_BLOCKED_V,
            F_GB_SOURCE_TACTIC:    source_tactic,
            F_GB_SOURCE_CLAIM_SEQ: source_claim_seq,
            "reason":              reason,
        },
        source="test",
    ))
    return rs.store.log[-1].seq


def _fold_unrelated(rs) -> int:
    """A neutral history-namespace claim to inject between others."""
    rs.fold(Claim(
        tag="observation",
        fields={"_history": [{"note": "filler"}]},
        source="test",
    ))
    return rs.store.log[-1].seq


# ── empty / clean store ──────────────────────────────────────────────


class TestCleanStore:

    def test_empty_store_passes(self, fresh_rowset):
        rs = fresh_rowset()
        assert lint_store(rs.store) == []

    def test_only_unrelated_claims_pass(self, fresh_rowset):
        rs = fresh_rowset()
        for _ in range(3):
            _fold_unrelated(rs)
        assert lint_store(rs.store) == []

    def test_supervisor_produced_pair_passes(self, fresh_rowset):
        rs = fresh_rowset()
        sup = Supervisor(
            policy=Policy.of(
                PolicyRule("t", lambda v, c: TerminateAction(reason="ok")),
            ),
            rowset=rs,
            session_id="s",
        )
        from lipas.rows.effect import EffectRow
        eff = next(r for r in rs.rows if isinstance(r, EffectRow))
        sup.tick(eff.project(rs.store), rs.store.ctx)
        assert lint_store(rs.store) == []


# ── goal_blocked_pairing: failure modes ──────────────────────────────


class TestMissingPair:

    def test_terminate_with_no_successor_fails(self, fresh_rowset):
        rs = fresh_rowset()
        term_seq = _fold_term(rs)
        violations = lint_store(rs.store)
        assert len(violations) == 1
        v = violations[0]
        assert v.rule == "goal_blocked_pairing"
        assert v.seq == term_seq
        assert "no successor" in v.message
        assert v.related_seqs == ()

    def test_terminate_followed_by_wrong_tag_fails(self, fresh_rowset):
        rs = fresh_rowset()
        term_seq = _fold_term(rs)
        unrelated_seq = _fold_unrelated(rs)
        violations = lint_store(rs.store)
        assert len(violations) == 1
        v = violations[0]
        assert v.rule == "goal_blocked_pairing"
        assert v.seq == term_seq
        assert "expected goal_blocked" in v.message
        assert v.related_seqs == (unrelated_seq,)

    def test_escalate_with_no_successor_fails(self, fresh_rowset):
        rs = fresh_rowset()
        esc_seq = _fold_esc(rs)
        violations = lint_store(rs.store)
        assert len(violations) == 1
        assert violations[0].seq == esc_seq
        assert "no successor" in violations[0].message


# ── goal_blocked_pairing: malformed pair ─────────────────────────────


class TestMalformedPair:

    def test_wrong_source_claim_seq(self, fresh_rowset):
        rs = fresh_rowset()
        term_seq = _fold_term(rs)
        # Fold a goal_blocked at the right position but pointing at a
        # nonsense seq.
        gb_seq = _fold_gb(
            rs,
            source_claim_seq=99999,           # wrong
            source_tactic=GB_TACTIC_TERMINATE,
        )
        violations = lint_store(rs.store)
        assert len(violations) == 1
        v = violations[0]
        assert v.rule == "goal_blocked_pairing"
        assert v.seq == term_seq
        assert v.related_seqs == (gb_seq,)
        assert "source_claim_seq" in v.message

    def test_wrong_source_tactic(self, fresh_rowset):
        rs = fresh_rowset()
        term_seq = _fold_term(rs)
        gb_seq = _fold_gb(
            rs,
            source_claim_seq=term_seq,
            source_tactic=GB_TACTIC_ESCALATE_HUMAN,   # wrong for terminate
        )
        violations = lint_store(rs.store)
        assert len(violations) == 1
        v = violations[0]
        assert v.rule == "goal_blocked_pairing"
        assert v.seq == term_seq
        assert v.related_seqs == (gb_seq,)
        assert "source_tactic" in v.message

    def test_both_fields_wrong_yields_two_violations(self, fresh_rowset):
        """Conjunct 2 and 3 are independent — both should fire."""
        rs = fresh_rowset()
        term_seq = _fold_term(rs)
        gb_seq = _fold_gb(
            rs,
            source_claim_seq=99999,                  # wrong
            source_tactic=GB_TACTIC_ESCALATE_HUMAN,  # wrong
        )
        violations = lint_store(rs.store)
        assert len(violations) == 2
        # Both anchored at the same trigger seq, both pointing at the
        # same offending pair.
        for v in violations:
            assert v.seq == term_seq
            assert v.related_seqs == (gb_seq,)
        msgs = " | ".join(v.message for v in violations)
        assert "source_claim_seq" in msgs
        assert "source_tactic"    in msgs


# ── multiple triggers ───────────────────────────────────────────────


class TestMultipleTriggers:

    def test_each_trigger_checked_independently(self, fresh_rowset):
        rs = fresh_rowset()
        # First pair: well-formed.
        t1 = _fold_term(rs, reason="r1")
        _fold_gb(rs, source_claim_seq=t1, source_tactic=GB_TACTIC_TERMINATE)
        # Second pair: missing.
        t2 = _fold_term(rs, reason="r2")
        # (no successor)

        violations = lint_store(rs.store)
        assert len(violations) == 1
        assert violations[0].seq == t2

    def test_violations_sorted_by_rule_then_seq(self, fresh_rowset):
        rs = fresh_rowset()
        t1 = _fold_term(rs, reason="first")
        # No pair for t1.
        t2 = _fold_term(rs, reason="second")
        # No pair for t2 either.

        # NOTE: each unpaired terminate's "successor" is the next
        # claim, which for t1 is t2 (wrong tag), and for t2 is
        # nothing (no successor). Both fire.
        violations = lint_store(rs.store)
        assert len(violations) == 2
        # Sorted by seq ascending within rule.
        assert violations[0].seq == t1
        assert violations[1].seq == t2


# ── single-rule entry point ─────────────────────────────────────────


class TestSingleRuleAPI:
    """lint_goal_blocked_pairing is re-exported so users can run it
    in isolation. Sanity-check it agrees with lint_store on simple
    cases."""

    def test_isolation_matches_aggregate(self, fresh_rowset):
        rs = fresh_rowset()
        _fold_term(rs)  # unpaired

        from_aggregate = lint_store(rs.store)
        from_isolated  = sorted(
            lint_goal_blocked_pairing(rs.store),
            key=lambda v: (v.rule, v.seq),
        )
        assert from_aggregate == from_isolated


# ── LintViolation shape ─────────────────────────────────────────────


class TestLintViolationShape:

    def test_default_related_seqs_empty(self):
        v = LintViolation(rule="r", message="m", seq=0)
        assert v.related_seqs == ()

    def test_violations_are_frozen(self):
        v = LintViolation(rule="r", message="m", seq=0)
        with pytest.raises(Exception):
            v.seq = 1  # type: ignore[misc]
