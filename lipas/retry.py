"""P2.3 retry executor — single-call orchestration over LLMAdapter.

Wraps `complete()` with classify+policy-driven retries on transient
errors. Returns a `RetryOutcome` carrying the final Reply (success or
terminal error) along with the total attempt count; never raises on a
classified error path. Caller decides what to do with a terminal-error
reply.

Streaming incompatibility:
    Returns the final Reply only (wrapped in RetryOutcome). Stream
    events are consumed internally by `complete()` and never surfaced.
    Token-by-token delivery cannot coexist with retry — already-emitted
    tokens cannot be unsent. If you want streaming, use the adapter
    directly without retry.

P2.4 amendment:
    Return type changed from `Reply` to `RetryOutcome(reply, attempts)`.
    The harness layer (P2.4) records `attempts` into call_result claims
    to make retry behavior an observable, auditable signal of the
    capability layer rather than a hidden implementation detail.
    Future fields (total_delay_s, attempt_kinds, ...) can be added to
    RetryOutcome without changing callsite unpacking — that is the
    reason for the dataclass over a tuple.
"""
from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass

from lipas.adapter import Reply, Request
from lipas.adapter.errors import (
    DEFAULT_POLICY,
    ErrorKind,
    RetryPolicy,
    classify,
)
from lipas.adapter.protocol import LLMAdapter, complete

__all__ = ["call_with_retry", "RetryOutcome"]

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class RetryOutcome:
    """Result of a `call_with_retry` invocation.

    Fields:
        reply:    the final Reply — success, terminal-error, or
                  exhausted-retry, per `call_with_retry` semantics.
        attempts: total number of `complete()` invocations that
                  occurred. `attempts == 1` means the first call
                  returned (success or non-retryable error) without
                  any retry. `attempts >= 2` means at least one retry
                  was issued; the final attempt is included in the
                  count.

    Frozen dataclass rather than tuple: future fields (total_delay_s,
    attempt_kinds, last_error_kind, ...) can be added without breaking
    callsite unpacking.
    """
    reply: Reply
    attempts: int

    # Read-only convenience projections preserve the useful part of the old
    # ``Reply``-only call site without hiding the new audit-critical attempt
    # count. New code should use ``.reply`` when passing a Reply onward.
    @property
    def stop_reason(self) -> str: return self.reply.stop_reason
    @property
    def error_detail(self): return self.reply.error_detail
    @property
    def usage(self): return self.reply.usage


async def call_with_retry(
    adapter: LLMAdapter,
    request: Request,
    *,
    policy_table: Mapping[ErrorKind, RetryPolicy] = DEFAULT_POLICY,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    rng: random.Random | None = None,
) -> RetryOutcome:
    """Execute a request with classified-error retries.

    Args:
        adapter, request: forwarded to `complete()` unchanged on every
            attempt. The same request is replayed; idempotency is the
            caller's problem.
        policy_table: maps ErrorKind -> RetryPolicy. Defaults to
            DEFAULT_POLICY. Must cover every ErrorKind value (enforced
            at the policy-layer test, not here — KeyError surfaces
            loudly if a caller passes a partial table).
        sleep: injectable for tests. Defaults to asyncio.sleep.
        rng: injectable for tests. Defaults to a fresh random.Random()
            (system entropy).

    Returns:
        RetryOutcome(reply, attempts), where:
          - reply is the first Reply with stop_reason != "error", OR
            the last error Reply when retries are exhausted, OR the
            first error Reply when its kind is non-retryable;
          - attempts is the total number of complete() calls made
            (1 = no retry occurred).

    Raises:
        Whatever `complete()` raises. The P2.1 contract is that the
        adapter converts errors into Reply(stop_reason='error'). If
        complete() raises, that's a P2.1 bug — propagate, do not
        swallow.

    Note on cross-kind retries:
        `attempt` is a single global counter. If kind changes between
        attempts, the new kind's `max_attempts` is compared against
        the running global counter. Side effect: a sequence
        RL, SE, SE, ... with RL.max_attempts=3 and SE.max_attempts=5
        is bounded by SE's cap once the kind switches. This is
        intentional — it prevents oscillation between two retryable
        kinds from looping past either's cap.
    """
    if rng is None:
        rng = random.Random()

    attempt = 0  # 0-indexed: index of the attempt about to run.

    while True:
        reply = await complete(adapter, request)

        if reply.stop_reason != "error":
            return RetryOutcome(reply=reply, attempts=attempt + 1)

        kind = classify(reply)
        policy = policy_table[kind]
        attempts_made = attempt + 1

        if not policy.should_retry or attempts_made >= policy.max_attempts:
            logger.info(
                "retry: terminal kind=%s attempts=%d/%d retryable=%s",
                kind.name, attempts_made, policy.max_attempts,
                policy.should_retry,
            )
            return RetryOutcome(reply=reply, attempts=attempts_made)

        # Full jitter (AWS Architecture Blog variant):
        #   delay ~ U(0, base * 2**attempt)
        ceiling = policy.base_delay_s * (2 ** attempt)
        delay = rng.uniform(0, ceiling)

        logger.info(
            "retry: kind=%s attempt=%d/%d delay=%.3fs",
            kind.name, attempts_made, policy.max_attempts, delay,
        )

        await sleep(delay)
        attempt += 1
