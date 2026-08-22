"""
Effect Row — the kind-discriminated causal/lineage projection.

Owns ``effect_intent`` / ``effect_result`` / ``effect_rejected``
claims.  Each claim carries a ``F_KIND`` discriminator (``"llm_call"``
or ``"tool_call"``) which routes validation to a kind-specific
sub-validator.  Cross-kind lineage (``F_COMPENSATES``) is fully
supported: an LLM call can compensate a tool call and vice-versa.

Schema overview
---------------
Shared on every claim:
    F_EFFECT_ID     str (regex-validated)
    F_KIND          "llm_call" | "tool_call"
    F_COMPENSATES   str | absent

Intent extras (kind-specific):
    LLM_CALL  → F_MODEL, F_REQUEST
    TOOL_CALL → F_TOOL_NAME, F_ARGUMENTS, F_DECLARED_SIDE_EFFECT

Result extras (kind-specific):
    LLM_CALL  → F_REPLY (always present, P2.7 rule preserved)
    TOOL_CALL → F_OUTPUT (always present), F_SIDE_EFFECT (actual class
                that occurred, ≤ declared)
Plus shared on result: F_STATUS, F_ATTEMPTS; F_ERROR iff status=error.

Rejected: F_REASON, F_DETAIL — kind-agnostic.  Pre-flight rejections
record F_KIND for filtering symmetry but no kind-specific extras.

Persisted names
---------------
Use the ``TAG_EFFECT_*`` / ``F_EFFECT_ID`` names from ``lipas.effect``.
The historical strings stored on disk are opaque implementation details.

ATOMICITY WARNING
    All call sites that fold intent / result / rejected MUST go through
    a RowSet that owns this EffectRow.  Folding these tags directly
    into the underlying ClaimStore (or through a different RowSet that
    lacks an EffectRow) defeats invariant enforcement.
"""
from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field

from ..calculus import Claim, StrategyRegistry
from ..adapter import Usage
from ..store    import ClaimStore
from ..effect import (
    EffectKind,
    F_ARGUMENTS, F_ATTEMPTS, F_CAUSED_BY, F_COMPENSATES, F_DECLARED_SIDE_EFFECT,
    F_DETAIL, F_EFFECT_ID, F_ERROR, F_KIND, F_MODEL, F_OUTPUT, F_SPEND,
    F_REASON, F_REPLY, F_REQUEST, F_SIDE_EFFECT, F_STATUS,
    F_TOOL_NAME, F_TOTAL_USAGE,
    TAG_EFFECT_INTENT, TAG_EFFECT_RESULT, TAG_EFFECT_REJECTED,
)


__all__ = [
    "EffectRow", "EffectNode", "EffectView",
    # Re-exports for convenience (canonical home: lipas.effect):
    "TAG_EFFECT_INTENT", "TAG_EFFECT_RESULT", "TAG_EFFECT_REJECTED",
    "F_EFFECT_ID", "F_KIND", "F_COMPENSATES", "F_CAUSED_BY",
    "F_STATUS", "F_ATTEMPTS", "F_TOTAL_USAGE", "F_SPEND",
    "F_ERROR", "F_REASON", "F_DETAIL",
    "F_MODEL", "F_REQUEST", "F_REPLY",
    "F_TOOL_NAME", "F_ARGUMENTS", "F_DECLARED_SIDE_EFFECT",
    "F_OUTPUT", "F_SIDE_EFFECT",
]


# Effect ids may use either "call_" (LLM) or "tool_" (tool) prefix. Both use
# a 12-hex-char suffix. Validators accept
# either; harnesses pick the prefix matching their kind.
_EFFECT_ID_RE = re.compile(r"^(call|tool)_[0-9a-f]{12}$")
_VALID_STATUS = frozenset({"ok", "error"})
_VALID_KINDS  = frozenset({k.value for k in EffectKind})

# Side-effect class string values, validated lazily (avoid runtime
# import cycle into tools.py).  tools.SideEffectClass MUST stay a
# subset of these.
_VALID_SIDE_EFFECTS = frozenset({
    "pure", "read_only", "idempotent_write", "external_write",
})


# =====================================================================
# Projection types
# =====================================================================

@dataclass(frozen=True)
class EffectNode:
    """One effect's full lineage view: intent + (result or rejection).

    ``kind`` is the parsed ``EffectKind`` from the intent's F_KIND.
    Result / rejection share the same kind by construction (validated
    on fold).  Callers that need kind-specific payload extraction
    should branch on ``kind``:

        if node.kind is EffectKind.LLM_CALL:
            reply = node.result.fields[F_REPLY]
        elif node.kind is EffectKind.TOOL_CALL:
            output = node.result.fields[F_OUTPUT]
    """
    effect_id: str
    kind: EffectKind
    intent: Claim
    result: Claim | None
    compensates: str | None
    compensated_by: tuple[str, ...]
    rejection: Claim | None = None

    @property
    def is_terminal(self) -> bool:
        return self.result is not None or self.rejection is not None

@dataclass(frozen=True)
class EffectView:
    """Read-only projection of all effect-namespace claims.

    Provides:
      - ``nodes``: id-indexed map of every intent (terminal or not).
      - ``orphans``: ids whose intent has no terminal claim (the
        crash-safety surface).
      - ``rejected``: ids whose terminal claim is a rejection.
      - ``chain(id)`` / ``descendants(id)``: lineage walks.
      - ``llm_nodes()`` / ``tool_nodes()``: kind-filtered iterators
        (replay and ToolHarness use these; both want one kind at a
        time).
    """
    nodes:    Mapping[str, EffectNode]
    orphans:  tuple[str, ...]
    rejected: tuple[str, ...] = ()

    def chain(self, effect_id: str) -> tuple[EffectNode, ...]:
        out: list[EffectNode] = []
        seen: set[str] = set()
        cur = self.nodes.get(effect_id)
        while cur is not None and cur.effect_id not in seen:
            out.append(cur)
            seen.add(cur.effect_id)
            if cur.compensates is None:
                break
            cur = self.nodes.get(cur.compensates)
        out.reverse()
        return tuple(out)

    def descendants(self, effect_id: str) -> tuple[EffectNode, ...]:
        root = self.nodes.get(effect_id)
        if root is None:
            return ()
        out: list[EffectNode] = []
        seen: set[str] = {effect_id}
        queue: list[str] = list(root.compensated_by)
        while queue:
            cid = queue.pop(0)
            if cid in seen:
                continue
            seen.add(cid)
            node = self.nodes.get(cid)
            if node is None:
                continue
            out.append(node)
            queue.extend(node.compensated_by)
        return tuple(out)

    def llm_nodes(self) -> tuple[EffectNode, ...]:
        return tuple(n for n in self.nodes.values()
                     if n.kind is EffectKind.LLM_CALL)

    def tool_nodes(self) -> tuple[EffectNode, ...]:
        return tuple(n for n in self.nodes.values()
                     if n.kind is EffectKind.TOOL_CALL)


# =====================================================================
# Row
# =====================================================================

@dataclass
class EffectRow:
    name: str = "effect"
    namespace: frozenset[str] = field(
        default_factory=lambda: frozenset({
            TAG_EFFECT_INTENT, TAG_EFFECT_RESULT, TAG_EFFECT_REJECTED,
        })
    )

    def register_strategies(self, registry: StrategyRegistry) -> None:
        return

    # ── invariants (top-level dispatch) ───────────────────────

    def check_invariants(self, claim: Claim, store: ClaimStore) -> list[str]:
        for c in store.filter(tag=claim.tag):
            if c.claim_id == claim.claim_id:
                return []

        if claim.tag == TAG_EFFECT_INTENT:
            return self._check_intent(claim, store)
        if claim.tag == TAG_EFFECT_RESULT:
            return self._check_result(claim, store)
        if claim.tag == TAG_EFFECT_REJECTED:
            return self._check_rejection(claim, store)
        return []

    # ── invariants: intent ────────────────────────────────────

    def _check_intent(self, claim: Claim, store: ClaimStore) -> list[str]:
        msgs: list[str] = []
        f = claim.fields

        eid = f.get(F_EFFECT_ID)
        if not isinstance(eid, str):
            msgs.append(
                f"{TAG_EFFECT_INTENT}: missing or non-str {F_EFFECT_ID!r}"
            )
            return msgs
        if not _EFFECT_ID_RE.match(eid):
            msgs.append(
                f"{TAG_EFFECT_INTENT}: {F_EFFECT_ID}={eid!r} does not "
                f"match {_EFFECT_ID_RE.pattern!r}"
            )

        for c in store.filter(tag=TAG_EFFECT_INTENT):
            if c.fields.get(F_EFFECT_ID) == eid:
                msgs.append(
                    f"{TAG_EFFECT_INTENT}: duplicate {F_EFFECT_ID}={eid!r} "
                    f"(prior claim_id={c.claim_id!r})"
                )
                break

        comp = f.get(F_COMPENSATES)
        if comp is not None:
            if not isinstance(comp, str):
                msgs.append(
                    f"{TAG_EFFECT_INTENT}: {F_COMPENSATES} must be "
                    f"str | None, got {type(comp).__name__}"
                )
            else:
                known = {
                    c.fields.get(F_EFFECT_ID)
                    for c in store.filter(tag=TAG_EFFECT_INTENT)
                }
                if comp not in known:
                    msgs.append(
                        f"{TAG_EFFECT_INTENT}: {F_COMPENSATES}={comp!r} "
                        f"does not reference any known intent {F_EFFECT_ID}"
                    )

        # Kind discriminator + kind-specific field checks.
        kind = f.get(F_KIND)
        if kind not in _VALID_KINDS:
            msgs.append(
                f"{TAG_EFFECT_INTENT}: {F_KIND}={kind!r} not in "
                f"{sorted(_VALID_KINDS)}"
            )
            return msgs

        if kind == EffectKind.LLM_CALL.value:
            msgs.extend(self._check_intent_llm(f))
        elif kind == EffectKind.TOOL_CALL.value:
            msgs.extend(self._check_intent_tool(f))
        return msgs

    def _check_intent_llm(self, f: Mapping) -> list[str]:
        msgs: list[str] = []
        if F_MODEL not in f or not isinstance(f[F_MODEL], str):
            msgs.append(
                f"{TAG_EFFECT_INTENT}[llm_call]: {F_MODEL!r} required (str)"
            )
        if F_REQUEST not in f:
            msgs.append(
                f"{TAG_EFFECT_INTENT}[llm_call]: {F_REQUEST!r} required"
            )
        return msgs

    def _check_intent_tool(self, f: Mapping) -> list[str]:
        msgs: list[str] = []
        name = f.get(F_TOOL_NAME)
        if not isinstance(name, str) or not name:
            msgs.append(
                f"{TAG_EFFECT_INTENT}[tool_call]: {F_TOOL_NAME!r} required "
                f"(non-empty str)"
            )
        args = f.get(F_ARGUMENTS)
        if args is not None and not isinstance(args, Mapping):
            msgs.append(
                f"{TAG_EFFECT_INTENT}[tool_call]: {F_ARGUMENTS!r} must be "
                f"Mapping | None, got {type(args).__name__}"
            )
        decl = f.get(F_DECLARED_SIDE_EFFECT)
        if decl not in _VALID_SIDE_EFFECTS:
            msgs.append(
                f"{TAG_EFFECT_INTENT}[tool_call]: "
                f"{F_DECLARED_SIDE_EFFECT}={decl!r} not in "
                f"{sorted(_VALID_SIDE_EFFECTS)}"
            )
        return msgs

    # ── invariants: result ────────────────────────────────────

    def _check_result(self, claim: Claim, store: ClaimStore) -> list[str]:
        msgs: list[str] = []
        f = claim.fields

        eid = f.get(F_EFFECT_ID)
        if not isinstance(eid, str):
            msgs.append(
                f"{TAG_EFFECT_RESULT}: missing or non-str {F_EFFECT_ID!r}"
            )
            return msgs

        # Find matching intent.
        intent: Claim | None = None
        for c in store.filter(tag=TAG_EFFECT_INTENT):
            if c.fields.get(F_EFFECT_ID) == eid:
                intent = c
                break
        if intent is None:
            msgs.append(
                f"{TAG_EFFECT_RESULT}: {F_EFFECT_ID}={eid!r} has no "
                f"matching prior {TAG_EFFECT_INTENT}"
            )

        prior = [
            c for c in store.filter(tag=TAG_EFFECT_RESULT)
            if c.fields.get(F_EFFECT_ID) == eid
        ]
        if prior:
            msgs.append(
                f"{TAG_EFFECT_RESULT}: duplicate result for "
                f"{F_EFFECT_ID}={eid!r} (prior claim_id="
                f"{prior[0].claim_id!r})"
            )

        prior_rej = [
            c for c in store.filter(tag=TAG_EFFECT_REJECTED)
            if c.fields.get(F_EFFECT_ID) == eid
        ]
        if prior_rej:
            msgs.append(
                f"{TAG_EFFECT_RESULT}: cannot fold result for "
                f"{F_EFFECT_ID}={eid!r} after {TAG_EFFECT_REJECTED} "
                f"(prior claim_id={prior_rej[0].claim_id!r})"
            )

        # Kind must match the intent's kind (when available).
        kind = f.get(F_KIND)
        if kind not in _VALID_KINDS:
            msgs.append(
                f"{TAG_EFFECT_RESULT}: {F_KIND}={kind!r} not in "
                f"{sorted(_VALID_KINDS)}"
            )
            return msgs
        if intent is not None:
            intent_kind = intent.fields.get(F_KIND)
            if intent_kind != kind:
                msgs.append(
                    f"{TAG_EFFECT_RESULT}: {F_KIND}={kind!r} does not "
                    f"match intent {F_KIND}={intent_kind!r} for "
                    f"{F_EFFECT_ID}={eid!r}"
                )

        # Status check (shared).
        status = f.get(F_STATUS)
        if status not in _VALID_STATUS:
            msgs.append(
                f"{TAG_EFFECT_RESULT}: {F_STATUS}={status!r} not in "
                f"{sorted(_VALID_STATUS)}"
            )
            return msgs

        has_error = F_ERROR in f
        if status == "ok" and has_error:
            msgs.append(
                f"{TAG_EFFECT_RESULT}: status='ok' must not carry {F_ERROR!r}"
            )
        if status == "error" and not has_error:
            msgs.append(
                f"{TAG_EFFECT_RESULT}: status='error' requires {F_ERROR!r}"
            )

        attempts = f.get(F_ATTEMPTS)
        if attempts is not None and (
            isinstance(attempts, bool)
            or not isinstance(attempts, int)
            or attempts < 1
        ):
            msgs.append(
                f"{TAG_EFFECT_RESULT}: {F_ATTEMPTS!r} must be a positive int"
            )

        # Kind-specific payload checks.
        if kind == EffectKind.LLM_CALL.value:
            msgs.extend(self._check_result_llm(f))
        elif kind == EffectKind.TOOL_CALL.value:
            msgs.extend(self._check_result_tool(f))

        return msgs

    def _check_result_llm(self, f: Mapping) -> list[str]:
        # P2.7 rule preserved: F_REPLY required regardless of status.
        msgs: list[str] = []
        if F_REPLY not in f:
            msgs.append(f"{TAG_EFFECT_RESULT}[llm_call]: {F_REPLY!r} required")
        total_usage = f.get(F_TOTAL_USAGE)
        if total_usage is not None and not isinstance(total_usage, Usage):
            msgs.append(
                f"{TAG_EFFECT_RESULT}[llm_call]: {F_TOTAL_USAGE!r} must be Usage"
            )
        return msgs

    def _check_result_tool(self, f: Mapping) -> list[str]:
        msgs: list[str] = []
        # F_OUTPUT required regardless of status (parallel to F_REPLY).
        # Empty / None is allowed; absent is not.
        if F_OUTPUT not in f:
            msgs.append(
                f"{TAG_EFFECT_RESULT}[tool_call]: {F_OUTPUT!r} required "
                f"(use None or empty value if the tool body returned nothing)"
            )
        # F_SIDE_EFFECT required: the actual class observed.
        actual = f.get(F_SIDE_EFFECT)
        if actual not in _VALID_SIDE_EFFECTS:
            msgs.append(
                f"{TAG_EFFECT_RESULT}[tool_call]: "
                f"{F_SIDE_EFFECT}={actual!r} not in "
                f"{sorted(_VALID_SIDE_EFFECTS)}"
            )
        spend = f.get(F_SPEND)
        if spend is not None:
            if not isinstance(spend, Mapping):
                msgs.append(
                    f"{TAG_EFFECT_RESULT}[tool_call]: {F_SPEND!r} must be a mapping"
                )
            else:
                for bucket, amount in spend.items():
                    if (
                        not isinstance(bucket, str)
                        or not bucket
                        or isinstance(amount, bool)
                        or not isinstance(amount, (int, float))
                        or not math.isfinite(float(amount))
                        or amount < 0
                    ):
                        msgs.append(
                            f"{TAG_EFFECT_RESULT}[tool_call]: invalid "
                            f"{F_SPEND} entry {bucket!r}={amount!r}"
                        )
                        break
        return msgs

    # ── invariants: rejection ─────────────────────────────────

    def _check_rejection(self, claim: Claim, store: ClaimStore) -> list[str]:
        msgs: list[str] = []
        f = claim.fields

        eid = f.get(F_EFFECT_ID)
        if not isinstance(eid, str):
            msgs.append(
                f"{TAG_EFFECT_REJECTED}: missing or non-str {F_EFFECT_ID!r}"
            )
            return msgs

        intents = [
            c for c in store.filter(tag=TAG_EFFECT_INTENT)
            if c.fields.get(F_EFFECT_ID) == eid
        ]
        if not intents:
            msgs.append(
                f"{TAG_EFFECT_REJECTED}: {F_EFFECT_ID}={eid!r} has no "
                f"matching prior {TAG_EFFECT_INTENT}"
            )

        prior_result = [
            c for c in store.filter(tag=TAG_EFFECT_RESULT)
            if c.fields.get(F_EFFECT_ID) == eid
        ]
        if prior_result:
            msgs.append(
                f"{TAG_EFFECT_REJECTED}: cannot reject {F_EFFECT_ID}={eid!r} "
                f"after {TAG_EFFECT_RESULT} (prior claim_id="
                f"{prior_result[0].claim_id!r})"
            )

        prior_rej = [
            c for c in store.filter(tag=TAG_EFFECT_REJECTED)
            if c.fields.get(F_EFFECT_ID) == eid
        ]
        if prior_rej:
            msgs.append(
                f"{TAG_EFFECT_REJECTED}: duplicate rejection for "
                f"{F_EFFECT_ID}={eid!r} (prior claim_id="
                f"{prior_rej[0].claim_id!r})"
            )

        # Kind required for filtering symmetry.  No kind-specific
        # extras on rejection — same shape regardless of what was
        # being attempted.
        kind = f.get(F_KIND)
        if kind not in _VALID_KINDS:
            msgs.append(
                f"{TAG_EFFECT_REJECTED}: {F_KIND}={kind!r} not in "
                f"{sorted(_VALID_KINDS)}"
            )

        reason = f.get(F_REASON)
        if not isinstance(reason, str) or not reason:
            msgs.append(
                f"{TAG_EFFECT_REJECTED}: {F_REASON} must be a non-empty str"
            )

        detail = f.get(F_DETAIL)
        if detail is not None and not isinstance(detail, dict):
            msgs.append(
                f"{TAG_EFFECT_REJECTED}: {F_DETAIL} must be dict | None, "
                f"got {type(detail).__name__}"
            )

        return msgs

    # ── projection ────────────────────────────────────────────

    def project(self, store: ClaimStore) -> EffectView:
        intents_by_eid: dict[str, Claim] = {}
        seen_ids: set[str] = set()
        for c in store.filter(tag=TAG_EFFECT_INTENT):
            if c.claim_id in seen_ids:
                continue
            seen_ids.add(c.claim_id)
            eid = c.fields.get(F_EFFECT_ID)
            if not isinstance(eid, str):
                continue
            intents_by_eid.setdefault(eid, c)

        results_by_eid: dict[str, Claim] = {}
        seen_ids = set()
        for c in store.filter(tag=TAG_EFFECT_RESULT):
            if c.claim_id in seen_ids:
                continue
            seen_ids.add(c.claim_id)
            eid = c.fields.get(F_EFFECT_ID)
            if not isinstance(eid, str):
                continue
            results_by_eid.setdefault(eid, c)

        rejections_by_eid: dict[str, Claim] = {}
        seen_ids = set()
        for c in store.filter(tag=TAG_EFFECT_REJECTED):
            if c.claim_id in seen_ids:
                continue
            seen_ids.add(c.claim_id)
            eid = c.fields.get(F_EFFECT_ID)
            if not isinstance(eid, str):
                continue
            rejections_by_eid.setdefault(eid, c)

        compensated_by: dict[str, list[str]] = {}
        for eid, intent in intents_by_eid.items():
            comp = intent.fields.get(F_COMPENSATES)
            if isinstance(comp, str) and comp in intents_by_eid:
                compensated_by.setdefault(comp, []).append(eid)

        nodes:    dict[str, EffectNode] = {}
        orphans:  list[str] = []
        rejected: list[str] = []
        for eid, intent in intents_by_eid.items():
            result    = results_by_eid.get(eid)
            rejection = rejections_by_eid.get(eid)
            if result is None and rejection is None:
                orphans.append(eid)
            elif rejection is not None:
                rejected.append(eid)

            # Parse kind defensively — invariant should prevent
            # missing/invalid kind from being folded, but a corrupted
            # store should project rather than crash.
            kind_str = intent.fields.get(F_KIND)
            try:
                kind = EffectKind(kind_str)
            except (ValueError, TypeError):
                continue  # skip malformed; visible as missing node

            comp = intent.fields.get(F_COMPENSATES)
            nodes[eid] = EffectNode(
                effect_id=eid,
                kind=kind,
                intent=intent,
                result=result,
                compensates=comp if isinstance(comp, str) else None,
                compensated_by=tuple(compensated_by.get(eid, ())),
                rejection=rejection,
            )

        return EffectView(
            nodes=nodes,
            orphans=tuple(orphans),
            rejected=tuple(rejected),
        )

    def __repr__(self) -> str:
        return f"EffectRow(namespace={sorted(self.namespace)})"
