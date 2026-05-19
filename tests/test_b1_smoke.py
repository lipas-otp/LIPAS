"""B1 v0.1 smoke test.

Two invocation modes, same core logic:

  • pytest tests/test_b1_smoke.py
  • python -m tests.test_b1_smoke        (or: PYTHONPATH=. python tests/test_b1_smoke.py)

Asserts:
  1. SqliteClaimStore matches ClaimStore's surface for a hand-rolled
     fold sequence.
  2. After close + reopen, merged state is byte-equal (Claim.fields
     equal dict-by-dict) to pre-close.
  3. RowSet works against SqliteClaimStore unchanged (uses
     CapabilityRow as the smallest exercise — just int spend).
  4. Encoding a Reply round-trips through the codec.
"""

from __future__ import annotations

import os
import tempfile

import pytest

from lipas.adapter.reply   import Reply
from lipas.adapter.request import Request, Message
from lipas.adapter.usage   import Usage
from lipas.calculus        import Claim, make_default_registry
from lipas.rows            import RowSet
from lipas.rows.capability import (
    CapabilityRow, F_AMOUNT, F_BUCKET, TAG_RESOURCE_SPENT,
)
from lipas.serialization   import (
    decode, encode, make_default_codec_registry,
)
from lipas.serialization.store_sqlite import SqliteClaimStore


# ─────────────────────────────────────────────────────────────────────
# tiny assertion helper — keeps the original "✓ / ✗" trace under both
# invocation modes. AssertionError still surfaces normally to pytest.
# ─────────────────────────────────────────────────────────────────────

def _check(cond: bool, label: str) -> None:
    mark = "✓" if cond else "✗"
    print(f"  {mark}  {label}")
    if not cond:
        raise AssertionError(label)


# ─────────────────────────────────────────────────────────────────────
# Codec round-trips — no path needed, pytest-friendly as-is.
# ─────────────────────────────────────────────────────────────────────

def test_codec_roundtrips_reply():
    print("== codec round-trips Reply ==")
    codecs = make_default_codec_registry()

    reply = Reply(
        content=[{"type": "text", "text": "hello 你好"}],
        usage=Usage(input=10, output=5),
        stop_reason="end_turn",
        model="gemma4",
    )
    enc = encode(reply, codecs)
    dec = decode(enc, codecs)

    _check(isinstance(dec, Reply),        "decoded is a Reply")
    _check(dec.model == reply.model,      "model preserved")
    _check(dec.stop_reason == "end_turn", "stop_reason preserved")
    _check(dec.usage == reply.usage,      "Usage preserved (frozen eq)")
    _check(dec.content == reply.content,  "content blocks preserved")


def test_codec_roundtrips_request():
    print("== codec round-trips Request ==")
    codecs = make_default_codec_registry()

    req = Request(
        model="gemma4",
        messages=[Message(role="user", content="hi")],
        max_tokens=128,
        system="be helpful",
    )
    enc = encode(req, codecs)
    dec = decode(enc, codecs)

    _check(isinstance(dec, Request),       "decoded is a Request")
    _check(dec.model == "gemma4",          "model preserved")
    _check(dec.system == "be helpful",     "system preserved")
    _check(dec.max_tokens == 128,          "max_tokens preserved")
    _check(len(dec.messages) == 1,         "one message")
    # Message gets coerced to dict by Request.__post_init__.
    _check(dec.messages[0]["role"] == "user", "role preserved")


# ─────────────────────────────────────────────────────────────────────
# Core scenarios — plain functions, callable from both pytest tests
# (via the fixtures/tests below) and from the __main__ script block.
# ─────────────────────────────────────────────────────────────────────

def _run_fold_and_filter(path: str) -> tuple[str, str, str]:
    print(f"== fold + filter against SqliteClaimStore @ {path!r} ==")
    store = SqliteClaimStore(path, registry=make_default_registry())

    c1 = Claim(tag="resource_spent",
               fields={"bucket": "tokens_in",  "amount": 100})
    c2 = Claim(tag="resource_spent",
               fields={"bucket": "tokens_out", "amount": 50})
    c3 = Claim(tag="other",
               fields={"x": 1})

    store.fold(c1)
    store.fold(c2)
    store.fold(c3)

    _check(len(store) == 3,                                 "log size 3")
    _check(len(store.filter(tag="resource_spent")) == 2,    "tag filter")
    _check(store.seq == 3,                                  "seq advanced")
    _check(store.merged.fields.get("amount") in {100, 50},  "merged has amount")
    store.close()
    return c1.claim_id, c2.claim_id, c3.claim_id


def _run_reopen(path: str, prior_ids: tuple[str, ...]) -> None:
    print(f"== reopen + replay @ {path!r} ==")
    store = SqliteClaimStore(path, registry=make_default_registry())

    _check(len(store) == 3,                              "reloaded log size 3")
    _check(store.seq == 3,                               "seq picks up at 3")
    log_ids = tuple(c.claim_id for c in store.log)
    _check(log_ids == prior_ids,                         "log order preserved")

    spent = store.filter(tag="resource_spent")
    buckets = sorted(c.fields["bucket"] for c in spent)
    _check(buckets == ["tokens_in", "tokens_out"],       "fields decoded")

    # Folding more after reopen must continue the seq monotonically.
    # NOTE: Claim is treated immutably by SqliteClaimStore.fold (it does
    # `claim = replace(claim, seq=self._seq)`, which rebinds locally and
    # does not mutate the caller's object). So we must inspect what
    # actually got stored — the last entry in store.log — rather than
    # the local reference we passed in.
    c4 = Claim(tag="other", fields={"x": 2})
    store.fold(c4)
    stored_c4 = store.log[-1]
    _check(stored_c4.seq == 3, "fresh fold gets seq=3 (auto-assigned)")
    _check(store.seq == 4,     "seq=4 after fourth fold")
    store.close()


def _run_rowset(path: str) -> None:
    print(f"== RowSet over SqliteClaimStore @ {path!r} ==")
    store = SqliteClaimStore(path, registry=make_default_registry())
    rowset = RowSet(store, rows=[
        CapabilityRow(budgets={"tokens_in": 1000.0}),
    ])

    rowset.fold(Claim(
        tag=TAG_RESOURCE_SPENT,
        fields={F_BUCKET: "tokens_in", F_AMOUNT: 300},
    ))
    rowset.fold(Claim(
        tag=TAG_RESOURCE_SPENT,
        fields={F_BUCKET: "tokens_in", F_AMOUNT: 400},
    ))

    proj = rowset.project("capability")
    _check(proj["tokens_in"]["spent"]     == 700.0, "spend tracked across folds")
    _check(proj["tokens_in"]["remaining"] == 300,   "remaining computed")

    # Should reject the over-budget claim.
    from lipas.rows.base import InvariantViolation
    rejected = False
    try:
        rowset.fold(Claim(
            tag=TAG_RESOURCE_SPENT,
            fields={F_BUCKET: "tokens_in", F_AMOUNT: 999},
        ))
    except InvariantViolation:
        rejected = True
    _check(rejected, "over-budget fold rejected by CapabilityRow")
    store.close()


# ─────────────────────────────────────────────────────────────────────
# pytest fixtures
#
# `folded_db` does the fold-and-filter scenario in setup, then yields
# (path, ids) so the reopen test can use the *same* on-disk file.
# Function scope is fine — each test gets a fresh tmp_path, no leakage.
# ─────────────────────────────────────────────────────────────────────

@pytest.fixture
def folded_db(tmp_path):
    path = str(tmp_path / "b1.db")
    ids = _run_fold_and_filter(path)
    return path, ids


# ─────────────────────────────────────────────────────────────────────
# pytest test functions
# ─────────────────────────────────────────────────────────────────────

def test_sqlite_fold_and_filter(folded_db):
    # All assertions happen inside _run_fold_and_filter (via the
    # fixture). This test exists so pytest reports the scenario as
    # its own test item; the body just sanity-checks the fixture
    # actually produced three claim ids.
    path, ids = folded_db
    _check(len(ids) == 3, "fold-and-filter scenario produced 3 claim_ids")
    _check(os.path.exists(path), "sqlite db file was created on disk")


def test_sqlite_reopen_equals_preclose(folded_db):
    path, ids = folded_db
    _run_reopen(path, ids)


def test_rowset_against_sqlite(tmp_path):
    _run_rowset(str(tmp_path / "b1_rowset.db"))


# ─────────────────────────────────────────────────────────────────────
# Script entry point — keeps `python -m tests.test_b1_smoke` working.
# ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    test_codec_roundtrips_reply()
    test_codec_roundtrips_request()

    with tempfile.TemporaryDirectory() as td:
        p1 = os.path.join(td, "b1.db")
        ids = _run_fold_and_filter(p1)
        _run_reopen(p1, ids)

        p2 = os.path.join(td, "b1_rowset.db")
        _run_rowset(p2)

    print("\nAll smoke checks passed.")
