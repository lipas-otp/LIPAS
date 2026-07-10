# Changelog

## [Unreleased]

### Added

- OpenAI Responses adapter, caller-facing normalized streaming, durable
  operation journal, and named-agent mailbox orchestration.
- Beta release gate and regression coverage for the new public surfaces.

### Changed

- Migrated stale tests to the explicit side-effect and `RetryOutcome` APIs.
- Fixed PEP 621 package metadata so wheels build successfully with Hatchling.

## [0.2.0] — 2026-05-20

Two themes: aligned the store's contract with database conventions
(trust the substrate; stop re-checking invariants in callers), and
added a thin testing layer that makes strategies cheaper to write
correctly. No breaking changes since 0.1.0a1.

### Added
- **Initial SQLite-backed `ClaimStore`** — serializable persistence
  for claim logs. In-memory store remains the default.
- **`lipas.testing.deterministic_fold`** — context manager that traps
  `time.time` and `os.environ` access inside fold strategies and
  raises `StrategyContractViolation`. Drop it into a test to prove a
  strategy is pure; no boilerplate, no framework buy-in.

### Changed
- Store contract realigned with database-style guarantees: callers
  trust the substrate's consistency rather than re-asserting it.
  Reduces ceremony in user-written strategies and agents.

---

## [0.1.0a1] — 2026-05-01 · First alpha

This is the first public release of lipas. It is an alpha: the core
algebra and harness contract are stable, but adapters are limited to
Ollama and APIs may evolve before 0.1.0 final.

### What this release is

lipas is an LLM agent runtime built around three guarantees that no
current framework provides together:

- **Side-effect classification is enforced, not optional.** Every tool
  declares `PURE` / `READ_ONLY` / `IDEMPOTENT_WRITE` / `EXTERNAL_WRITE`
  at registration. The harness uses this to gate retries, enforce guards,
  and determine replay safety.

- **Resource budgets are pre-flight constraints, not post-hoc
  observations.** A budget check runs before every LLM and tool call.
  If the estimated cost would exceed your declared limit, the call is
  not issued — a typed rejection claim is folded instead. You cannot
  accidentally overspend a budget that lipas knows about.

- **LLM calls are deterministically replayable.** Record a run, replay
  it, and the adapter is short-circuited: no network, no tokens, no
  variance. Use this for debugging, testing against tighter budgets, or
  exact post-mortem reproduction of a failure.

### What is included

**Claim Calculus (Layer 0)**
The algebraic foundation. `Claim`, `merge` (⊕_b), `StrategyRegistry`,
`BeliefContext`, `BOTTOM`. All built-in merge strategies
(`strategy_last_write`, `strategy_counter_max`, `strategy_append`,
`strategy_expectations_merge`). The full system is a monotone
join-semilattice: re-delivery of any claim is always a no-op.

**ClaimStore (Layer 0.5)**
Append-only claim log with incremental materialized join and
tag-indexed filter. Single-writer. No external dependencies.

**Rows (Layer 1)**
- `HistoryRow` — epistemic projection (observations, facts, outcomes,
  reflections). No hard gates; semilattice semantics absorb duplicates.
- `CapabilityRow` — resource accounting with pre-flight and fold-time
  budget gates. Per-bucket spend tracking with claim-id deduplication
  (replay-safe accounting).
- `IdentityRow` — trust scores (Beta-distributed), delegation chains,
  revocations.
- `EffectRow` — effect graph projection. Provides `EffectView` with
  lineage walk, `llm_nodes()`, `tool_nodes()`, orphan and rejection
  detection.
- `RowSet` — composition container. Invariant checking on every fold.

**Tool layer**
- `@tool(side_effect=...)` decorator with JSON Schema extraction from
  type hints. `side_effect` is required; registration raises if absent.
- `ToolRegistry` with duplicate detection.
- `ToolHarness` — full pre-flight pipeline (budget → capability →
  guard → intent → execute → result → spend). Produces structured
  `effect_intent` / `effect_result` / `resource_spent` claims.
  Overruns routed to `budget_overrun` tag, never silently dropped.

**LLM layer**
- `LLMHarness` — same pre-flight shape as `ToolHarness`. Records
  `effect_intent` and `effect_result` for every LLM call.
- `ReplayCursor` — short-circuits the harness on replay; no network
  call, no new claims folded.
- `ReplayingAdapter` — drives the harness with recorded replies as a
  fake adapter; a fresh audit trail is folded into a new store.
  Useful for re-running a session against changed budgets or guards.
- `strict_match=True` (default) — rejects replays where `model` or
  `system` do not match the recorded intent.
- `OllamaAdapter` — local inference, no API key required.

**Agent**
- `ReActAgent` — Reason→Act→Observe loop. Each iteration triple
  (thought, action, observation) stored as claims in `HistoryRow`.
  Terminates on `end_turn`, `budget_exhausted`, or `max_iterations`.
- `AgentState`, `FinalResult` — typed input/output envelope.

**Guards**
- `Guard` protocol — `allow` / `deny` with structured reason and
  detail dict. Uniform across LLM and tool calls.
- Guards run in the pre-flight pipeline after budget checks. First
  deny wins. Reason and detail are folded as a typed claim.

**Examples** (`examples/`)
Six runnable examples covering: happy path, budget rejection, guard
rejection, single-call replay, ReAct end-to-end, and full ReAct
replay with three run variants (normal, strict-match rejection,
loose-match pass-through).

- `01_single_call.py` — happy path with audit trail
- `02_budget.py` — pre-flight budget rejection
- `03_guard.py` — guard rejection
- `04_replay.py` — single-call LLM replay
- `05_react_calculator.py` — ReAct + ToolHarness end-to-end
- `06_react_replay.py` — full ReAct replay with strict-match negative test

### Known limits

**Tool replay is not yet safe for non-PURE tools.** In v0.1, the
`ToolHarness` has no replay path. During a replay run, tools
re-execute against the live world. `EXTERNAL_WRITE` tools will
re-fire their side-effects (re-send email, re-charge card). Only
use replay on runs whose tools are `PURE` or `READ_ONLY` until
`ToolReplayer` arrives in Phase 4.

**Crash-safety window.** The gap between tool execution and the
`effect_result` fold is documented but not closed. A process crash
in this window produces an `effect_intent` with no corresponding
`effect_result`. Phase 4's write-ahead log closes this.

**In-memory store only.** `ClaimStore` does not persist across
process restarts.

**No streaming to caller.** The harness assembles streamed LLM
responses internally. The caller receives a completed `Reply`.

**Single agent only.** Multi-agent orchestration is out of scope
until the supervision tree (Phase 4) is stable.

**Ollama only.** OpenAI and Anthropic adapters arrive in Phase 5
(v0.2 target).

### Roadmap

| Phase | Contents | Status |
|---|---|---|
| 0 | Claim calculus, store | ✅ Done |
| 1 | Rows, basic types, tool harness | ✅ Done |
| 2 | Side-effect algebra, LLM harness, guards, LLM replay | ✅ Done |
| 3 | ReAct agent, ToolHarness, EffectRow, Ollama adapter | ✅ Done |
| 4 | Supervision tree, policy DSL, ToolReplayer, write-ahead log | 🔲 Next |
| 5 | OpenAI / Anthropic adapters, CLI trace viewer, OTel export | 🔲 Planned |
| 6 | Multi-agent, persistent store, compensation | 🔲 Future |

### Installation

```bash
pip install lipas==0.1.0a1            # core only (no adapter)
pip install "lipas[ollama]==0.1.0a1"  # + OllamaAdapter
```

Requires Python ≥ 3.10.
