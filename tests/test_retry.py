"""P2.3 retry executor tests.

Layout (mirrors AC):
    TestSuccess        — A. first-call success, no sleep
    TestTransient      — B. retry until success
    TestExhaustion     — C. retries exhausted -> last error returned
    TestPermanent      — D. permanent kinds bail immediately
    TestMixedKinds     — E. different kinds across attempts
    TestUnknown        — F. UNKNOWN follows DEFAULT_POLICY
    TestBackoff        — G. seeded rng -> deterministic delays
    TestPolicyOverride — H. caller-supplied policy_table is honored

All tests inject sleep + rng so wall-clock time is zero and jitter
is reproducible. `complete` is monkeypatched to script Reply
sequences — we are testing retry orchestration, not the adapter.
"""
from __future__ import annotations

import asyncio
import random
from collections.abc import Sequence

import pytest

from lipas import retry as retry_mod
from lipas.adapter import Reply, Usage
from lipas.adapter.errors import DEFAULT_POLICY, ErrorKind, RetryPolicy
from lipas.retry import call_with_retry


# -- helpers ----------------------------------------------------------

def ok_reply() -> Reply:
    return Reply(
        content=[], usage=Usage(),
        stop_reason="end_turn",
        model="m", error_detail=None,
    )


def err_reply(detail: dict) -> Reply:
    return Reply(
        content=[], usage=Usage(),
        stop_reason="error",
        model="m", error_detail=detail,
    )


def rate_limit_reply() -> Reply:
    return err_reply({"type": "http_error", "status_code": 429, "body": {}})


def server_error_reply() -> Reply:
    return err_reply({"type": "http_error", "status_code": 503, "body": {}})


def auth_reply() -> Reply:
    return err_reply({"type": "http_error", "status_code": 401, "body": {}})


def unknown_reply() -> Reply:
    return err_reply({"type": "novel_garbage_we_have_never_seen"})


class SleepRecorder:
    """Records every delay; never actually sleeps. Tests run instantly."""
    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.calls.append(delay)


def script_complete(replies: Sequence[Reply]):
    """Async fake of `complete()` that yields scripted Replies in order.
    Over-call (more than scripted) raises AssertionError so retry
    over-runs are caught loudly."""
    iterator = iter(replies)

    async def fake_complete(adapter, request):
        fake_complete.calls += 1
        try:
            return next(iterator)
        except StopIteration:
            raise AssertionError(
                f"complete() called more than {len(replies)} times "
                f"— retry executor over-ran the script"
            )

    fake_complete.calls = 0
    return fake_complete


@pytest.fixture
def fake_adapter():
    # `complete` is monkeypatched, so adapter is never introspected.
    return object()


@pytest.fixture
def fake_request():
    return object()


def run(coro):
    return asyncio.run(coro)


# =====================================================================
# A. Success path
# =====================================================================

class TestSuccess:
    def test_first_call_success_no_sleep(
        self, monkeypatch, fake_adapter, fake_request
    ):
        fake = script_complete([ok_reply()])
        monkeypatch.setattr(retry_mod, "complete", fake)
        sleeper = SleepRecorder()

        reply = run(call_with_retry(
            fake_adapter, fake_request, sleep=sleeper,
        ))

        assert reply.stop_reason == "end_turn"
        assert fake.calls == 1
        assert sleeper.calls == []


# =====================================================================
# B. Transient -> success
# =====================================================================

class TestTransient:
    def test_rate_limit_then_success(
        self, monkeypatch, fake_adapter, fake_request
    ):
        fake = script_complete([
            rate_limit_reply(), rate_limit_reply(), ok_reply(),
        ])
        monkeypatch.setattr(retry_mod, "complete", fake)
        sleeper = SleepRecorder()

        reply = run(call_with_retry(
            fake_adapter, fake_request,
            sleep=sleeper, rng=random.Random(0),
        ))

        assert reply.stop_reason == "end_turn"
        assert fake.calls == 3
        assert len(sleeper.calls) == 2
        base = DEFAULT_POLICY[ErrorKind.RATE_LIMIT].base_delay_s
        assert 0 <= sleeper.calls[0] <= base * 1
        assert 0 <= sleeper.calls[1] <= base * 2


# =====================================================================
# C. Exhaustion
# =====================================================================

class TestExhaustion:
    def test_returns_last_error_when_attempts_exhausted(
        self, monkeypatch, fake_adapter, fake_request
    ):
        max_attempts = DEFAULT_POLICY[ErrorKind.RATE_LIMIT].max_attempts
        fake = script_complete([rate_limit_reply()] * max_attempts)
        monkeypatch.setattr(retry_mod, "complete", fake)
        sleeper = SleepRecorder()

        reply = run(call_with_retry(
            fake_adapter, fake_request,
            sleep=sleeper, rng=random.Random(0),
        ))

        # Returns Reply, does NOT raise.
        assert reply.stop_reason == "error"
        assert fake.calls == max_attempts
        # No sleep AFTER final failure — sleeps are between attempts.
        assert len(sleeper.calls) == max_attempts - 1


# =====================================================================
# D. Permanent kinds bail immediately
# =====================================================================

PERMANENT_CASES = [
    ("AUTH", lambda: auth_reply()),
    ("INVALID_REQUEST", lambda: err_reply({
        "type": "http_error", "status_code": 400,
        "body": {"error": {"type": "invalid_request_error",
                           "message": "bad shape"}},
    })),
    ("CONTEXT_LENGTH", lambda: err_reply({
        "type": "http_error", "status_code": 400,
        "body": {"error": {"type": "invalid_request_error",
                           "message": "prompt is too long: 250000 > 200000"}},
    })),
    ("CONTENT_FILTER", lambda: err_reply({
        "type": "provider_error",
        "provider_error": {"type": "content_policy_violation"},
    })),
]


class TestPermanent:
    @pytest.mark.parametrize("name,reply_fn", PERMANENT_CASES,
                             ids=[c[0] for c in PERMANENT_CASES])
    def test_permanent_kind_no_retry_no_sleep(
        self, name, reply_fn, monkeypatch, fake_adapter, fake_request,
    ):
        # Script length 1: any retry attempt would exhaust the script
        # and AssertionError. That's the test.
        fake = script_complete([reply_fn()])
        monkeypatch.setattr(retry_mod, "complete", fake)
        sleeper = SleepRecorder()

        reply = run(call_with_retry(
            fake_adapter, fake_request, sleep=sleeper,
        ))

        assert reply.stop_reason == "error", name
        assert fake.calls == 1, f"{name}: must not retry"
        assert sleeper.calls == [], f"{name}: must not sleep"


# =====================================================================
# E. Mixed kinds across attempts
# =====================================================================

class TestMixedKinds:
    def test_retryable_then_permanent_stops(
        self, monkeypatch, fake_adapter, fake_request
    ):
        fake = script_complete([
            rate_limit_reply(),
            auth_reply(),
        ])
        monkeypatch.setattr(retry_mod, "complete", fake)
        sleeper = SleepRecorder()

        reply = run(call_with_retry(
            fake_adapter, fake_request,
            sleep=sleeper, rng=random.Random(0),
        ))

        # Returns the AUTH reply (last seen); no further retry.
        assert reply.stop_reason == "error"
        assert reply.error_detail["status_code"] == 401
        assert fake.calls == 2
        # Exactly one sleep, between attempt 0 (RL) and attempt 1.
        assert len(sleeper.calls) == 1
        rl_base = DEFAULT_POLICY[ErrorKind.RATE_LIMIT].base_delay_s
        assert 0 <= sleeper.calls[0] <= rl_base * 1

    def test_two_retryable_kinds_use_each_own_base(
        self, monkeypatch, fake_adapter, fake_request
    ):
        fake = script_complete([
            rate_limit_reply(),    # attempt 0 -> sleep uses RL base * 2^0
            server_error_reply(),  # attempt 1 -> sleep uses SE base * 2^1
            ok_reply(),
        ])
        monkeypatch.setattr(retry_mod, "complete", fake)
        sleeper = SleepRecorder()

        reply = run(call_with_retry(
            fake_adapter, fake_request,
            sleep=sleeper, rng=random.Random(0),
        ))

        assert reply.stop_reason == "end_turn"
        assert len(sleeper.calls) == 2
        rl_base = DEFAULT_POLICY[ErrorKind.RATE_LIMIT].base_delay_s
        se_base = DEFAULT_POLICY[ErrorKind.SERVER_ERROR].base_delay_s
        assert 0 <= sleeper.calls[0] <= rl_base * (2 ** 0)
        assert 0 <= sleeper.calls[1] <= se_base * (2 ** 1)


# =====================================================================
# F. UNKNOWN follows policy table
# =====================================================================

class TestUnknown:
    def test_unknown_obeys_policy_table_whichever_way(
        self, monkeypatch, fake_adapter, fake_request
    ):
        # Don't pin whether UNKNOWN retries — that's P2.2's call.
        # Just assert retry executor matches whatever policy says.
        policy = DEFAULT_POLICY[ErrorKind.UNKNOWN]
        scripted = [unknown_reply()] * policy.max_attempts
        fake = script_complete(scripted)
        monkeypatch.setattr(retry_mod, "complete", fake)
        sleeper = SleepRecorder()

        reply = run(call_with_retry(
            fake_adapter, fake_request,
            sleep=sleeper, rng=random.Random(0),
        ))

        assert reply.stop_reason == "error"
        if policy.should_retry:
            assert fake.calls == policy.max_attempts
            assert len(sleeper.calls) == policy.max_attempts - 1
        else:
            assert fake.calls == 1
            assert sleeper.calls == []


# =====================================================================
# G. Deterministic backoff with seeded rng
# =====================================================================

class TestBackoff:
    def test_seeded_rng_produces_predictable_sequence(
        self, monkeypatch, fake_adapter, fake_request
    ):
        max_attempts = DEFAULT_POLICY[ErrorKind.RATE_LIMIT].max_attempts
        if max_attempts < 2:
            pytest.skip("RATE_LIMIT policy has < 2 attempts; nothing to retry")

        fake = script_complete([rate_limit_reply()] * max_attempts)
        monkeypatch.setattr(retry_mod, "complete", fake)
        sleeper = SleepRecorder()

        # Predict using identical algorithm + identical seed.
        base = DEFAULT_POLICY[ErrorKind.RATE_LIMIT].base_delay_s
        predict_rng = random.Random(42)
        expected = [
            predict_rng.uniform(0, base * (2 ** i))
            for i in range(max_attempts - 1)
        ]

        run(call_with_retry(
            fake_adapter, fake_request,
            sleep=sleeper, rng=random.Random(42),
        ))

        assert sleeper.calls == expected


# =====================================================================
# H. policy_table override
# =====================================================================

class TestPolicyOverride:
    def test_caller_table_overrides_default(
        self, monkeypatch, fake_adapter, fake_request
    ):
        # Flip RATE_LIMIT to non-retryable in a custom table. One
        # rate-limit reply must now bail without retry.
        custom = dict(DEFAULT_POLICY)
        custom[ErrorKind.RATE_LIMIT] = RetryPolicy(
            should_retry=False, max_attempts=1, base_delay_s=0.0,
        )

        fake = script_complete([rate_limit_reply()])
        monkeypatch.setattr(retry_mod, "complete", fake)
        sleeper = SleepRecorder()

        reply = run(call_with_retry(
            fake_adapter, fake_request,
            policy_table=custom, sleep=sleeper,
        ))

        assert reply.stop_reason == "error"
        assert fake.calls == 1
        assert sleeper.calls == []
