# LIPAS Foundation: The Claim Algebra

> **Conceptual foundation, not a literal API schema.** The current `Claim`
> object uses a tag plus a field mapping and is specified operationally in
> [`docs/execution-model.md`](../docs/execution-model.md). This note preserves
> the algebraic motivation for claims and folds.


## 1. Purpose

This document identifies the single algebraic operation, from which those
structures and axioms can be derived:

    ⊕  (claim merge)

The relationship between the two documents is analogous to the relationship
between a programming language specification and its denotational semantics.


## 2. Claims: The Primitive

### 2.1 Definition

A **claim** is a typed, sourced assertion carrying a unit of information.

    c = (source, kind, field, value, meta)

where:
  - source ∈ S    — origin (sensor, reasoning step, outcome observation, etc.)
  - kind ∈ K      — one of {epistemic, conative, effectual}
  - field ∈ F     — what aspect of the world or agent state is being asserted
  - value ∈ V_f   — the asserted content, drawn from a domain specific to field f
  - meta          — timestamp, confidence, provenance, etc.

A claim asserts exactly one field. Composite assertions (e.g., "location is X
with confidence 0.9") must be decomposed into multiple claims or modeled as a
single composite field with a joint value domain (see §3.2).

### 2.2 Claim Kinds

Every piece of information in the system is a claim of exactly one kind:

  - **Epistemic** claims assert facts.
    ("obstacle detected at (3,4)", "battery at 40%")

  - **Conative** claims assert intentions.
    ("goal: reach waypoint B", "priority: 7", "type: navigate")

  - **Effectual** claims record actions and their results.
    ("executed move-north", "observed: blocked", "effect-class: external")

This three-way partition corresponds directly to the three LIPAS layers
(Belief, Commitment, Effect) but reframes them as a single homogeneous
population of claims distinguished only by the `kind` tag.

### 2.3 Field-Kind Discipline

The set of valid fields F is partitioned into F_epistemic, F_conative, and
F_effectual. A claim of kind k may only assert values for fields in F_k.

This constraint ensures that the three kinds remain structurally separated:
an epistemic claim cannot carry a priority field, and a conative claim cannot
directly assert a world fact. Cross-kind influence happens only through the
execution cycle — epistemic claims inform deliberation, which generates
conative claims, which drive effects, which produce effectual claims that
feed back as epistemic input.


## 3. The Core Operation: ⊕

### 3.1 Belief State

A **belief state** is a function  b: F → V_f^⊥  that maps each field to either
a value or ⊥ (no information). It represents the agent's total accumulated
knowledge at a point in time.

### 3.2 Per-Field Join

Each field f comes equipped with a join-semilattice (V_f^⊥, ⊔_f) where ⊥ is
the bottom element. The join ⊔_f defines how two values for the same field are
reconciled.

The only algebraic requirement on ⊔_f is that it forms a join-semilattice:

    v ⊔_f v             = v                 (idempotent)
    v ⊔_f v'            = v' ⊔_f v          (commutative)
    (v ⊔_f v') ⊔_f v''  = v ⊔_f (v' ⊔_f v'') (associative)
    ⊥ ⊔_f v             = v                 (identity)

When multiple fields are semantically coupled (e.g., a measured value and its
confidence, or a position and its error bound), care must be taken to ensure
their ⊔_f strategies are coherent. If `location` uses last-writer-wins and
`location_confidence` uses max, a merge may produce an inconsistent pair — a
new position paired with an old confidence. The recommended approach is to
bundle coupled fields into a single composite field with a joint value domain
and a single ⊔_f that maintains their invariant relationship. For example,
(location, confidence) can be modeled as a single field with value domain
Pos × ℝ and a merge that keeps the pair with the highest timestamp intact.

### 3.3 Definition of ⊕

The **merge operator** ⊕ combines two belief states field-wise:

    (b₁ ⊕ b₂)(f)  =  b₁(f) ⊔_f b₂(f)     for all f ∈ F

A single new claim c is incorporated by lifting it to a belief state and
merging:  b' = b ⊕ lift(c),  where lift(c)(f) = c.value if c.field = f,
and ⊥ otherwise.

### 3.4 Properties (inherited from per-field joins)

    b ⊕ b         = b                       (idempotent)
    b₁ ⊕ b₂       = b₂ ⊕ b₁                (commutative)
    (b₁ ⊕ b₂) ⊕ b₃ = b₁ ⊕ (b₂ ⊕ b₃)       (associative)
    b ⊕ ⊥         = b                       (identity)
    b ⊑ b ⊕ b'                              (increasing)

The last property — that merging never loses information — is the single
fact from which LIPAS's monotonicity guarantees follow.

Associativity and commutativity together imply that ⊕ over a set of claims
is order-independent: any permutation of the same claims produces the same
result. Consequently, expressions like b₀ ⊕ c₁ ⊕ c₂ ⊕ ... ⊕ c_t need no
parenthesization.

### 3.5 Per-Field Strategy Examples

Different fields call for different joins. The choice of ⊔_f is a design
decision, subject only to the semilattice laws.

  - Set union:   V_f = P(X),  v₁ ⊔ v₂ = v₁ ∪ v₂
    Use: known obstacles, recorded failures, observed outcome types.

  - Max:         V_f = (ℝ, ≤), v₁ ⊔ v₂ = max(v₁, v₂)
    Use: priority levels, high-water marks, worst-case estimates.

  - Logical OR:  V_f = {⊥, ⊤}, v₁ ⊔ v₂ = v₁ ∨ v₂
    Use: "has event X ever been observed?"

  - LWW:         V_f = Val × Timestamp, (v₁,t₁) ⊔ (v₂,t₂) = argmax_t
    Use: "best current estimate" of a changing quantity.
    LWW requires a total order on timestamps. Ties must be broken by a
    deterministic rule (e.g., lexicographic comparison of source IDs) to
    ensure ⊔_f is well-defined.

  - Counter:     V_f = ℕ, v₁ ⊔ v₂ = max(v₁, v₂)
    Use: failure counts, observation counts in a single-agent setting.
    For multi-agent systems where multiple sources independently increment
    a counter, a GCounter (vector of per-source counts, merged by
    component-wise max, summed for the total) is the appropriate choice.
    In the single-agent case, plain max suffices.

Strategies that are not join-semilattice operations (e.g., arithmetic mean)
must be lifted into representations where the join is well-defined
(e.g., (sum, count) pairs with component-wise max).

### 3.6 Computational Cost

In principle, ⊕ is defined over all fields in F. In practice, ⊕ need not be
computed eagerly over the entire field space. A sparse representation — storing
only fields with non-⊥ values — allows ⊕ to operate in time proportional to
the number of defined fields in each operand, not the size of F. Since each
claim touches exactly one field, incorporating a single claim via b ⊕ lift(c)
requires exactly one ⊔_f computation. Batch merges of two belief states take
time proportional to the smaller operand's defined field count.


## 4. Recovering LIPAS from ⊕

### 4.1 Belief Space

    LIPAS (B, ⊑, ⊔)  =  the codomain of ⊕

  - B is the set of reachable belief states.
  - ⊑ is the product order induced by per-field semilattice orders.
  - ⊔ is ⊕.

The belief state at time t is the fold:

    b_t = b_0 ⊕ c_1 ⊕ c_2 ⊕ ... ⊕ c_t

where c_i ranges over all claims received up to time t (from perception,
integration, and learning). By associativity and commutativity of ⊕, this
fold is order-independent: the final belief state depends on the set of
claims, not the order in which they arrived.

### 4.2 Commitment Structure

    LIPAS (D, ≤, type)  =  conative claims under priority arbitration

Two levels of structure must be distinguished:

  - The **accumulated commitments** — the set of all conative claims ever
    generated — grow monotonically via ⊕ (set union over F_conative fields).
    This is the epistemic record: the agent never forgets that it once held
    an intention.

  - The **currently active commitments** — those eligible for arbitration at
    time t — may shrink. A goal may be achieved, abandoned, or superseded.
    This is deliberate: commitment revision is not a failure of monotonicity
    but a feature of the conative layer. The accumulated record still contains
    the retired commitment; only its active status changes.

In operational terms:

  - D at time t is the set of currently active conative claims.
  - ≤ is the total order induced by the priority field
    (using max as its ⊔_f ensures priorities are comparable).
  - type is the type tag of each conative claim.

Deliberation (δ) is the generation of new conative claims from the current
belief state. Arbitration selects the highest-priority active conative claim
for execution.

δ is not required to be monotonic in b. Generating intentions from beliefs
is a strategic act, not a purely accumulative one. This is the principal
point where the agent's design — its goals, plans, heuristics — enters
the system. ⊕ provides the epistemic substrate on which δ operates, but
does not determine δ's output. This also provides a natural interface for
future commitment retraction mechanisms: they operate on the active set,
not on the accumulated record.

### 4.3 Effect Space

    LIPAS (E = E_int ∪ E_ext)  =  claim generators, partitioned by world impact

An effect e is an action that, when executed, produces new claims:

  - An E_int effect produces only epistemic or conative claims
    (reasoning results, plan revisions). The world is unchanged.

  - An E_ext effect may change the world and produces effectual claims
    (action records, outcome observations).

The integration morphism μ(b, e, o) is:

    μ(b, e, o)  =  b ⊕ lift(claim(e, o))

where claim(e, o) is the effectual claim encoding "effect e was executed
and outcome o was observed." This is a direct application of ⊕.

### 4.4 Morphisms as ⊕ Applications

Each LIPAS morphism that updates belief is an instance of ⊕:

  - Perception κ(w, b): the environment generates epistemic claims;
    κ(w, b) = b ⊕ lift(perceive(w)).
    Axiom P2 (retention) follows from b ⊑ b ⊕ anything.

  - Integration μ(b, e, o): the outcome generates effectual claims;
    μ(b, e, o) = b ⊕ lift(claim(e, o)).
    Axiom I1 (monotonicity) follows from b ⊑ b ⊕ anything.
    Axiom I2 (recording) follows from the effectual claim carrying (e, o).

The non-⊕ morphisms are:

  - Deliberation δ: B → P(D)        — strategic, not determined by ⊕.
  - Action α: D × B → E             — deterministic selection, not ⊕.
  - World transition φ: E × W → W   — external physics, not ⊕.
  - Outcome observation o: E×W×W → O — external, not ⊕.

This division is precise: **every belief-updating step is ⊕;
every non-belief-updating step is not.**


## 5. Recovering Theorems

### 5.1 Theorem 1 (Belief Monotonicity)

LIPAS proves:  ∀t: b_t ⊑ b_{t+1},  using axioms P2 and I1.

In the claim algebra, this is immediate. Each cycle produces claims from
(potentially) three sources:

    b_{t+1} = b_t ⊕ perception_claims ⊕ integration_claims ⊕ learning_claims

Each ⊕ is increasing (b ⊑ b ⊕ c for any c), and increasing operations
compose, so b_t ⊑ b_{t+1}. Furthermore, commutativity and associativity
of ⊕ imply that the order in which these three batches are merged is
irrelevant — the result is the same regardless.

Axioms P2 and I1 are not independent assumptions; they are both instances
of the single algebraic fact that semilattice join is increasing.

### 5.2 Theorem 2 (Eventual Expectation)

Under L1–L3, repeated failures of the same (type, outcome) pair eventually
become expected.

In claim terms: each failure produces an effectual claim that increments
the failure counter for (type, outcome). In the single-agent setting, the
counter field uses max as its ⊔_f, so the count only increases. Once it
reaches K, L3 forces the outcome into the expectation set. The monotonicity
of the counter is a consequence of ⊕; the threshold K is a design parameter.

### 5.3 Theorem 3 (Internal Safety)

E_int effects preserve world state. This is a property of the effect-world
interface (axioms W1, W2), not of ⊕. The claim algebra does not govern
world physics; it governs information flow within the agent.


## 6. The Semantic View: Monotone Fold

The claim algebra is the operational foundation of LIPAS — it says what
the system does at each step. There is a complementary semantic description:

    b_final = fold(step, b_0, claim_stream)

where step(b, c) = b ⊕ lift(c), and the fold is monotone because ⊕ is
increasing.

This "monotone fold" view connects LIPAS to lattice-theoretic convergence
results, but with important caveats about applicability. The space of
reachable belief states under ⊕ is a join-semilattice with bottom (the
initial belief b_0), but it is not in general a complete lattice — it may
lack a top element or arbitrary meets. Tarski's fixed-point theorem, which
requires a complete lattice, does not apply directly.

Convergence depends on the structure of the per-field lattices and the
claim stream:

  - If every per-field lattice V_f has finite height (i.e., all ascending
    chains are finite), then the product lattice also has this property,
    and the fold must reach a fixed point after finitely many claims.

  - If the claim stream is eventually quiescent (finitely many distinct
    claims are ever produced), convergence follows from idempotency:
    repeated claims do not change the state.

  - For infinite-height lattices with unbounded claim streams, convergence
    is not guaranteed by ⊕ alone. It depends on application-specific
    properties of the claim-generating process.

Informally: the agent "converges toward" a state where no new claim changes
the belief — i.e., where the agent has learned everything its environment
and actions can teach it. But this convergence is conditional on structural
properties, not automatic.

This is a useful perspective for reasoning about convergence and eventual
competence. But it is a semantic characterization, not an operational
definition. The distinction matters:

    "The agent computes a fixed point"  describes WHAT it converges to.
    "The agent merges claims via ⊕"     describes HOW it gets there.

A calculus needs the latter.


## 7. What ⊕ Does Not Determine

The claim algebra is the skeleton of LIPAS, not the whole body.
The following require specification beyond ⊕:

  1. **Deliberation strategy** — how epistemic claims generate conative
     claims. This is the agent's intelligence: its goals, planning
     algorithms, heuristics. ⊕ provides the inputs and accumulates
     the outputs, but the mapping itself is exogenous.

  2. **Priority assignment** — how conative claims receive their priority
     values. The total order ≤ is maintained by ⊕, but initial values
     come from the agent's design or its deliberation strategy.

  3. **World physics** — how effects change world state (φ). This is
     external to the agent entirely.

  4. **Resolution design** — which semilattice to use for each field.
     This is a design decision with real consequences: choosing set-union
     vs. LWW for a location field produces very different behavior.

  5. **Belief revision** — LIPAS v0.1 accumulates claims monotonically.
     This is a deliberate design choice that ensures certain safety
     properties (no information loss, deterministic replay). Non-monotonic
     revision — retracting or weakening claims in response to contradictory
     evidence — requires extending ⊕ or supplementing it with a separate
     contraction operator. This extension is orthogonal to the current
     algebra and is deferred to future work.


## 8. Relationship to Known Structures

⊕ is not unprecedented. Its closest relatives are:

  - **CRDTs** (Conflict-free Replicated Data Types): CRDTs merge replicated
    state via a per-type join, ensuring eventual consistency without
    coordination. ⊕ generalizes this by operating over a heterogeneous
    product where each field may use a different CRDT-like merge strategy
    within a single structure.

  - **Datalog / CALM**: Datalog accumulates facts monotonically until a
    fixed point. CALM (Consistency As Logical Monotonicity) identifies
    monotonic operations as coordination-free. ⊕ shares the monotonic
    accumulation pattern but extends it beyond pure fact accumulation
    to include intentions (conative claims) and action records (effectual
    claims), with priority arbitration over the conative layer.

  - **Event Sourcing**: Event Sourcing also follows an "append-only log +
    fold" pattern, but it typically lacks semilattice structure — the fold
    function is order-sensitive, so replaying events in a different order
    may produce a different state. The commutativity of ⊕ is a strictly
    stronger property: claim order does not matter. This makes ⊕-based
    systems easier to reason about in distributed or asynchronous settings,
    where message ordering is unreliable.

  - **Bayesian update**: posterior ∝ prior × likelihood. Not a semilattice
    (not idempotent: updating on the same evidence twice changes the
    posterior). Bayesian reasoning can be embedded within ⊕ by designing
    appropriate field representations, but ⊕ is structurally different.

What distinguishes ⊕ from all of the above is the combination of:

  (a) Heterogeneous per-field merge strategies in a single product algebra.
  (b) Three claim kinds (epistemic / conative / effectual) in the same space,
      with different downstream treatment (accumulation / arbitration / recording).
  (c) A closed loop: effectual claims feed back as epistemic input to the
      next cycle, driving learning.

No existing algebraic framework combines all three.


## 9. Summary

  The core thesis:

    Everything in LIPAS is a claim.
    Claims merge via ⊕.
    ⊕ is a join on a field-indexed product semilattice.
    Monotonicity, recording, and convergence are consequences — not axioms.

  The one thing ⊕ does not give you is deliberation: the strategic,
  creative, non-monotonic act of deciding what to want. That is where
  the agent's intelligence lives, and it is — correctly — outside the
  algebra.

  This algebraic foundation enables formal verification of LIPAS
  implementations: any system that correctly implements ⊕ and respects
  the claim-kind discipline automatically inherits the monotonicity,
  recording, and convergence properties without additional proof
  obligations. The axioms become theorems; the proof burden shifts from
  per-implementation verification to a one-time algebraic argument.
