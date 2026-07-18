"""Validation and fail-closed behaviour for guard building blocks."""
from __future__ import annotations

import asyncio

import pytest

from lipas.adapter import Request
from lipas.effect import LLMTarget
from lipas.guard import GuardVerdict, HumanApprovalGuard


def _target() -> LLMTarget:
    return LLMTarget(Request("fake", (), 1))


def test_guard_verdict_rejects_inconsistent_raw_construction():
    with pytest.raises(TypeError, match="allowed"):
        GuardVerdict(allowed=1)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="requires"):
        GuardVerdict(allowed=False)
    with pytest.raises(ValueError, match="must not"):
        GuardVerdict(allowed=True, reason="contradiction")


@pytest.mark.parametrize("timeout", [True, 0, -1, float("nan"), float("inf")])
def test_human_approval_guard_rejects_invalid_timeout(timeout):
    with pytest.raises(ValueError, match="finite positive"):
        HumanApprovalGuard(timeout_s=timeout)


def test_human_approval_guard_without_resolver_denies_closed():
    verdict = asyncio.run(HumanApprovalGuard().check(_target(), None))
    assert verdict == GuardVerdict.deny(
        "no_resolver",
        hint="HumanApprovalGuard requires resolver=...",
    )


def test_human_approval_guard_validates_resolver_result():
    async def invalid(_target, _estimate):
        return "allow"

    guard = HumanApprovalGuard(resolver=invalid)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="expected GuardVerdict"):
        asyncio.run(guard.check(_target(), None))


def test_human_approval_guard_times_out_as_typed_denial():
    async def slow(_target, _estimate):
        await asyncio.sleep(1)
        return GuardVerdict.allow()

    guard = HumanApprovalGuard(resolver=slow, timeout_s=0.001)
    verdict = asyncio.run(guard.check(_target(), None))
    assert verdict.allowed is False
    assert verdict.reason == "approval_timeout"
