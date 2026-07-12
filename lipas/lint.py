"""
LIPAS · Lint — structural correctness checks over a ClaimStore.

Lints are deterministic, pure read-only scans of the claim log that
detect violations of cross-claim invariants which the Row-level
``check_invariants`` machinery cannot express (Row invariants are
per-claim by signature).

The v0.1 lint surface is intentionally tiny: one rule, one entry
point. Add new rules as needs surface; the closed-set, registered-
at-import philosophy from strategies applies here too — rules are not
user-pluggable in v0.1.

v0.1 rules
==========

goal_blocked_pairing
--------------------
Every ``supervisor_terminate`` / ``supervisor_escalate`` claim MUST
be followed at ``seq + 1`` by a ``goal_blocked`` claim with:

    fields[F_GB_SOURCE_CLAIM_SEQ] == source.seq
    fields[F_GB_SOURCE_TACTIC]    == (
        "terminate"      if source.tag == TAG_SUPERVISOR_TERMINATE
        "escalate_human" if source.tag == TAG_SUPERVISOR_ESCALATE
    )

All three conditions are checked independently; a single trigger may
generate up to three distinct violations if its pair is fully
malformed. This is intentional — each violation is a different
failure mode and each should be visible to the operator.

The rule produces violations anchored at the SOURCE claim's seq (so a
log reader can grep for the trigger), with the offending pair's seq
in ``related_seqs`` when it exists.

Why this lint exists
====================
``Supervisor._emit_batch`` is the only place in v0.1 that produces
this pair atomically. The lint catches:

  - direct fold-bypass of Supervisor (someone folded a terminate
    by hand without the pair);
  - regressions in ``_emit_batch`` ordering;
  - tape corruption between record and replay;
  - mid-batch crashes between trigger fold and pair fold (until B1
    durable storage gives us atomic batches).

Public API
==========

    from lipas.lint import lint_store, LintViolation

    violations = lint_store(store)
    if violations:
        for v in violations:
            print(f"[{v.rule}] seq={v.seq}: {v.message}")

Returns a list sorted by ``(rule, seq)`` for stable output across runs.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Iterable

from lipas.store import ClaimStore
from lipas.supervisor import (
    F_GB_SOURCE_CLAIM_SEQ,
    F_GB_SOURCE_TACTIC,
    GB_TACTIC_ESCALATE_HUMAN,
    GB_TACTIC_TERMINATE,
    TAG_GOAL_BLOCKED,
    TAG_SUPERVISOR_ESCALATE,
    TAG_SUPERVISOR_TERMINATE,
)


__all__ = [
    "LintViolation",
    "lint_store",
    # Individual rules are re-exported so tests / advanced users can
    # run a single rule without re-evaluating the rest.
    "lint_goal_blocked_pairing",
]


# ── Violation type ──────────────────────────────────────────────────


@dataclass(frozen=True)
class LintViolation:
    """One violation detected by a lint rule.

    rule:
        Slug of the rule that fired. Stable across versions.
    message:
        Human-readable description. Stable enough to grep on (rule
        name + seq are enough for unique identification; the message
        body MAY be reworded across versions).
    seq:
        Primary anchor — the seq of the claim the violation is "about".
        For goal_blocked_pairing this is the seq of the offending
        terminate/escalate (not the missing/wrong goal_blocked).
    related_seqs:
        Additional seqs referenced by the violation. Used when a
        cross-claim rule needs to point at more than one claim
        (e.g. "the pair exists at seq=N+1 but its source_claim_seq
        is wrong" — anchor seq is the trigger, related_seqs is the
        offending pair).
    """
    rule: str
    message: str
    seq: int
    related_seqs: tuple[int, ...] = ()


# ── Rule: goal_blocked_pairing ─────────────────────────────────────


_TRIGGER_TAGS = (TAG_SUPERVISOR_TERMINATE, TAG_SUPERVISOR_ESCALATE)


def _expected_tactic(source_tag: str) -> str:
    if source_tag == TAG_SUPERVISOR_TERMINATE:
        return GB_TACTIC_TERMINATE
    if source_tag == TAG_SUPERVISOR_ESCALATE:
        return GB_TACTIC_ESCALATE_HUMAN
    raise AssertionError(f"unexpected trigger tag: {source_tag!r}")


def lint_goal_blocked_pairing(store: ClaimStore) -> list[LintViolation]:
    """Verify every terminate/escalate has a well-formed goal_blocked.

    Three conjuncts checked, one violation per failed conjunct:

      1. ``store.log[source.seq + 1]`` exists and has tag goal_blocked.
      2. ``goal_blocked.fields[source_claim_seq] == source.seq``.
      3. ``goal_blocked.fields[source_tactic]`` matches the trigger.

    Missing-pair (conjunct 1 fails) short-circuits conjuncts 2/3 —
    there's nothing to check the fields of.
    """
    out: list[LintViolation] = []
    log = list(store.log)
    if not log:
        return out

    # Build seq -> claim map for O(1) successor lookup. Seqs are
    # assigned by fold and dense in the in-memory store; the dict is
    # the robust path in case a future store flavour leaves gaps.
    by_seq: dict[int, object] = {c.seq: c for c in log}

    for c in log:
        if c.tag not in _TRIGGER_TAGS:
            continue

        expected_seq = c.seq + 1
        nxt = by_seq.get(expected_seq)

        # Conjunct 1: pair exists at the next seq with the right tag.
        if nxt is None:
            out.append(LintViolation(
                rule="goal_blocked_pairing",
                message=(
                    f"{c.tag} at seq={c.seq} has no successor claim; "
                    f"expected goal_blocked at seq={expected_seq}"
                ),
                seq=c.seq,
            ))
            continue
        if nxt.tag != TAG_GOAL_BLOCKED:
            out.append(LintViolation(
                rule="goal_blocked_pairing",
                message=(
                    f"{c.tag} at seq={c.seq} is followed by "
                    f"tag={nxt.tag!r} at seq={nxt.seq}; "
                    f"expected goal_blocked"
                ),
                seq=c.seq,
                related_seqs=(nxt.seq,),
            ))
            continue

        # Conjuncts 2 & 3: check the pair's fields. Checked
        # independently so a fully-broken pair surfaces both errors.
        ref = nxt.fields.get(F_GB_SOURCE_CLAIM_SEQ)
        if ref != c.seq:
            out.append(LintViolation(
                rule="goal_blocked_pairing",
                message=(
                    f"goal_blocked at seq={nxt.seq} has "
                    f"source_claim_seq={ref!r}; expected {c.seq} "
                    f"(paired to {c.tag} at seq={c.seq})"
                ),
                seq=c.seq,
                related_seqs=(nxt.seq,),
            ))

        actual_tactic = nxt.fields.get(F_GB_SOURCE_TACTIC)
        expected = _expected_tactic(c.tag)
        if actual_tactic != expected:
            out.append(LintViolation(
                rule="goal_blocked_pairing",
                message=(
                    f"goal_blocked at seq={nxt.seq} has "
                    f"source_tactic={actual_tactic!r}; expected "
                    f"{expected!r} (paired to {c.tag} at seq={c.seq})"
                ),
                seq=c.seq,
                related_seqs=(nxt.seq,),
            ))

    return out


# ── Public entry point ─────────────────────────────────────────────


# Registered rules. v0.1 has one; adding rules is a matter of
# appending here and writing the function. No user-pluggable
# registration in v0.1 — the closed set is the audit guarantee.
_RULES: tuple[Callable[[ClaimStore], list[LintViolation]], ...] = (
    lint_goal_blocked_pairing,
)


def lint_store(store: ClaimStore) -> list[LintViolation]:
    """Run all v0.1 lint rules over a ClaimStore.

    Returns violations sorted by ``(rule, seq)`` for stable output
    across runs.  An empty list means the store passes all rules.
    """
    out: list[LintViolation] = []
    for rule in _RULES:
        out.extend(rule(store))
    out.sort(key=lambda v: (v.rule, v.seq))
    return out
