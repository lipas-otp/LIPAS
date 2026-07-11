# The three rows, and why just three

> **Conceptual foundation, not a public API reference.** Row ownership and
> supported namespaces evolve with the runtime; see the current row modules
> and [`docs/execution-model.md`](../docs/execution-model.md) for executable
> semantics.

## 1. What the calculus leaves unanswered

The calculus gives you a single operation — a join on a field-indexed product semilattice — with three algebraic properties: idempotent, commutative, associative. From those properties you get replay safety, ordering independence, and monotone convergence. That's a lot for one symbol to buy.

But the calculus is silent on three questions that every real agent must answer:

1. **When should a fold be refused?** The merge operation has no notion of "no, you can't do that." It accepts everything.
2. **Do different claims have different operational consequences?** The `tag` field is a label, not a rule. `merge` treats a "heartbeat" claim and a "spend \$500" claim identically.
3. **What does it mean to read the state?** `store.merged` is a single mashed-up claim. Different consumers need different cross-sections.

These gaps are not flaws in the calculus. They are where **operational reality** enters a purely algebraic substrate. The three rows are the minimum partition of that reality.

---

## 2. The three root causes, and why each forces a specific kind of structure

Each root cause breaks a naive assumption about information and forces a specific *kind* of structure above the merge.

### 2.1 Non-determinism + opacity → History row

Same input, different output: the world isn't a function. And even when you see an output, you can't see the reasoning that produced it. Together these mean that forward simulation cannot reconstruct the past, and the outcome alone cannot explain itself.

The only remedy is to **record** — every claim, every step, every attribution. The contract is event-sourcing plus reasoning trace.

This is the *cheapest* row to add: the calculus already gives you everything. Append-only log? That's the claim sequence. Attribution? That's `claim.source` and `claim.claim_id`. Monotone accumulation? That's every semilattice strategy you've already written. The History row introduces **no new invariants**; it just names the projection that exposes "what happened, in order, with provenance."

Necessity: without History, an agent cannot learn (learning compares now to then), cannot debug (the outcome without reasoning is an oracle), and cannot replay (testing becomes impossible).

### 2.2 Costly + side effects → Capability row

Information is free; action is not. Every tool call burns tokens, every HTTP POST touches the outside world, every shell command is irreversible. You cannot run indefinitely, and you cannot undo what reached the world.

The remedy has two parts:
- **Accumulation** of spending (monotone — the total only rises)
- **Gating** before admission (non-monotone — some folds must be refused)

Here the calculus gives you only half. The accumulation half is classic CRDT territory — a PN-counter style representation, with `claim_id` deduplication, is exactly how you get "linear consumption over a commutative substrate." But the gating half — "refuse this fold because it would exhaust the budget" — is fundamentally outside the merge. A merge that sometimes says no is no longer a merge.

This is why the Capability row is the row with the most interesting structure: it is the **only row where invariants do real work**. The row checks before the fold, and lets the calculus run monotonically after.

Necessity: without Capability, an agent has no exhaustion signal (cannot terminate gracefully), no contract (cannot promise "I'll spend at most X"), and no sandboxing (cannot run untrusted work with bounded cost).

### 2.3 Principal-agent → Identity row

When one entity acts on behalf of another, two information asymmetries appear:

- **Adverse selection** (before the act): you can't distinguish a competent agent from an incompetent one, or a faithful agent from a malicious one.
- **Moral hazard** (after the act): you can't fully verify what they actually did.

Economics has known for fifty years that these require two separate mechanisms:
- Adverse selection is answered by **capability credentials** — only those who already hold the right may act. Filter the principal pool.
- Moral hazard is answered by **audit logs** — every action is attributable, immutably, after the fact.

The calculus gives you the audit half for free: `claim_id` is unforgeable once emitted, `source` ties every field to an origin, and monotonicity guarantees the log never shrinks. The credential half requires invariants again — refusing folds that come from principals lacking the necessary capability.

Delegation and revocation deserve special mention. Delegation is a positive claim ("Alice grants Bob her read capability"). Revocation is its complement ("that grant is withdrawn"). Both are monotone when expressed correctly: you never un-emit a delegation claim; you emit a revocation claim that shadows it. The Identity row's projection filters accordingly. The calculus never regresses.

Necessity: without Identity, an agent cannot delegate (no way to say "this sub-agent acts for me"), cannot revoke (cannot withdraw trust once given), and cannot authorize (cannot gate actions by who's asking).

---

## 3. Why three — not two, not four

The sufficiency argument comes from asking: what relationships does an agent *have*?

An agent stands in exactly three relationships:

- **To its past** (the things that have happened to it) — History
- **To its resources** (the things it can still do) — Capability
- **To other agents** (the minds it must trust or be trusted by) — Identity

These exhaust the space. "The agent's relationship to itself" is not a fourth axis — the self is *constituted by* its history, *bounded by* its capacities, and *individuated by* its identity. There is no remainder.

A different lens gives the same answer. Any operational invariant an agent enforces is either:

- **Informational** — "has X been observed?" → History's territory
- **Quantitative** — "is budget Y still open?" → Capability's territory
- **Relational** — "may Z perform this?" → Identity's territory

If you try to add a fourth row, you will find it is either a subcategory of one of these (e.g., "attention" is a kind of capacity), a re-expression of something already present (e.g., "trust scores" live in Identity), or a problem the calculus already solves (e.g., "consistency" is what merge gives you). The three categories are jointly exhaustive and mutually orthogonal.

---

## 4. How each row sits on the same calculus

This is the part worth underlining. The rows **do not extend the algebra**. They add exactly three things on top of an unchanged merge:

| Addition | History | Capability | Identity |
|---|---|---|---|
| **Namespace** (tags owned) | observation, fact, outcome, task, reflection | resource_spent, quota_used, rate_event | delegation, revocation, trust_update |
| **Invariant** (fold-time gate) | none (pure projection) | budget overrun → refuse | unknown principal / missing target → refuse |
| **Projection** (read view) | domain facts + history + fail counts | spent/remaining per bucket | active delegations + trust scores |

The calculus chassis is identical in all three cases. What differs is **where on the chassis operational reality demands an extra bolt**. History demands nothing, because recording is what monotone fold already does. Capability demands gating, because resources are real. Identity demands gating, because trust is asymmetric.

This is what I meant by "parsimony should point to the communication layer, not swallow operational differences." The merge is the communication layer — one operation, one Claim, one `claim_id`. The rows are where the merge meets the world.

---

## 5. The bridge to the 8-step loop

Each row activates in different phases of the cycle, and this is how you can tell the design is right:

- **Phase 1 (Perceive)** — observations are folded into History.
- **Phase 2 (Deliberate)** — deliberation reads from History's projection (what do we know?) and Identity's projection (who am I? what may I do?).
- **Phase 5 (Act) / Phase 6 (Dispatch)** — the Capability row gates here: *before* dispatch, check the budget invariant; *after* dispatch, fold a `resource_spent` claim.
- **Phase 7 (Interpret) / Phase 8 (Learn)** — all three rows update via pure merge.

The rows do not replace the loop; they localize its concerns along the three operational axes. If you ever want to know where to put a new invariant, ask which root cause it answers: informational (History), quantitative (Capability), or relational (Identity).
