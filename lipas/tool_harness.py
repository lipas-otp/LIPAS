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
                                  "estimate_invalid",
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
    resolve tool → [replay decision] → schema → guards → valid estimate
                 → budget → record_intent → execute

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

Tools that don't declare estimate_fn pay only the two system buckets. A tool
that declares an estimate_fn but cannot produce a finite non-negative estimate
is rejected before execution: a broken estimate must not silently turn a hard
budget into a post-hoc warning.

The return value is an Anthropic-shaped tool_result dict:

    {"type": "tool_result", "tool_use_id": effect_id,
     "content": str, "is_error": bool}

so ReActAgent's _message_from_tool_results works unchanged. On
``refuse`` and ``fail`` the harness does NOT return a dict; it
raises (ReplayRefused / ReplayMissing) so the session terminates.
"""
from __future__ import annotations

import asyncio
import json
import hashlib
import logging
import math
import time
import uuid
from copy import deepcopy
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Optional

from lipas.calculus import Claim
from lipas.effect import EffectKind, ToolTarget
from lipas.exceptions import OrphanedEffectError
from lipas.guard import Guard, GuardVerdict
from lipas.replay_tools import (
    F_DECISION_SOURCE_EFFECT_ID,
    ReplayDecision,
    ReplayMissing,
    ReplayRefused,
    TAG_REPLAY_DECISION,
    ToolReplayer,
)
from lipas.rows import RowSet
from lipas.rows.capability import (
    CapabilityRow,
    F_AMOUNT, F_BUCKET as CAP_F_BUCKET,
    TAG_BUDGET_OVERRUN, TAG_RESOURCE_SPENT,
)
from lipas.rows.effect import (
    EffectRow,
    F_ARGUMENTS, F_ATTEMPTS, F_CAUSED_BY, F_COMPENSATES, F_DECLARED_SIDE_EFFECT,
    F_DETAIL, F_EFFECT_ID, F_ERROR, F_KIND, F_OUTPUT, F_REASON, F_SPEND,
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
    "ToolEstimateRejection",
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


@dataclass(frozen=True)
class ToolEstimateRejection:
    """A tool declared an unusable pre-flight resource estimate."""
    tool_name: str
    detail: str

    @property
    def reason(self) -> str:
        return "estimate_invalid"

    def as_detail(self) -> dict:
        return {"tool_name": self.tool_name, "detail": self.detail}


_AnyRejection = (
    UnknownToolRejection | SchemaRejection
    | ToolGuardRejection | ToolBudgetRejection | ToolEstimateRejection
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
    argument_resolver: Any | None = None
    result_sanitizer: Any | None = None

    # Internal: tracks whether the session-init claim has been folded
    # for the configured replayer. One harness == one session.
    _replay_session_started: bool = field(default=False, init=False, repr=False)
    _consumed_replay_effect_ids: set[str] = field(
        default_factory=set, init=False, repr=False,
    )

    # ── public API ─────────────────────────────────────────────

    async def call(
        self,
        *,
        tool_name: str,
        arguments: Mapping[str, Any],
        effect_id: str | None = None,
        tool_use_id: str | None = None,
        compensates: str | None = None,
        caused_by: str | None = None,
    ) -> dict:
        """Execute one tool call.

        Parameters
        ----------
        tool_name, arguments:
            What to invoke.
        effect_id:
            Stable internal id for this audited effect. When omitted, a fresh
            ``tool_<hex>`` id is generated.
        tool_use_id:
            Provider correlation id copied to the returned ``tool_result``.
            This is deliberately separate from ``effect_id`` because provider
            ids are neither globally unique nor constrained to LIPAS's Effect
            id format. Defaults to the internal effect id for direct calls.
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
        eid = effect_id or f"tool_{uuid.uuid4().hex[:12]}"
        result_id = tool_use_id or eid
        # A tool receives its own nested copy. This prevents a mutating client
        # from changing either the caller's mapping or the recorded intent.
        args = deepcopy(dict(arguments))

        recovered = self._recover_existing(eid, result_id, tool_name, args)
        if recovered is not None:
            return recovered

        # ── 0. Resolve tool ─────────────────────────────────
        try:
            tool = self.tools.get(tool_name)
        except ToolNotFoundError:
            rej = UnknownToolRejection(
                tool_name=tool_name,
                available=tuple(self.tools.names()),
            )
            self._fold_intent_unknown(eid, tool_name, args, compensates, caused_by)
            self._fold_rejection(eid, rej)
            return self._tool_result(
                result_id,
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
            self._restore_consumed_replay_effect_ids()
            if not self._replay_session_started:
                self.rowset.fold(self.tool_replayer.session_init_claim())
                self._replay_session_started = True

            decision = self.tool_replayer.decide(
                tool,
                args,
                exclude_effect_ids=frozenset(
                    self._consumed_replay_effect_ids,
                ),
            )
            if decision.recorded_node is not None:
                self._consumed_replay_effect_ids.add(
                    decision.recorded_node.effect_id,
                )

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
                    eid, result_id, tool, args, compensates, caused_by, decision,
                )

            if decision.operation == "refuse":
                self._do_replay_refuse(eid, tool, args, compensates, caused_by, decision)
                # _do_replay_refuse always raises; this point is unreachable.

            # decision.operation == "re-execute": fall through to the
            # normal pipeline below.

        # ── 1. Schema gate ──────────────────────────────────
        # _signature is set by Tool.__post_init__.  Bind() raises
        # TypeError on missing required / unexpected kwarg / etc.
        try:
            bound = tool._signature.bind(**args)
            bound.apply_defaults()
            # The same fully bound arguments drive estimate, guards, audit,
            # and execution. In particular, an estimate_fn sees defaults just
            # as the tool body does.
            args = deepcopy(dict(bound.arguments))
        except TypeError as e:
            schema_rej = SchemaRejection(tool_name=tool.name, detail=str(e))
            self._fold_intent(eid, tool, args, compensates, caused_by)
            self._fold_rejection(eid, schema_rej)
            return self._tool_result(
                result_id, f"Schema violation: {e}", is_error=True,
            )

        # Guards are policy observers, not argument transformers. Keep their
        # input separate from the mapping eventually sent to the tool body.
        target = ToolTarget(tool=tool, arguments=deepcopy(args))

        # ── 2. Guard gate ───────────────────────────────────
        guard_rej = await self._preflight_guards(target)
        if guard_rej is not None:
            self._fold_intent(eid, tool, args, compensates, caused_by)
            self._fold_rejection(eid, guard_rej)
            return self._tool_result(
                result_id,
                f"Guard {guard_rej.guard_name!r} denied: "
                f"{guard_rej.verdict.reason}",
                is_error=True,
            )

        # ── 3. Budget gate ──────────────────────────────────
        estimate, estimate_rej = self._estimate_dict(tool, args)
        if estimate_rej is not None:
            self._fold_intent(eid, tool, args, compensates, caused_by)
            self._fold_rejection(eid, estimate_rej)
            return self._tool_result(
                result_id,
                f"Tool estimate for {tool.name!r} is invalid: {estimate_rej.detail}",
                is_error=True,
            )
        assert estimate is not None
        bud_rej  = self._preflight_budget(estimate)
        if bud_rej is not None:
            self._fold_intent(eid, tool, args, compensates, caused_by)
            self._fold_rejection(eid, bud_rej)
            return self._tool_result(
                result_id,
                f"Budget exhausted for {bud_rej.bucket!r}: "
                f"{bud_rej.spent}+{bud_rej.estimate} > {bud_rej.limit}",
                is_error=True,
            )

        # ── 4. Record intent ────────────────────────────────
        self._fold_intent(eid, tool, args, compensates, caused_by)

        # ── 5. Execute ──────────────────────────────────────
        t0           = time.monotonic()
        output: Any  = None
        status: str  = "ok"
        error_detail: dict | None = None
        try:
            execution_args = (
                self.argument_resolver(tool, deepcopy(args))
                if self.argument_resolver is not None else args
            )
            raw_output = await tool.acall(execution_args)
            output = (
                self.result_sanitizer(raw_output)
                if self.result_sanitizer is not None else raw_output
            )
        except asyncio.CancelledError:
            # Cancellation cannot prove a sync thread or remote operation
            # stopped. Keep the already-recorded intent orphaned so recovery
            # fails closed instead of persisting a false terminal error.
            raise
        except BaseException as e:
            # BaseException intentional: KeyboardInterrupt / SystemExit
            # still need to fold a result + spend before propagating,
            # otherwise we get an orphan effect_intent and the wall_seconds
            # we burned vanishes.
            status = "error"
            message: Any = str(e)
            if self.result_sanitizer is not None:
                message = self.result_sanitizer(message)
            error_detail = {
                "type":      "tool_exception",
                "exception": type(e).__name__,
                "message":   str(message),
            }
            wall_seconds = time.monotonic() - t0
            spend = self._compute_spend(estimate, wall_seconds)
            self._fold_result(
                eid, tool, output, status, error_detail, spend=spend,
            )
            self._fold_spend(eid, spend)
            if not isinstance(e, Exception):
                # KeyboardInterrupt etc. — fold-and-propagate.
                raise
            logger.info(
                "tool %r raised %s: %s",
                tool.name, type(e).__name__, e,
            )
            return self._tool_result(
                result_id,
                f"{error_detail['exception']}: {error_detail['message']}",
                is_error=True,
            )

        wall_seconds = time.monotonic() - t0

        # ── 6. Record result ────────────────────────────────
        spend = self._compute_spend(estimate, wall_seconds)
        self._fold_result(
            eid, tool, output, status, error_detail, spend=spend,
        )

        # ── 7. Record spend ─────────────────────────────────
        self._fold_spend(eid, spend)

        # ── 8. Synthesize tool_result ───────────────────────
        return self._tool_result(result_id, _stringify(output), is_error=False)

    def reconcile_orphan(
        self,
        effect_id: str,
        *,
        output: Any = None,
        error: Mapping[str, Any] | None = None,
        wall_seconds: float = 0.0,
    ) -> dict:
        """Close an intent-only tool Effect after an operator/provider check.

        A cancelled synchronous thread cannot be force-killed safely.  The
        caller may therefore inspect the real world (or the provider's
        idempotency lookup) and explicitly record the observed outcome.  This
        is deliberately separate from ``call`` so a retry can never turn an
        orphan into a second live submission.
        """
        if not isinstance(effect_id, str) or not effect_id:
            raise ValueError("effect_id must be a non-empty string")
        if (
            isinstance(wall_seconds, bool)
            or not isinstance(wall_seconds, (int, float))
            or wall_seconds < 0
        ):
            raise ValueError("wall_seconds must be a non-negative number")
        effect_row = next(
            (row for row in self.rowset.rows if isinstance(row, EffectRow)), None,
        )
        if effect_row is None:
            raise OrphanedEffectError(f"tool effect {effect_id!r} is not recorded")
        node = effect_row.project(self.rowset.store).nodes.get(effect_id)
        if node is None or node.intent is None:
            raise OrphanedEffectError(f"tool effect {effect_id!r} has no intent")
        if node.result is not None or node.rejection is not None:
            return self._tool_result(
                effect_id,
                "already reconciled",
                is_error=node.result is not None
                and node.result.fields.get(F_STATUS) != "ok",
            )
        tool_name = node.intent.fields.get(F_TOOL_NAME)
        arguments = node.intent.fields.get(F_ARGUMENTS, {})
        if not isinstance(tool_name, str) or not isinstance(arguments, Mapping):
            raise OrphanedEffectError(f"tool effect {effect_id!r} has invalid intent")
        tool = self.tools.get(tool_name)
        estimate, rejection = self._estimate_dict(tool, arguments)
        if rejection is not None or estimate is None:
            raise OrphanedEffectError(
                f"tool effect {effect_id!r} cannot be reconciled: invalid estimate",
            )
        spend = self._compute_spend(estimate, float(wall_seconds))
        status = "error" if error is not None else "ok"
        self._fold_result(effect_id, tool, output, status, dict(error) if error else None, spend=spend)
        self._fold_spend(effect_id, spend)
        return self._tool_result(
            effect_id,
            _stringify(output) if error is None else str(error.get("message", "error")),
            is_error=error is not None,
        )

    def _restore_consumed_replay_effect_ids(self) -> None:
        """Recover source-tape consumption when a target tape is reopened."""
        for claim in self.rowset.store.filter(tag=TAG_REPLAY_DECISION):
            source_id = claim.fields.get(F_DECISION_SOURCE_EFFECT_ID)
            if isinstance(source_id, str):
                self._consumed_replay_effect_ids.add(source_id)

    def _recover_existing(
        self,
        effect_id: str,
        tool_use_id: str,
        tool_name: str,
        arguments: Mapping[str, Any],
    ) -> dict | None:
        """Return a recorded terminal tool result without executing again."""
        effect_row = next(
            (row for row in self.rowset.rows if isinstance(row, EffectRow)),
            None,
        )
        if effect_row is None:
            return None
        node = effect_row.project(self.rowset.store).nodes.get(effect_id)
        if node is None:
            return None
        if node.kind is not EffectKind.TOOL_CALL:
            raise ValueError(f"effect id {effect_id!r} belongs to a non-tool effect")
        fields = node.intent.fields
        if fields.get(F_TOOL_NAME) != tool_name:
            raise ValueError(f"effect id {effect_id!r} was reused for a different tool")

        recorded_args = fields.get(F_ARGUMENTS)
        normalized_args: Mapping[str, Any] = dict(arguments)
        if recorded_args != normalized_args:
            try:
                tool = self.tools.get(tool_name)
                bound = tool._signature.bind(**dict(arguments))
                bound.apply_defaults()
                normalized_args = dict(bound.arguments)
            except (ToolNotFoundError, TypeError):
                pass
        if recorded_args != normalized_args:
            raise ValueError(
                f"effect id {effect_id!r} was reused with different arguments",
            )

        if node.result is not None:
            result_fields = node.result.fields
            recorded_spend = result_fields.get(F_SPEND, {})
            if not isinstance(recorded_spend, Mapping):
                raise TypeError(
                    f"recorded tool effect {effect_id!r} has invalid spend",
                )
            self._fold_spend(effect_id, recorded_spend)
            is_error = result_fields.get(F_STATUS) == "error"
            return self._tool_result(
                tool_use_id,
                self._recorded_result_content(result_fields),
                is_error=is_error,
            )
        if node.rejection is not None:
            rejection_fields = node.rejection.fields
            return self._tool_result(
                tool_use_id,
                self._recovered_rejection_content(rejection_fields),
                is_error=True,
            )
        raise OrphanedEffectError(
            f"tool effect {effect_id!r} has intent but no terminal outcome",
        )

    @staticmethod
    def _recorded_result_content(fields: Mapping[str, Any]) -> str:
        content = _stringify(fields.get(F_OUTPUT))
        if fields.get(F_STATUS) != "error":
            return content
        error = fields.get(F_ERROR)
        if not isinstance(error, Mapping):
            return content
        exception = error.get("exception")
        message = error.get("message")
        if exception is None or message is None:
            return content
        return f"{exception}: {message}"

    @staticmethod
    def _recovered_rejection_content(fields: Mapping[str, Any]) -> str:
        """Rebuild the exact tool_result text emitted by a rejection path."""
        reason = fields.get(F_REASON)
        raw_detail = fields.get(F_DETAIL)
        detail = raw_detail if isinstance(raw_detail, Mapping) else {}
        if reason == "unknown_tool":
            available = detail.get("available", ())
            names = ", ".join(str(name) for name in available)
            return (
                f"Unknown tool {detail.get('tool_name')!r}. Available: "
                f"{names or '<none>'}"
            )
        if reason == "schema_violation":
            return f"Schema violation: {detail.get('detail')}"
        if isinstance(reason, str) and reason.startswith("guard:"):
            return (
                f"Guard {detail.get('guard')!r} denied: "
                f"{detail.get('reason')}"
            )
        if reason == "estimate_invalid":
            return (
                f"Tool estimate for {detail.get('tool_name')!r} is invalid: "
                f"{detail.get('detail')}"
            )
        if reason == "budget_exhausted":
            return (
                f"Budget exhausted for {detail.get('bucket')!r}: "
                f"{detail.get('spent')}+{detail.get('estimate')} > "
                f"{detail.get('limit')}"
            )
        return f"Recorded rejection: {reason or 'rejected'}"

    # ── replay execution helpers (P3.2) ────────────────────────

    def _do_replay_substitute(
        self,
        eid: str,
        tool_use_id: str,
        tool: Tool,
        args: Mapping[str, Any],
        compensates: str | None,
        caused_by: str | None,
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

        self._fold_intent(eid, tool, args, compensates, caused_by)

        new_fields = deepcopy(dict(recorded_node.result.fields))
        new_fields[F_EFFECT_ID] = eid
        new_fields[F_SPEND] = {"tool_calls": 1.0, "wall_seconds": 0.0}
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

        result_fields = recorded_node.result.fields
        return self._tool_result(
            tool_use_id,
            self._recorded_result_content(result_fields),
            is_error=result_fields.get(F_STATUS) == "error",
        )

    def _do_replay_refuse(
        self,
        eid: str,
        tool: Tool,
        args: Mapping[str, Any],
        compensates: str | None,
        caused_by: str | None,
        decision: ReplayDecision,
    ) -> None:
        """Fold intent + rejection for a refused replay, then raise.

        Always raises ReplayRefused; never returns.
        """
        self._fold_intent(eid, tool, args, compensates, caused_by)
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
    ) -> tuple[dict[str, float] | None, ToolEstimateRejection | None]:
        """Upper-bound spend estimate for pre-flight budget checking.

        Always includes ``tool_calls=1.0``.  Other buckets come from
        ``tool.estimate_fn(args)`` if declared.  ``wall_seconds`` is
        only included if the tool's estimate_fn declared it (the
        tool's claim of the maximum wall time it will consume — must
        be enforced by the tool itself per D2).

        A malformed estimate is a pre-flight rejection. The estimate is also
        reused for post-call accounting, so the recorded spend matches the
        exact value admitted by the budget gate.
        """
        upper: dict[str, float] = {"tool_calls": 1.0}
        if tool.estimate_fn is None:
            return upper, None
        try:
            # Estimation is also observational: it cannot be allowed to
            # rewrite the arguments admitted by schema/guard checks.
            estimate = tool.estimate_fn(deepcopy(dict(args)))
            if not isinstance(estimate, Mapping):
                raise TypeError("estimate must return a mapping of bucket names to amounts")
            for bucket, amount in estimate.items():
                if not isinstance(bucket, str) or not bucket:
                    raise ValueError(f"invalid bucket name {bucket!r}")
                if bucket not in tool.declared_buckets:
                    raise ValueError(
                        f"estimate returned undeclared bucket {bucket!r}; "
                        f"declare it with @tool(declared_buckets=...)"
                    )
                if (
                    isinstance(amount, bool)
                    or not isinstance(amount, (int, float))
                    or not math.isfinite(float(amount))
                    or amount < 0
                ):
                    raise ValueError(
                        f"estimate for {bucket!r} must be a finite non-negative number, got {amount!r}"
                    )
                upper[bucket] = float(amount)
        except Exception as exc:
            return None, ToolEstimateRejection(
                tool_name=tool.name,
                detail=f"{type(exc).__name__}: {exc}",
            )
        return upper, None

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
        self, estimate: Mapping[str, float], wall_seconds: float,
    ) -> dict[str, float]:
        """Build the {bucket: amount} dict to fold as resource_spent.

        ``tool_calls`` and ``wall_seconds`` are system-managed and always
        recorded (overriding any estimate value of the same key). Other
        buckets reuse the validated pre-flight estimate, so a non-deterministic
        estimate_fn cannot admit one amount and record another.
        """
        spend = dict(estimate)
        spend["tool_calls"] = 1.0
        spend["wall_seconds"] = float(wall_seconds)
        return spend

    # ── fold helpers ───────────────────────────────────────────

    def _fold_intent(
        self,
        eid: str,
        tool: Tool,
        args: Mapping[str, Any],
        compensates: str | None,
        caused_by: str | None,
    ) -> None:
        fields: dict = {
            F_EFFECT_ID:             eid,
            F_KIND:                  EffectKind.TOOL_CALL.value,
            F_TOOL_NAME:             tool.name,
            F_ARGUMENTS:             deepcopy(dict(args)),
            F_DECLARED_SIDE_EFFECT:  tool.side_effect.value,
        }
        if compensates is not None:
            fields[F_COMPENSATES] = compensates
        if caused_by is not None:
            fields[F_CAUSED_BY] = caused_by
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
        caused_by: str | None,
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
            F_ARGUMENTS:             deepcopy(dict(args)),
            F_DECLARED_SIDE_EFFECT:  "external_write",
        }
        if compensates is not None:
            fields[F_COMPENSATES] = compensates
        if caused_by is not None:
            fields[F_CAUSED_BY] = caused_by
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
        *,
        spend: Mapping[str, float],
    ) -> None:
        fields: dict = {
            F_EFFECT_ID:    eid,
            F_KIND:         EffectKind.TOOL_CALL.value,
            F_ATTEMPTS:     1,            # no retry layer (D6)
            F_STATUS:       status,
            F_OUTPUT:       deepcopy(output),
            F_SIDE_EFFECT:  tool.side_effect.value,
            F_SPEND:        dict(spend),
            # ^ actual side-effect class.  v0.1: equals declared.  Future
            #   work: a tool may downgrade (declared EXTERNAL_WRITE,
            #   actual no-op IDEMPOTENT_WRITE because cache-hit) and
            #   record a tighter actual class here.
        }
        if status == "error" and error_detail is not None:
            fields[F_ERROR] = deepcopy(error_detail)
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
            claim_id = self._spend_claim_id(eid, bucket)
            if any(
                claim.claim_id == claim_id
                for tag in (TAG_RESOURCE_SPENT, TAG_BUDGET_OVERRUN)
                for claim in self.rowset.store.filter(tag=tag)
            ):
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
                    claim_id=claim_id,
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
                    claim_id=claim_id,
                ))

    @staticmethod
    def _spend_claim_id(effect_id: str, bucket: str) -> str:
        digest = hashlib.sha256(
            f"tool-spend:{effect_id}:{bucket}".encode("utf-8"),
        ).hexdigest()[:24]
        return f"spend_{digest}"

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
