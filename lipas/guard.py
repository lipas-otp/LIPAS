"""
LIPAS · Guard protocol (P2.8 → P3.0).

A Guard is a pre-flight policy gate: it inspects an upcoming effect
(LLM call OR tool call) and returns a verdict — allow or deny with a
reason.  Symmetric with the budget gate (P2.6).

P3.0 generalization
-------------------
``Guard.check(target, estimate)`` now receives an ``EffectTarget``
union (``LLMTarget`` for LLM calls, ``ToolTarget`` for tool calls)
rather than a bare ``Request``.  Guards that only care about LLM
calls pattern-match on ``LLMTarget`` and ignore tool calls; same
for tool-only guards.  Cross-cutting guards (cost ceilings, rate
limits) read fields common to both via ``isinstance``.

Guards do NOT bill, do NOT touch the store, and MUST be independent
of each other (they may run in any order, though the harness runs
them in registration order for deterministic logs).  The first Deny
wins; subsequent guards are not consulted.

Common guard kinds (provided as building blocks):

  - CallableGuard       : wrap a sync/async function.
  - HumanApprovalGuard  : block on an external resolver (queue, web
                          hook, CLI) that returns Allow/Deny.
                          Stub provided; deployments wire in their
                          own resolver.

Rejection is recorded as ``effect_rejected`` with reason starting in
``"guard:"`` (e.g. ``"guard:human_approval"``), distinguishing guard
denials from budget denials.  The caller-visible Reply (or, for
tools, the synthesized error result) uses the same
``preflight_rejection`` shape as budget rejections — downstream
handlers don't need to special-case.
"""
from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Protocol, Union, runtime_checkable

from .adapter import ResourceEstimate
from .effect import EffectTarget, LLMTarget, ToolTarget


__all__ = [
    "Guard",
    "GuardVerdict",
    "CallableGuard",
    "HumanApprovalGuard",
    # Re-exports for guard implementations:
    "EffectTarget",
    "LLMTarget",
    "ToolTarget",
]


# =====================================================================
# Verdict
# =====================================================================

@dataclass(frozen=True)
class GuardVerdict:
    """A guard's decision.

    ``allowed=True`` means "let the call through"; the harness moves
    on to the next guard / live path.  ``allowed=False`` means "deny";
    the harness records effect_rejected and returns a synthesized
    Reply / error result.

    ``reason`` is a short slug used as the ``effect_rejected.reason``
    field's prefix (``"guard:<reason>"``).  ``detail`` is free-form
    JSON-friendly metadata folded into ``effect_rejected.detail``.
    """
    allowed: bool
    reason: str = ""
    detail: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def allow(cls) -> "GuardVerdict":
        return cls(allowed=True)

    @classmethod
    def deny(cls, reason: str, **detail: Any) -> "GuardVerdict":
        if not reason:
            raise ValueError("GuardVerdict.deny requires a non-empty reason")
        return cls(allowed=False, reason=reason, detail=dict(detail))


# =====================================================================
# Guard Protocol
# =====================================================================

@runtime_checkable
class Guard(Protocol):
    """Pre-flight policy gate.

    Implementations MUST:
      - be safe to call concurrently across DIFFERENT targets;
      - return a verdict in bounded time (the harness does not impose
        a timeout — wrap with ``asyncio.wait_for`` at the deployment
        edge if you need one);
      - tolerate either ``LLMTarget`` or ``ToolTarget`` — guards that
        only care about one kind should return ``GuardVerdict.allow()``
        for the other (this is the "first-deny-wins composition is
        order-independent" property).

    Implementations MAY:
      - inspect the estimate (e.g. cost-aware guards);
      - block on external systems (human-in-the-loop, remote policy);
      - cache verdicts internally — but EffectTarget is not hashable
        in general (LLMTarget contains tuples of dicts); cache keys
        must be derived explicitly (model, message fingerprint, tool names).

    Pattern for kind-specific guards
    --------------------------------
        from lipas.guard import LLMTarget, ToolTarget, GuardVerdict

        class MaxModelGuard:
            name = "max_model"
            allowed_models = {"haiku", "sonnet"}
            async def check(self, target, estimate):
                if not isinstance(target, LLMTarget):
                    return GuardVerdict.allow()  # not our concern
                if target.request.model not in self.allowed_models:
                    return GuardVerdict.deny(
                        "model_not_allowed",
                        model=target.request.model,
                    )
                return GuardVerdict.allow()
    """

    name: str

    async def check(
        self,
        target: EffectTarget,
        estimate: ResourceEstimate | None,
    ) -> GuardVerdict: ...


# =====================================================================
# CallableGuard — wrap a sync/async function
# =====================================================================

GuardFn = Callable[
    [EffectTarget, "ResourceEstimate | None"],
    Union[GuardVerdict, Awaitable[GuardVerdict]],
]


@dataclass
class CallableGuard:
    """Adapt a plain function into a Guard.

    The function may be sync or async; ``check`` awaits async returns
    and runs sync callables inline (no implicit ``asyncio.to_thread``).
    Callers writing genuinely blocking sync code should wrap with
    ``asyncio.to_thread`` themselves or write an async function in
    the first place.
    """
    name: str
    fn: GuardFn

    async def check(
        self,
        target: EffectTarget,
        estimate: ResourceEstimate | None,
    ) -> GuardVerdict:
        out = self.fn(target, estimate)
        if inspect.isawaitable(out):
            out = await out
        if not isinstance(out, GuardVerdict):
            raise TypeError(
                f"CallableGuard {self.name!r} returned {type(out).__name__}, "
                f"expected GuardVerdict"
            )
        return out


# =====================================================================
# HumanApprovalGuard — pluggable resolver stub
# =====================================================================

ApprovalResolver = Callable[
    [EffectTarget, "ResourceEstimate | None"], Awaitable[GuardVerdict]
]


@dataclass
class HumanApprovalGuard:
    """Block on an external approval resolver.

    The resolver is the deployment-side integration: it might post to
    a Slack channel and await a click, queue to a CLI prompt, or
    consult a web app.  The Guard layer does not care; it only
    requires the resolver to return a GuardVerdict.

    Default ``timeout_s`` is None (block indefinitely).  Callers
    wanting bounded blocking should set a timeout; on expiry the
    guard denies with ``reason="approval_timeout"``.

    Default ``resolver=None`` is a deliberate fail-closed posture:
    a misconfigured deployment denies every effect rather than
    silently allowing them.  Production deployments MUST inject a
    resolver; the noisy default surfaces the misconfiguration on
    the first call.

    Resolvers MUST treat cancellation as request-withdrawn:
    cancel any external UI prompts (Slack message, CLI input,
    queued approval row) so that a late human "approve" click
    cannot fire after the harness has already returned a denial.
    """

    name: str = "human_approval"
    resolver: ApprovalResolver | None = None
    timeout_s: float | None = None

    async def check(
        self,
        target: EffectTarget,
        estimate: ResourceEstimate | None,
    ) -> GuardVerdict:
        if self.resolver is None:
            return GuardVerdict.deny(
                "no_resolver",
                hint="HumanApprovalGuard requires resolver=...",
            )
        coro = self.resolver(target, estimate)
        try:
            if self.timeout_s is None:
                return await coro
            return await asyncio.wait_for(coro, timeout=self.timeout_s)
        except asyncio.TimeoutError:
            return GuardVerdict.deny(
                "approval_timeout",
                timeout_s=self.timeout_s,
            )
