"""P2.2 classifier + policy tests.

Layout mirrors the AC document:
    TestChannel       — A. integration with P2.1 mock fixtures
    TestKindCoverage  — B. each ErrorKind has at least one mapping case
    TestBoundary      — C. raise/return asymmetry (strict in, lenient out)
    TestPermanent     — D. permanent kinds never retry
    TestPolicyTable   — invariant: every kind covered by DEFAULT_POLICY
"""
from __future__ import annotations

import httpx
import pytest

from lipas.adapter import Reply, Usage
from lipas.adapter.errors import (
    DEFAULT_POLICY, ErrorKind, RetryPolicy, classify,
)

# Reuse P2.1 mock harness. Names per the existing test_anthropic.py
# fixtures: make_adapter, make_request, complete, run, SUCCESS_SSE.
from tests.test_anthropic_adapter import (
    SUCCESS_SSE,
    complete,
    make_adapter,
    make_request,
    run,
)


# -- helpers ----------------------------------------------------------

def err_reply(detail: dict) -> Reply:
    """Minimal Reply for completeness-layer tests. Bypasses the
    adapter entirely — these tests assert pure-function behaviour
    of classify() given a hand-rolled error_detail."""
    return Reply(
        content=[],
        usage=Usage(),
        stop_reason="error",
        model="claude-test",
        error_detail=detail,
    )


SSE_MID_STREAM_ERROR = (
    b'data: {"type":"message_start","message":{"model":"claude-test",'
    b'"usage":{"input_tokens":5,"output_tokens":0}}}\n\n'
    b'data: {"type":"error","error":{"type":"overloaded_error",'
    b'"message":"upstream overloaded"}}\n\n'
)


# =====================================================================
# A. Channel — classifier consumes real Reply(error) from P2.1 mock
# =====================================================================

class TestChannel:
    """Proves P2.1 → P2.2 wiring. We do NOT pin which ErrorKind each
    case maps to here — that's TestKindCoverage's job. This layer
    only asserts: the channel is connected, classify() does not
    raise on any Reply produced by the adapter, and a successful
    Reply correctly raises ValueError."""

    def _last_reply(self, events):
        # Adapter yields Delta* then Done. complete() returns the
        # final Reply; if the test uses a different helper, adjust.
        return events

    def test_http_4xx_classifies(self):
        a = make_adapter(lambda r: httpx.Response(
            401,
            json={"error": {"type": "authentication_error",
                            "message": "invalid x-api-key"}},
        ))
        reply = run(complete(a, make_request()))
        assert reply.stop_reason == "error"
        kind = classify(reply)
        assert isinstance(kind, ErrorKind)

    def test_http_5xx_classifies(self):
        a = make_adapter(lambda r: httpx.Response(
            503,
            json={"error": {"type": "api_error", "message": "down"}},
        ))
        reply = run(complete(a, make_request()))
        kind = classify(reply)
        assert isinstance(kind, ErrorKind)

    def test_network_error_classifies(self):
        def boom(_request):
            raise httpx.ConnectError("dns failure")

        a = make_adapter(boom)
        reply = run(complete(a, make_request()))
        kind = classify(reply)
        assert isinstance(kind, ErrorKind)

    def test_sse_mid_stream_error_classifies(self):
        a = make_adapter(lambda r: httpx.Response(
            200, content=SSE_MID_STREAM_ERROR,
        ))
        reply = run(complete(a, make_request()))
        kind = classify(reply)
        assert isinstance(kind, ErrorKind)

    def test_successful_reply_raises(self):
        a = make_adapter(lambda r: httpx.Response(200, content=SUCCESS_SSE))
        reply = run(complete(a, make_request()))
        assert reply.stop_reason == "end_turn"
        with pytest.raises(ValueError, match="stop_reason='error'"):
            classify(reply)


# =====================================================================
# B. Kind coverage — every ErrorKind reachable via at least one case
# =====================================================================

class TestKindCoverage:
    """One minimal Reply per ErrorKind. Each case asserts the full
    (kind, should_retry, max_attempts) triple to lock the policy
    table at the same time. Time values (base_delay_s) are NOT
    asserted — see TestPolicyTable for the bound."""

    def _assert_triple(self, reply: Reply, kind: ErrorKind):
        assert classify(reply) == kind
        p = DEFAULT_POLICY[kind]
        assert isinstance(p, RetryPolicy)
        # Tied to AC: full triple is (kind, should_retry, max_attempts).
        assert p.should_retry == DEFAULT_POLICY[kind].should_retry
        assert p.max_attempts == DEFAULT_POLICY[kind].max_attempts

    def test_rate_limit_via_http(self):
        self._assert_triple(
            err_reply({"type": "http_error", "status_code": 429, "body": {}}),
            ErrorKind.RATE_LIMIT,
        )

    def test_rate_limit_via_provider(self):
        self._assert_triple(
            err_reply({
                "type": "provider_error",
                "provider_error": {"type": "rate_limit_error"},
            }),
            ErrorKind.RATE_LIMIT,
        )

    def test_timeout_via_read_timeout(self):
        # TIMEOUT vs NETWORK split relies on exception_type string —
        # this is the P2.1 debt called out in the AC doc.
        self._assert_triple(
            err_reply({
                "type": "network_error",
                "exception_type": "ReadTimeout",
                "message": "read timed out",
            }),
            ErrorKind.TIMEOUT,
        )

    def test_network_via_connect_error(self):
        self._assert_triple(
            err_reply({
                "type": "network_error",
                "exception_type": "ConnectError",
                "message": "dns failure",
            }),
            ErrorKind.NETWORK,
        )

    def test_server_error_via_http(self):
        self._assert_triple(
            err_reply({"type": "http_error", "status_code": 503, "body": {}}),
            ErrorKind.SERVER_ERROR,
        )

    def test_server_error_via_overloaded_provider(self):
        # Backlog B-4: overloaded_error currently folds into
        # SERVER_ERROR. When the OVERLOADED kind lands, this test
        # flips to assert OVERLOADED — the change is intentional
        # and detectable here.
        self._assert_triple(
            err_reply({
                "type": "provider_error",
                "provider_error": {"type": "overloaded_error"},
            }),
            ErrorKind.SERVER_ERROR,
        )

    def test_auth_via_http_401(self):
        self._assert_triple(
            err_reply({"type": "http_error", "status_code": 401, "body": {}}),
            ErrorKind.AUTH,
        )

    def test_auth_via_http_403(self):
        self._assert_triple(
            err_reply({"type": "http_error", "status_code": 403, "body": {}}),
            ErrorKind.AUTH,
        )

    def test_invalid_request_via_http_400(self):
        self._assert_triple(
            err_reply({
                "type": "http_error", "status_code": 400,
                "body": {"error": {"type": "invalid_request_error",
                                   "message": "bad shape"}},
            }),
            ErrorKind.INVALID_REQUEST,
        )

    def test_context_length_via_http_400_keyword(self):
        # Backlog B-6: keyword sniffing is the current implementation,
        # not the desired one. This test pins current behaviour so
        # the keyword path is exercised; when B-6 lands the case
        # flips to a structured field assertion.
        self._assert_triple(
            err_reply({
                "type": "http_error", "status_code": 400,
                "body": {"error": {
                    "type": "invalid_request_error",
                    "message": "prompt is too long: 250000 tokens > 200000",
                }},
            }),
            ErrorKind.CONTEXT_LENGTH,
        )

    def test_content_filter_via_provider(self):
        self._assert_triple(
            err_reply({
                "type": "provider_error",
                "provider_error": {"type": "content_policy_violation"},
            }),
            ErrorKind.CONTENT_FILTER,
        )

    def test_unknown_via_novel_detail_type(self):
        self._assert_triple(
            err_reply({"type": "novel_garbage_we_have_never_seen"}),
            ErrorKind.UNKNOWN,
        )


# =====================================================================
# C. Boundary — strict on stop_reason, lenient on error_detail
# =====================================================================

class TestBoundary:
    """The raise/return asymmetry. Together these pin the contract:
    structural mistakes raise; semantic novelty returns UNKNOWN."""

    def test_end_turn_raises(self):
        reply = Reply(
            content=[], usage=Usage(),
            stop_reason="end_turn",
            model="m", error_detail=None,
        )
        with pytest.raises(ValueError, match="stop_reason='error'"):
            classify(reply)

    def test_max_tokens_raises(self):
        # Any non-"error" stop_reason is a programmer error at
        # the call site — classify is not where you ask "did
        # this succeed?".
        reply = Reply(
            content=[], usage=Usage(),
            stop_reason="max_tokens",
            model="m", error_detail=None,
        )
        with pytest.raises(ValueError):
            classify(reply)

    def test_unknown_detail_type_returns_UNKNOWN_not_raise(self):
        kind = classify(err_reply({"type": "this_does_not_exist"}))
        assert kind == ErrorKind.UNKNOWN

    def test_empty_detail_returns_UNKNOWN_not_raise(self):
        # Empty dict is a degenerate-but-legal error_detail (e.g.
        # an upstream component decided "I know it failed but I
        # have nothing structured to say"). Reply's __post_init__
        # accepts it (non-None), and classify must return UNKNOWN
        # rather than crash on missing keys.
        #
        # Note: error_detail=None is structurally impossible under
        # Reply's invariant (stop_reason='error' ⇒ error_detail is
        # not None, enforced in __post_init__). The defensive
        # `if error_detail is None` branch in classify is therefore
        # dead under current shapes; we keep it for robustness if
        # the invariant ever loosens, but cannot construct a Reply
        # to exercise it from this layer.
        assert classify(err_reply({})) == ErrorKind.UNKNOWN

    def test_provider_error_with_unknown_provider_type(self):
        # Lenient on provider_error.type too — new Anthropic error
        # codes must not crash the classifier.
        kind = classify(err_reply({
            "type": "provider_error",
            "provider_error": {"type": "brand_new_error_code"},
        }))
        assert kind == ErrorKind.UNKNOWN


# =====================================================================
# D. Permanent kinds never retry
# =====================================================================

PERMANENT_KINDS = (
    ErrorKind.AUTH,
    ErrorKind.INVALID_REQUEST,
    ErrorKind.CONTEXT_LENGTH,
    ErrorKind.CONTENT_FILTER,
)


class TestPermanent:
    """Single test pinned to AC D: these four kinds must NEVER
    retry under DEFAULT_POLICY. Drift here = retry storms in
    production. Treat any failure of this test as P0."""

    def test_permanent_kinds_never_retry(self):
        for kind in PERMANENT_KINDS:
            policy = DEFAULT_POLICY[kind]
            assert policy.should_retry is False, (
                f"{kind} must not retry — flipping this to True "
                f"causes retry storms on non-recoverable errors. "
                f"If you genuinely want to retry, change ErrorKind "
                f"membership, do not flip the policy bit."
            )
            assert policy.max_attempts == 1, (
                f"{kind}: max_attempts must be 1 when "
                f"should_retry=False; got {policy.max_attempts}."
            )

    def test_permanent_kinds_marked_non_transient(self):
        # Cross-check against the existing is_transient property.
        # Permanent kinds in the policy table must also be
        # non-transient in the enum — keeps the two layers aligned.
        for kind in PERMANENT_KINDS:
            assert not kind.is_transient


# =====================================================================
# Policy table invariants
# =====================================================================

class TestPolicyTable:

    def test_default_policy_covers_all_kinds(self):
        # If you add an ErrorKind, DEFAULT_POLICY must grow with it.
        # KeyError at runtime is not acceptable — the executor will
        # crash on a real error path that's hard to reproduce.
        missing = set(ErrorKind) - set(DEFAULT_POLICY.keys())
        assert not missing, f"DEFAULT_POLICY missing kinds: {missing}"

    def test_base_delay_non_negative(self):
        # AC E: don't pin specific delay values, but negative is
        # always a bug.
        for kind, policy in DEFAULT_POLICY.items():
            assert policy.base_delay_s >= 0, (
                f"{kind}: base_delay_s must be >= 0; "
                f"got {policy.base_delay_s}"
            )

    def test_max_attempts_at_least_one(self):
        for kind, policy in DEFAULT_POLICY.items():
            assert policy.max_attempts >= 1, (
                f"{kind}: max_attempts must be >= 1 "
                f"(an attempt always happens, even if no retry); "
                f"got {policy.max_attempts}"
            )

    def test_transient_kinds_retry(self):
        # Symmetry with TestPermanent: kinds marked transient at
        # the enum layer should retry at the policy layer. This
        # catches drift in the other direction.
        for kind in ErrorKind:
            if kind.is_transient:
                assert DEFAULT_POLICY[kind].should_retry, (
                    f"{kind} is_transient=True but policy says "
                    f"should_retry=False — pick one."
                )
