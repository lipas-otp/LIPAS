"""
LIPAS · ToolHarness (P3.1 → P3.2).

The smallest unit that combines tool invocation with auditable,
idempotent record-keeping over the ClaimStore.  Mirrors LLMHarness
in shape; differs in that there is no retry layer (D6) and no
streaming (D8).

One call produces, depending on path:

  Replay-driven paths (P3.2, RFC-001):
    substitute  : replay_decision + effect_intent + effect_result + spend
                  (no live execution).
    refuse      : replay_decision + effect_intent + effect_rejected,
                  then raises ReplayRefused.
    fail        : replay_decision only, then raises ReplayMissing.
    re-execute  : replay_decision + normal pipeline below.

  Pre-flight rejection paths:
    1. effect_intent (kind=tool_call)
    2. effect_rejected (kind=tool_call,
                        reason ∈ {"unknown_tool",
                                  "schema_violation",
                                  "guard:<slug>",
                                  "budget_exhausted"})

  Normal path:
    1. effect_intent (kind=tool_call)
    2. tool.acall(arguments)   — sync/async handler
    3. effect_result (kind=tool_call,
                      F_OUTPUT always present,
                      F_STATUS ∈ {"ok", "error"},
                      F_ERROR iff status=error)
    4. resource_spent ×N       routed to TAG_BUDGET_OVERRUN if folding
                               a normal spend would breach budget.
                               Recorded on success, AND on errors
                               (the wall clock and one tool_call
                               were spent regardless of outcome).

Pre-flight order (top of call):
    resolve tool → [replay decision] → schema → guards → budget
                 → record_intent → execute

Why no retry?
  Tools may carry side effects.  EXTERNAL_WRITE retried blindly
  double-charges credit cards / posts duplicate tweets.  Retry
  policy is a behaviour-layer (ReAct, etc.) decision: feed the
  is_error tool_result back to the LLM and let it choose.

Spend computation:
  - tool_calls   : always 1.0 (system-managed, overrides estimate_fn)
  - wall_seconds : measured monotonic delta (system-managed)
  - other buckets: from tool.estimate_fn(arguments), per D2 contract
    (actual ≤ estimate; the harness records the estimate value)

  For substitute, wall_seconds is folded as 0.0 (no live execution)
  and tool_calls=1.0 still records (one logical call was charged to
  the conversation).

Tools that don't declare estimate_fn pay only the two system
buckets.  Tools whose estimate_fn raises pay the system buckets
plus a logged warning — degraded but not fatal.

The return value is an Anthropic-shaped tool_result dict:

    {"type": "tool_result", "tool_use_id": effect_id,
     "content": str, "is_error": bool}

so ReActAgent's _message_from_tool_results works unchanged. On
``refuse`` and ``fail`` the harness does NOT return a dict; it
raises (ReplayRefused / ReplayMissing) so the session terminates.
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Optional

from lipas.calculus import Claim
from lipas.effect import EffectKind, ToolTarget
from lipas.guard import Guard, GuardVerdict
from lipas.replay_tools import (
    ReplayDecision,
    ReplayMissing,
    ReplayRefused,
    ToolReplayer,
)
from lipas.rows import RowSet
from lipas.rows.capability import (
    CapabilityRow,
    F_AMOUNT, F_BUCKET as CAP_F_BUCKET,
    TAG_BUDGET_OVERRUN, TAG_RESOURCE_SPENT,
)
from lipas.rows.effect import (
    F_ARGUMENTS, F_ATTEMPTS, F_COMPENSATES, F_DECLARED_SIDE_EFFECT,
    F_DETAIL, F_EFFECT_ID, F_ERROR, F_KIND, F_OUTPUT, F_REASON,
    F_SIDE_EFFECT, F_STATUS, F_TOOL_NAME,
    TAG_EFFECT_INTENT, TAG_EFFECT_REJECTED, TAG_EFFECT_RESULT,
)
from lipas.tools import Tool, ToolNotFoundError, ToolRegistry


__all__ = [
    "ToolHarness",
    "UnknownToolRejection",
    "SchemaRejection",
    "ToolGuardRejection",
    "ToolBudgetRejection",
]

logger = logging.getLogger(__name__)


# =====================================================================
# Pre-flight outcomes
# =====================================================================

@dataclass(frozen=True)
class UnknownToolRejection:
    """LLM emitted a tool_use for a name not in the registry."""
    tool_name: str
    available: tuple[str, ...]

    @property
    def reason(self) -> str:
        return "unknown_tool"

    def as_detail(self) -> dict:
        return {"tool_name": self.tool_name, "available": list(self.available)}


@dataclass(frozen=True)
class SchemaRejection:
    """Arguments did not bind to the tool's signature."""
    tool_name: str
    detail: str

    @property
    def reason(self) -> str:
        return "schema_violation"

    def as_detail(self) -> dict:
        return {"tool_name": self.tool_name, "detail": self.detail}


@dataclass(frozen=True)
class ToolGuardRejection:
    """A guard denied the tool call.

    ``reason`` is namespaced as ``"guard:<slug>"`` for symmetry with
    LLMHarness's GuardRejection.
    """
    guard_name: str
    verdict: GuardVerdict

    @property
    def reason(self) -> str:
        return f"guard:{self.verdict.reason}"

    def as_detail(self) -> dict:
        return {
            "guard":  self.guard_name,
            "reason": self.verdict.reason,
            **self.verdict.detail,
        }


@dataclass(frozen=True)
class ToolBudgetRejection:
    """Budget would be exhausted by this call."""
    bucket:   str
    spent:    float
    estimate: float
    limit:    float

    @property
    def reason(self) -> str:
        return "budget_exhausted"

    def as_detail(self) -> dict:
        return {
            "bucket":   self.bucket,
            "spent":    self.spent,
            "estimate": self.estimate,
            "limit":    self.limit,
        }


_AnyRejection = (
    UnknownToolRejection | SchemaRejection
    | ToolGuardRejection | ToolBudgetRejection
)


# =====================================================================
# Harness
# =====================================================================

@dataclass
class ToolHarness:
    """Orchestrates one tool call as a sequence of folded claims.

    Concurrent calls are safe iff the underlying ClaimStore serialises
    folds atomically.  Without that, two concurrent calls can each pass
    budget pre-flight against a stale projection and both record as
    resource_spent rather than budget_overrun.

    Attributes
    ----------
    tools:
        The ToolRegistry.  Used for name → Tool lookup.
    rowset:
        The RowSet that owns the ClaimStore.
    guards:
        Sequence of Guards consulted after the schema gate.  Receive
        ``ToolTarget(tool=tool, arguments=args)`` as their target.
        First Deny wins.  Cross-cutting guards (e.g. cost ceilings)
        that also handle LLMTarget are run as-is; LLM-only guards
        return GuardVerdict.allow() for ToolTarget per the Guard
        protocol.
    tool_replayer:
        Optional ToolReplayer (P3.2, RFC-001).  When set, every call()
        consults the replayer immediately after tool resolution and
        before the schema gate; the returned ReplayDecision drives one
        of substitute / re-execute / refuse / fail.  When None the
        harness behaves identically to P3.1.

        On the first call() against a configured replayer, the
        harness folds the replayer's session-init claim
        (TAG_REPLAY_DECISION, session_init=True) so the audit trail
        records the exact replay configuration.

        Tool replay is INDEPENDENT of LLMHarness's ReplayCursor; each
        replays its own kind. Both may be active simultaneously over
        a shared RowSet.
    """

    tools:  ToolRegistry
    rowset: RowSet
    guards: Sequence[Guard] = ()
    tool_replayer: Optional[ToolReplayer] = None

    # Internal: tracks whether the session-init claim has been folded
    # for the configured replayer. One harness == one session.
    _replay_session_started: bool = field(default=False, init=False, repr=False)

    # ── public API ─────────────────────────────────────────────

    async def call(
        self,
        *,
        tool_name: str,
        arguments: Mapping[str, Any],
        effect_id: str | None = None,
        compensates: str | None = None,
    ) -> dict:
        """Execute one tool call.

        Parameters
        ----------
        tool_name, arguments:
            What to invoke.
        effect_id:
            Stable id for this effect.  When the call originates from
            an LLM tool_use block, pass ``tool_use.id`` here so
            downstream tool_result blocks (which key on
            ``tool_use_id``) round-trip correctly and replay can
            match by id.  When omitted, a fresh ``tool_<hex>`` is
            generated — fine for direct invocation outside an LLM
            loop (tests, scripted runs).
        compensates:
            Optional effect_id of a prior effect this call is
            intended to compensate (e.g. a refund tool compensating
            a charge tool).  Recorded verbatim on the intent claim.

        Returns
        -------
        Anthropic-shaped tool_result dict on every non-raising path.
        Raises ReplayMissing (STRICT_TAPE absent recording) or
        ReplayRefused (LIVE_REROUTE refusing EXTERNAL_WRITE) when the
        replay layer terminates the session.
        """
        eid  = effect_id or f"tool_{uuid.uuid4().hex[:12]}"
        args = dict(arguments)

        # ── 0. Resolve tool ─────────────────────────────────
        try:
            tool = self.tools.get(tool_name)
        except ToolNotFoundError:
            rej = UnknownToolRejection(
                tool_name=tool_name,
                available=tuple(self.tools.names()),
            )
            self._fold_intent_unknown(eid, tool_name, args, compensates)
            self._fold_rejection(eid, rej)
            return self._tool_result(
                eid,
                f"Unknown tool {tool_name!r}. Available: "
                f"{', '.join(rej.available) or '<none>'}",
                is_error=True,
            )

        # ── 0a. Replay decision (P3.2 / RFC-001 §6.2) ──────
        # Inserted BEFORE schema gate so substitute paths bypass
        # schema/guards/budget entirely (the recorded run already
        # passed those gates; re-evaluating against new state is a
        # category error — same reasoning as LLM ReplayCursor).
        if self.tool_replayer is not None:
            if not self._replay_session_started:
                self.rowset.fold(self.tool_replayer.session_init_claim())
                self._replay_session_started = True

            decision = self.tool_replayer.decide(tool, args)

            # Fold the per-call decision claim regardless of the
            # operation, so audit captures every replay choice
            # (including fail/refuse).
            self.rowset.fold(self.tool_replayer.decision_claim(
                decision, target_effect_id=eid,
            ))

            if decision.operation == "fail":
                # STRICT_TAPE found nothing matching. No intent /
                # result folded — the session is being aborted, the
                # decision claim alone is enough audit trail.
                raise ReplayMissing(
                    f"STRICT_TAPE: no recorded result for tool={tool.name!r} "
                    f"args={args!r}"
                )

            if decision.operation == "substitute":
                return self._do_replay_substitute(
                    eid, tool, args, compensates, decision,
                )

            if decision.operation == "refuse":
                self._do_replay_refuse(eid, tool, args, compensates, decision)
                # _do_replay_refuse always raises; this point is unreachable.

            # decision.operation == "re-execute": fall through to the
            # normal pipeline below.

        # ── 1. Schema gate ──────────────────────────────────
        # _signature is set by Tool.__post_init__.  Bind() raises
        # TypeError on missing required / unexpected kwarg / etc.
        try:
            tool._signature.bind(**args).apply_defaults()
        except TypeError as e:
            rej = SchemaRejection(tool_name=tool.name, detail=str(e))
            self._fold_intent(eid, tool, args, compensates)
            self._fold_rejection(eid, rej)
            return self._tool_result(
                eid, f"Schema violation: {e}", is_error=True,
            )

        target = ToolTarget(tool=tool, arguments=args)

        # ── 2. Guard gate ───────────────────────────────────
        guard_rej = await self._preflight_guards(target)
        if guard_rej is not None:
            self._fold_intent(eid, tool, args, compensates)
            self._fold_rejection(eid, guard_rej)
            return self._tool_result(
                eid,
                f"Guard {guard_rej.guard_name!r} denied: "
                f"{guard_rej.verdict.reason}",
                is_error=True,
            )

        # ── 3. Budget gate ──────────────────────────────────
        estimate = self._estimate_dict(tool, args)
        bud_rej  = self._preflight_budget(estimate)
        if bud_rej is not None:
            self._fold_intent(eid, tool, args, compensates)
            self._fold_rejection(eid, bud_rej)
            return self._tool_result(
                eid,
                f"Budget exhausted for {bud_rej.bucket!r}: "
                f"{bud_rej.spent}+{bud_rej.estimate} > {bud_rej.limit}",
                is_error=True,
            )

        # ── 4. Record intent ────────────────────────────────
        self._fold_intent(eid, tool, args, compensates)

        # ── 5. Execute ──────────────────────────────────────
        t0           = time.monotonic()
        output: Any  = None
        status: str  = "ok"
        error_detail: dict | None = None
        try:
            output = await tool.acall(args)
        except BaseException as e:
            # BaseException intentional: KeyboardInterrupt / SystemExit
            # still need to fold a result + spend before propagating,
            # otherwise we get an orphan effect_intent and the wall_seconds
            # we burned vanishes.
            status = "error"
            error_detail = {
                "type":      "tool_exception",
                "exception": type(e).__name__,
                "message":   str(e),
            }
            wall_seconds = time.monotonic() - t0
            self._fold_result(eid, tool, output, status, error_detail)
            self._fold_spend(eid, self._compute_spend(tool, args, wall_seconds))
            if not isinstance(e, Exception):
                # KeyboardInterrupt etc. — fold-and-propagate.
                raise
            logger.info(
                "tool %r raised %s: %s",
                tool.name, type(e).__name__, e,
            )
            return self._tool_result(
                eid,
                f"{error_detail['exception']}: {error_detail['message']}",
                is_error=True,
            )

        wall_seconds = time.monotonic() - t0

        # ── 6. Record result ────────────────────────────────
        self._fold_result(eid, tool, output, status, error_detail)

        # ── 7. Record spend ─────────────────────────────────
        self._fold_spend(eid, self._compute_spend(tool, args, wall_seconds))

        # ── 8. Synthesize tool_result ───────────────────────
        return self._tool_result(eid, _stringify(output), is_error=False)

    # ── replay execution helpers (P3.2) ────────────────────────

    def _do_replay_substitute(
        self,
        eid: str,
        tool: Tool,
        args: Mapping[str, Any],
        compensates: str | None,
        decision: ReplayDecision,
    ) -> dict:
        """Mirror a recorded result into the target store without executing.

        Folds:
          - effect_intent  : built from the CURRENT tool's declaration
                             (so the audit reflects the current
                             SideEffectClass; class-mismatch resolution
                             is recorded separately on the decision
                             claim).
          - effect_result  : copied verbatim from the recorded result,
                             only F_EFFECT_ID overridden. F_STATUS,
                             F_OUTPUT, F_SIDE_EFFECT, F_ATTEMPTS,
                             F_ERROR all round-trip.
          - resource_spent : tool_calls=1.0, wall_seconds=0.0.
        """
        recorded_node = decision.recorded_node
        if recorded_node is None or recorded_node.result is None:
            # Should never happen — the matrix only emits substitute
            # when there is a recording with a result. Defensive.
            raise RuntimeError(
                f"replay substitute reached without a recorded result "
                f"(decision={decision!r})"
            )

        self._fold_intent(eid, tool, args, compensates)

        new_fields = dict(recorded_node.result.fields)
        new_fields[F_EFFECT_ID] = eid
        # F_KIND, F_STATUS, F_OUTPUT, F_SIDE_EFFECT, F_ATTEMPTS,
        # F_ERROR (if present) copied verbatim.
        self.rowset.fold(Claim(
            tag=TAG_EFFECT_RESULT,
            fields=new_fields,
            source="tool_harness.replay.substitute",
        ))

        # Charge one logical tool call (it appeared in the
        # conversation) with no wall time (no live execution).
        self._fold_spend(eid, {"tool_calls": 1.0, "wall_seconds": 0.0})

        output   = recorded_node.result.fields.get(F_OUTPUT)
        is_error = recorded_node.result.fields.get(F_STATUS) == "error"
        return self._tool_result(eid, _stringify(output), is_error=is_error)

    def _do_replay_refuse(
        self,
        eid: str,
        tool: Tool,
        args: Mapping[str, Any],
        compensates: str | None,
        decision: ReplayDecision,
    ) -> None:
        """Fold intent + rejection for a refused replay, then raise.

        Always raises ReplayRefused; never returns.
        """
        self._fold_intent(eid, tool, args, compensates)
        mode_value = (
            self.tool_replayer.mode.value
            if self.tool_replayer is not None else "?"
        )
        self.rowset.fold(Claim(
            tag=TAG_EFFECT_REJECTED,
            fields={
                F_EFFECT_ID: eid,
                F_KIND:      EffectKind.TOOL_CALL.value,
                F_REASON:    decision.reason,
                F_DETAIL: {
                    "declared_class":  decision.declared_class.value,
                    "effective_class": decision.effective_class.value,
                    "mode":            mode_value,
                    "tool_name":       tool.name,
                },
            },
            source="tool_harness.replay.refuse",
        ))
        raise ReplayRefused(
            f"replay refused tool={tool.name!r} (reason={decision.reason})"
        )

    # ── pre-flight: guards ─────────────────────────────────────

    async def _preflight_guards(
        self, target: ToolTarget,
    ) -> ToolGuardRejection | None:
        if not self.guards:
            return None
        for g in self.guards:
            # Tools have no ResourceEstimate analogue; cost-aware guards
            # that care about LLM spend pattern-match on LLMTarget and
            # short-circuit for ToolTarget.  Tool-aware guards read
            # target.tool / target.arguments directly.
            verdict = await g.check(target, None)
            if not isinstance(verdict, GuardVerdict):
                raise TypeError(
                    f"guard {g.name!r} returned {type(verdict).__name__}, "
                    f"expected GuardVerdict"
                )
            if not verdict.allowed:
                return ToolGuardRejection(guard_name=g.name, verdict=verdict)
        return None

    # ── pre-flight: budget ─────────────────────────────────────

    def _capability_row(self) -> CapabilityRow | None:
        for r in self.rowset.rows:
            if isinstance(r, CapabilityRow):
                return r
        return None

    def _estimate_dict(
        self, tool: Tool, args: Mapping[str, Any],
    ) -> dict[str, float]:
        """Upper-bound spend estimate for pre-flight budget checking.

        Always includes ``tool_calls=1.0``.  Other buckets come from
        ``tool.estimate_fn(args)`` if declared.  ``wall_seconds`` is
        only included if the tool's estimate_fn declared it (the
        tool's claim of the maximum wall time it will consume — must
        be enforced by the tool itself per D2).

        Lenient: a raising estimate_fn logs a warning and yields no
        extra estimate, so the call still goes through; it just
        bypasses gating for those buckets and may overrun later.
        """
        upper: dict[str, float] = {"tool_calls": 1.0}
        if tool.estimate_fn is not None:
            try:
                est = tool.estimate_fn(args)
                for k, v in est.items():
                    upper[k] = float(v)
            except Exception as e:
                logger.warning(
                    "estimate_fn for tool %r raised %s: %s "
                    "— pre-flight degraded to {tool_calls: 1.0}",
                    tool.name, type(e).__name__, e,
                )
        return upper

    def _preflight_budget(
        self, estimate: Mapping[str, float],
    ) -> ToolBudgetRejection | None:
        cap = self._capability_row()
        if cap is None or not cap.budgets:
            return None

        proj = cap.project(self.rowset.store)
        for bucket, est_amount in estimate.items():
            if bucket not in cap.budgets:
                continue
            info = proj[bucket]
            if info["spent"] + est_amount > info["limit"]:
                return ToolBudgetRejection(
                    bucket=bucket,
                    spent=info["spent"],
                    estimate=est_amount,
                    limit=info["limit"],
                )
        return None

    # ── spend ──────────────────────────────────────────────────

    def _compute_spend(
        self, tool: Tool,
        args: Mapping[str, Any],
        wall_seconds: float,
    ) -> dict[str, float]:
        """Build the {bucket: amount} dict to fold as resource_spent.

        ``tool_calls`` and ``wall_seconds`` are system-managed and
        always recorded (overriding any estimate_fn return value of
        the same key).  Other buckets come from estimate_fn — under
        the D2 contract that ``actual ≤ estimate``, the harness
        records the estimate value as the conservative spend.

        Errors (estimate_fn raising) degrade silently: the call
        already executed, partial system-bucket spend is recorded.
        """
        spend: dict[str, float] = {
            "tool_calls":   1.0,
            "wall_seconds": float(wall_seconds),
        }
        if tool.estimate_fn is not None:
            try:
                est = tool.estimate_fn(args)
                for k, v in est.items():
                    if k in ("tool_calls", "wall_seconds"):
                        continue  # system-managed, do not let tool override
                    spend[k] = float(v)
            except Exception:
                pass  # already warned in _estimate_dict; don't double-log
        return spend

    # ── fold helpers ───────────────────────────────────────────

    def _fold_intent(
        self,
        eid: str,
        tool: Tool,
        args: Mapping[str, Any],
        compensates: str | None,
    ) -> None:
        fields: dict = {
            F_EFFECT_ID:             eid,
            F_KIND:                  EffectKind.TOOL_CALL.value,
            F_TOOL_NAME:             tool.name,
            F_ARGUMENTS:             dict(args),
            F_DECLARED_SIDE_EFFECT:  tool.side_effect.value,
        }
        if compensates is not None:
            fields[F_COMPENSATES] = compensates
        self.rowset.fold(Claim(
            tag=TAG_EFFECT_INTENT,
            fields=fields,
            source="tool_harness.call",
        ))

    def _fold_intent_unknown(
        self,
        eid: str,
        tool_name: str,
        args: Mapping[str, Any],
        compensates: str | None,
    ) -> None:
        """Intent for a tool we couldn't resolve.

        We still fold an intent so the rejection has a parent and
        EffectView.chain(eid) returns a connected pair.  Use
        ``external_write`` as a conservative placeholder for the
        side-effect class — the call never executed, so the
        declaration is moot, but the stricter classification is
        the safer audit-trail signal.
        """
        fields: dict = {
            F_EFFECT_ID:             eid,
            F_KIND:                  EffectKind.TOOL_CALL.value,
            F_TOOL_NAME:             tool_name,
            F_ARGUMENTS:             dict(args),
            F_DECLARED_SIDE_EFFECT:  "external_write",
        }
        if compensates is not None:
            fields[F_COMPENSATES] = compensates
        self.rowset.fold(Claim(
            tag=TAG_EFFECT_INTENT,
            fields=fields,
            source="tool_harness.call",
        ))

    def _fold_result(
        self,
        eid: str,
        tool: Tool,
        output: Any,
        status: str,
        error_detail: dict | None,
    ) -> None:
        fields: dict = {
            F_EFFECT_ID:    eid,
            F_KIND:         EffectKind.TOOL_CALL.value,
            F_ATTEMPTS:     1,            # no retry layer (D6)
            F_STATUS:       status,
            F_OUTPUT:       output,
            F_SIDE_EFFECT:  tool.side_effect.value,
            # ^ actual side-effect class.  v0.1: equals declared.  Future
            #   work: a tool may downgrade (declared EXTERNAL_WRITE,
            #   actual no-op IDEMPOTENT_WRITE because cache-hit) and
            #   record a tighter actual class here.
        }
        if status == "error" and error_detail is not None:
            fields[F_ERROR] = error_detail
        self.rowset.fold(Claim(
            tag=TAG_EFFECT_RESULT,
            fields=fields,
            source="tool_harness.call",
        ))

    def _fold_spend(
        self,
        eid: str,
        spend: Mapping[str, float],
    ) -> None:
        if not spend:
            return
        cap  = self._capability_row()
        proj = cap.project(self.rowset.store) if cap is not None else None

        for bucket, amount in spend.items():
            if amount <= 0:
                continue
            is_overrun = (
                cap is not None
                and proj is not None
                and bucket in cap.budgets
                and proj[bucket]["spent"] + amount > proj[bucket]["limit"]
            )
            if is_overrun:
                logger.warning(
                    "tool_harness: in-band budget overrun bucket=%r "
                    "amount=%s effect_id=%s — recorded under %s. "
                    "Investigate estimate_fn accuracy on the offending tool.",
                    bucket, amount, eid, TAG_BUDGET_OVERRUN,
                )
                self.rowset.fold(Claim(
                    tag=TAG_BUDGET_OVERRUN,
                    fields={
                        CAP_F_BUCKET: bucket,
                        F_AMOUNT:     amount,
                        F_EFFECT_ID:  eid,
                    },
                    source="tool_harness.call",
                ))
            else:
                self.rowset.fold(Claim(
                    tag=TAG_RESOURCE_SPENT,
                    fields={
                        CAP_F_BUCKET: bucket,
                        F_AMOUNT:     amount,
                        F_EFFECT_ID:  eid,
                    },
                    source="tool_harness.call",
                ))

    def _fold_rejection(
        self,
        eid: str,
        rejection: _AnyRejection,
    ) -> None:
        self.rowset.fold(Claim(
            tag=TAG_EFFECT_REJECTED,
            fields={
                F_EFFECT_ID: eid,
                F_KIND:      EffectKind.TOOL_CALL.value,
                F_REASON:    rejection.reason,
                F_DETAIL:    rejection.as_detail(),
            },
            source="tool_harness.call",
        ))

    # ── tool_result synthesis ──────────────────────────────────

    @staticmethod
    def _tool_result(
        effect_id: str,
        content: str,
        *,
        is_error: bool,
    ) -> dict:
        d: dict = {
            "type":         "tool_result",
            "tool_use_id":  effect_id,
            "content":      content,
        }
        if is_error:
            d["is_error"] = True
        return d


# =====================================================================
# Helpers
# =====================================================================

def _stringify(output: Any) -> str:
    """Render a tool's output as a string for the LLM tool_result block.

    JSON for structured shapes (dict/list/etc.); fallback to str()
    when JSON refuses (custom classes, sets, etc.).  Strings pass
    through unchanged.

    This is deliberately lossy: a tool returning a dataclass instance
    gets stringified.  Tools that need rich content should return
    plain JSON-shaped data.  See decision D11.
    """
    if isinstance(output, str):
        return output
    if output is None:
        return ""
    try:
        return json.dumps(output, default=str, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(output)
