"""
LIPAS · Layer 0 — Claim Calculus
================================

The algebraic foundation.  Everything above this file is derived
from the primitives defined here.

Primitives
    Claim           A partial, typed assertion carrying named fields.
    ⊕_b             Belief-indexed merge  (``merge``).
    Strategy        Per-field merge function — the ONE irreducible
                    primitive that users supply.

Derived operations
    fold(b, c)         = merge(b, c, ctx, reg)             — common case
    reduce(b, c₁, c₂)  = fold(b, merge(c₁, c₂, ctx, reg))  — reduction rule

Algebraic guarantees
    Each field is resolved by a registered MergeStrategy.

    *Semilattice* strategies (max, min, union, counter_max,
    expectations_merge) satisfy idempotency, commutativity, and
    associativity.

    *Ordered* strategies (keep, last_write, append, belief_adaptive)
    deliberately depend on fold order. Append is associative but duplicate
    folds are not harmless; keep/last_write are idempotent assignments but
    are not commutative.

    ClaimStore prevents duplicate logical claim ids before merge. Semilattice
    fields additionally tolerate reordering; ordered fields do not.

~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

lipas.calculus — fold strategies.

NORMATIVE: Strategy purity contract
====================================

A strategy function is any callable registered to the strategy registry,
with signature::

    strategy(a, b, ctx, registry) -> a'

Strategies MUST be pure functions of their four arguments. Concretely:

  ALLOWED inputs to a strategy:
    - `a`, `b`           : the two claim/row values being merged
    - `ctx`              : the BeliefContext, treated as read-only (§3.2)
    - `registry`         : the strategy registry, treated as read-only

  FORBIDDEN inputs (non-exhaustive; the test tool enumerates the closed list):
    - time.time(), time.monotonic(), datetime.now(), datetime.utcnow()
    - random.* (any module-level state)
    - os.environ (read or write)
    - any I/O: open(), socket, subprocess, http, file reads
    - any global mutable state outside `a`, `b`, `ctx`, `registry`
    - any thread/process identity (os.getpid, threading.current_thread)

  FORBIDDEN side effects:
    - mutation of `a`, `b` in place (return a new value)
    - mutation of `ctx` (see §3.2)
    - mutation of `registry`
    - logging at WARNING or above (DEBUG/INFO is tolerated; see §3.3)

  REQUIRED:
    - Determinism: strategy(a, b, ctx, registry) called twice with equal
      arguments MUST produce equal results, in any process, on any host.
    - Totality on the declared domain: strategies SHOULD NOT raise on
      well-typed inputs; if they do, the exception type itself becomes
      part of the fold semantics and MUST be deterministic.

This contract is enforced by a repository test helper. It is NOT enforced in
production: the cost of runtime sandboxing is not
justified, and the test gate is sufficient because strategies are a
closed set registered at import time.

Replay depends on this contract: a strategy that reads time, process state,
or I/O can make the same claim tape project differently on another run.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

# ================================================================
#  Type aliases
# ================================================================

# A MergeStrategy resolves two values for the same field,
# optionally consulting the current belief context.
MergeStrategy = Callable[[Any, Any, "BeliefContext"], Any]


# ================================================================
#  Strategy registry
# ================================================================


class StrategyRegistry:
    """
    Maps field names → MergeStrategy.

    Instance-based so that different agents (or tests) can maintain
    independent strategy tables without interference.

    The built-in default for unregistered fields is ``strategy_last_write``.
    This makes ``merged`` a deterministic current-value projection in fold
    order, not a commutative semilattice. Call ``set_default()`` to override.

    Hashability constraint. Strategy implementations MUST NOT hash
    field values (no set(), frozenset(), dict.fromkeys(), no
    using values as dict keys). Field values legitimately include
    unhashable shapes (e.g. F_REQUEST carries a Request whose
    messages: tuple[dict, ...] is structurally unhashable).
    Strategies that need de-duplication should compare by
    __eq__ over a list, or hash an explicitly-derived key
    (e.g. effect_id).
    """

    def __init__(self) -> None:
        self._table: dict[str, MergeStrategy] = {}
        self._default: MergeStrategy = strategy_last_write

    # -- mutators --------------------------------------------------

    def register(self, field_name: str, strategy: MergeStrategy) -> None:
        """Bind *field_name* to *strategy*."""
        self._table[field_name] = strategy

    def set_default(self, strategy: MergeStrategy) -> None:
        """Replace the fallback strategy for unregistered fields."""
        self._default = strategy

    # -- lookup ----------------------------------------------------

    def get(self, field_name: str) -> MergeStrategy:
        """Return the strategy for *field_name*, or the default."""
        return self._table.get(field_name, self._default)

    def __contains__(self, field_name: str) -> bool:
        return field_name in self._table

    def __repr__(self) -> str:
        names = ", ".join(sorted(self._table))
        return f"StrategyRegistry([{names}])"


# ================================================================
#  Built-in strategies
# ================================================================
#
# Naming convention:
#     strategy_<name>(left, right, ctx) -> merged_value
#
# Each docstring states the algebraic properties.
# "Semilattice" = idempotent + commutative + associative.
# "Monoid"      = associative + identity (but possibly not idempotent
#                 or commutative).


def strategy_max(a: Any, b: Any, ctx: "BeliefContext") -> Any:
    """Higher value wins.  *Semilattice.*"""
    if a is None:
        return b
    if b is None:
        return a
    return max(a, b)


def strategy_min(a: Any, b: Any, ctx: "BeliefContext") -> Any:
    """Lower value wins.  *Semilattice.*"""
    if a is None:
        return b
    if b is None:
        return a
    return min(a, b)


def strategy_union(a: Any, b: Any, ctx: "BeliefContext") -> Any:
    """Set union.  *Semilattice.*"""
    return set(a or []) | set(b or [])


def strategy_append(a: Any, b: Any, ctx: "BeliefContext") -> Any:
    """
    List concatenation. Algebraic Properties:
    - Associative: (a + b) + c == a + (b + c)
    - Monotone: Information content only grows.
    - NOT Commutative: strategy_append([A], [B]) != strategy_append([B], [A]).
      The order of arrival determines the order in the list.
    - NOT Idempotent: strategy_append([X], [X]) -> [X, X].
      Duplicate merges lead to duplicate entries.

    Callers must ensure each claim is folded exactly once.  *Monoid.*
    """
    return (a or []) + (b or [])


def strategy_keep(a: Any, b: Any, ctx: "BeliefContext") -> Any:
    """
    First-write-wins: once a field is set, it is never overwritten.
    *Ordered, idempotent assignment; not a semilattice.*

    Algebraic Properties:
    - Idempotent and associative, but not commutative.
    - Monotonic: Information transitions from 'None' to 'Value' and then locks.
    - Order-Dependent (Not Commutative): In a logical sense, as it assumes any
      non-None value is the 'Final Truth' for this specific claim chain.

    Engineering Purpose:
    - Anchoring Facts: Use for fields that should never change once established
      (e.g., Task ID, Original Timestamp, or First Error Reporter).
    - Noise Filtering: Automatically discards late-arriving, redundant, or
      conflicting updates once the initial state is captured.

    Note:
    Unlike 'strategy_last_write', this provides high stability in
    asynchronous environments by honoring the 'first witness' of an event.
    """
    return a if a is not None else b


def strategy_last_write(a: Any, b: Any, ctx: "BeliefContext") -> Any:
    """
    Newer value wins.  NOT commutative — merge order matters.
    Use when you genuinely need mutable state and accept that
    the merge must be applied in a controlled sequence.
    *Unlike keep, this is NOT a semilattice.* assignment.
    # ← This is an assignment (not commutative, not idempotent)
    """
    return b if b is not None else a


def strategy_counter_max(a: Any, b: Any, ctx: "BeliefContext") -> Any:
    """
    Point-wise max over counter dicts ``{key: int}``.
    *Semilattice.*

    Each claim reports the *total* count it knows about; the merge
    keeps the higher number for every key.  This is how LIPAS
    maintains monotone failure counters without breaking idempotency.
    """
    result = dict(a or {})
    for k, v in (b or {}).items():
        result[k] = max(result.get(k, 0), v)
    return result


def strategy_expectations_merge(a: Any, b: Any, ctx: "BeliefContext") -> Any:
    """
    Per-key set union over ``{str: set(OutcomeTag)}`` dicts.
    *Semilattice.*

    Each key is a commitment type; each value is the set of
    OutcomeTags the agent has learned to expect for that type.
    """
    result = dict(a or {})
    for k, v in (b or {}).items():
        result[k] = result.get(k, set()) | set(v)
    return result


def strategy_belief_adaptive(a: Any, b: Any, ctx: "BeliefContext") -> Any:
    """
    Belief-indexed adaptive strategy (The core of ⊕_b).

    Behavior:
    - Optimistic Mode: If failures are low, it acts like 'last_write' (Newer wins).
    - Defensive Mode: If failures reach 'caution_threshold', it acts like 'keep'
      (Existing wins), protecting the agent from potentially corrupted data.

    This is where the *b* in ⊕_b becomes concrete — the merge
    outcome depends on what the agent currently believes.

    Isolation & Context:
    - To prevent 'Environment A' failures from polluting 'Environment B', ensure
      Claims carry a *'kind' label*. This strategy should ideally consult
      ``ctx.fail_count_for(kind)`` instead of a global counter.

    Algebraic Properties:
    - NOT Commutative: In defensive mode, the 'existing' value (first argument)
      is structurally privileged. Always place the 'Current Belief' as 'a'.
    - NOT a simple Semilattice: Outcome depends on the hidden state of 'ctx'.

    Use Case:
    - Use for high-stakes sensor data or external API inputs where the agent
      must 'close the gates' when it senses the environment is becoming unreliable.
    """
    if ctx is None:
        return b if b is not None else a
    if ctx.total_failures() >= ctx.caution_threshold:
        return a if a is not None else b
    return b if b is not None else a


# ================================================================
#  Belief context
# ================================================================


@dataclass
class BeliefContext:
    """
    The *b* in ⊕_b.

    Passed into every strategy call so that belief-adaptive
    strategies can consult the agent's accumulated state without
    needing a direct reference to the full Belief object.
    """

    fail_counts: dict = field(default_factory=dict)
    caution_threshold: int = 3

    def total_failures(self) -> int:
        """Sum of all failure counters."""
        return sum(self.fail_counts.values()) if self.fail_counts else 0

    def fail_count_for(self, commitment_type: Optional[str]) -> int:
        """Total failures for one commitment type."""
        if commitment_type is None:
            return 0
        return sum(
            v
            for k, v in self.fail_counts.items()
            if isinstance(k, tuple) and len(k) == 2 and k[0] == commitment_type
        )


# ================================================================
#  Claim
# ================================================================


@dataclass
class Claim:
    """
    The single value type of Claim Calculus.

    A Claim is a partial, typed assertion about the world or the
    agent's internal state.  It carries four pieces:

        tag         Semantic category (what kind of claim this is).
        fields      Named values (the content).  A field whose value
                    is ``None`` is treated as *absent* — carrying no
                    information.
        kind        Optional commitment-type label.  Propagated
                    through merges so that belief-adaptive strategies
                    can identify the originating commitment.
        priority    Numeric.  When two non-⊥ claims merge, the
                    higher-priority claim's tag wins.

    Runtime evidence concepts — observations, commitments, effects,
    outcomes, traces, fail records, expectations — are represented as
    Claims and processed through the same ⊕ operation. Mutable execution
    control state such as leases and checkpoints has a separate authority.
    """

    tag: str
    fields: dict = field(default_factory=dict)
    kind: Optional[str] = None
    priority: int = 0
    # ── Provenance metadata (all optional, filled by Belief.fold) ──
    source: str = ""  # e.g. "step.search", "agent.react", "tool.file_read"
    claim_id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    seq: int = -1  # logical clock — assigned by Belief.fold()

    # -- field access ----------------------------------------------

    def get(self, key: str, default: Any = None) -> Any:
        """Return the value for *key*, or *default* if absent."""
        return self.fields.get(key, default)

    def with_field(self, key: str, value: Any, claim_id: str | None = None) -> "Claim":
        claim_id = claim_id or self.claim_id
        return Claim(
            tag=self.tag,
            fields={**self.fields, key: value},
            kind=self.kind,
            priority=self.priority,
            source=self.source,
            claim_id=claim_id,
        )

    def with_fields(self, updates: dict, claim_id: str | None = None) -> "Claim":
        claim_id = claim_id or self.claim_id
        return Claim(
            tag=self.tag,
            fields={**self.fields, **updates},
            kind=self.kind,
            priority=self.priority,
            source=self.source,
            claim_id=claim_id,
        )

    # -- display ---------------------------------------------------

    def __repr__(self) -> str:
        parts = [f"tag={self.tag!r}"]
        if self.fields:
            keys = sorted(self.fields)
            if len(keys) <= 5:
                parts.append(f"fields={{{', '.join(keys)}}}")
            else:
                parts.append(f"fields=[{len(keys)} keys]")
        if self.kind:
            parts.append(f"kind={self.kind!r}")
        if self.priority:
            parts.append(f"pri={self.priority}")
        if self.source:
            parts.append(f"src={self.source!r}")
        return f"Claim({', '.join(parts)})"


# ⊥  — the identity element of ⊕.
# merge(c, BOTTOM) = merge(BOTTOM, c) = c  for any claim c.
BOTTOM = Claim(tag="⊥")


# ================================================================
#  merge:  ⊕_b
# ================================================================


def merge(
    c1: Claim,
    c2: Claim,
    ctx: BeliefContext,
    registry: StrategyRegistry,
) -> Claim:
    """
    ⊕_b : Claim × Claim × BeliefContext × Registry → Claim

    The **Core Logic Engine (core operation)** of Claim Calculus.

    This function is not a simple data join; it is a context-aware synthesis
    of information. It acts as a 'dispatcher' that delegates conflict resolution
    to specialized strategies.

    Parameters:
    - c1, c2:   The two Claims to be merged.
    - ctx:      The 'Sensory Context'. Provides environmental state (like failure
                counts) to strategies, enabling defensive or adaptive behavior.
    - registry: The 'Law Book'. Maps field names to specific MergeStrategies,
                defining how different types of data should interact.

    That is,
    Merges two claims field-wise: for every field name present in
    either claim, the registered strategy for that name is applied.

    Core Logic:
    1. Identity: If either claim is BOTTOM (⊥), the other is returned as-is.
    2. Field preservation: fields existing in only one claim are preserved.
       Conflicting values follow their registered strategy and may be replaced.
    3. Conflict Resolution: When a field exists in both, the 'Law Book' (registry)
       determines the fusion method, optionally consulting 'ctx'.
    4. Metadata Synthesis:
       - Tag: The 'Priority' determines which semantic tag survives.
         The higher-priority claim's tag wins.
       - Kind: Propagates the first non-None category (provenance).
       - Priority: The resulting claim inherits the highest confidence level.

    Algebraic Properties & Integrity:
    When all field-level strategies are Semilattice joins, the ⊕ operation forms
    a product lattice. This ensures the merge is commutative and idempotent,
    making it resilient to out-of-order or duplicate messages.

    However, if ordered strategies (keep, last_write, append, adaptive) are
    used, order matters. ClaimStore's stable-id admission prevents duplicate
    logical events; it does not make an ordered projection commutative.
    """
    if c1.tag == "⊥":
        return c2
    if c2.tag == "⊥":
        return c1

    all_keys = set(c1.fields) | set(c2.fields)
    merged_fields: dict[str, Any] = {}

    for key in all_keys:
        v1 = c1.fields.get(key)
        v2 = c2.fields.get(key)
        if v1 is None:
            merged_fields[key] = v2
        elif v2 is None:
            merged_fields[key] = v1
        else:
            strategy = registry.get(key)
            merged_fields[key] = strategy(v1, v2, ctx)

    tag = c1.tag if c1.priority >= c2.priority else c2.tag
    kind = c1.kind or c2.kind
    priority = max(c1.priority, c2.priority)

    return Claim(tag, merged_fields, kind, priority)


# ================================================================
#  reduce:  the single reduction rule
# ================================================================


def reduce(
    belief_claim: Claim,
    c1: Claim,
    c2: Claim,
    ctx: BeliefContext,
    registry: StrategyRegistry,
) -> Claim:
    """
    The single reduction rule of Claim Calculus:

        (b, c₁, c₂)  →  b ⊔ (c₁ ⊕_b c₂)

    First merges *c₁* and *c₂* under belief context *ctx*, then
    folds the result into *belief_claim*.

    ``fold(b, c)`` is the common special case where one of the two
    claims is ⊥:  ``reduce(b, ⊥, c) = merge(b, c)``.

    Monotonicity: the returned claim is ≥ *belief_claim* in
    information content (no field is lost).
    """
    merged = merge(c1, c2, ctx, registry)
    return merge(belief_claim, merged, ctx, registry)


# ================================================================
#  Default registry
# ================================================================


def make_default_registry() -> StrategyRegistry:
    """
    Create a :class:`StrategyRegistry` pre-loaded with strategies
    for LIPAS's structural fields (prefixed with ``_``).

    Users should call this, then extend the result with
    domain-specific registrations before passing it to
    :class:`Belief` or :class:`LipasAgent`.
    """
    r = StrategyRegistry()

    # Structural fields (managed by Layer 1)
    r.register("_history", strategy_append)
    r.register("_fail_log", strategy_append)
    r.register("_fail_counts", strategy_counter_max)
    r.register("_expectations", strategy_expectations_merge)

    # Common domain conventions
    r.register("priority", strategy_max)
    r.register("deadline", strategy_min)
    return r
