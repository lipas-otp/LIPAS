# Changelog

## [0.20.0] — 2026-07-20 · Local Task Product Alpha

### Added

- The first local workspace task workbench: durable `lipas task` commands,
  Workspace/Approval/Artifact/Verification/Report product models, bounded
  filesystem/Shell/Git capabilities, persistent approvals, and evidence-based
  task reports.
- Automatic durable-run lease heartbeats and typed model/tool phase timeouts.
- Safe multi-tool concurrency: contiguous independent `pure`/`read_only` calls
  may run in parallel and checkpoint as one ordered batch. Writes and calls
  with hard budgets, guards, replay, or custom execution hooks remain serial;
  completed batch Effects restore without re-execution after a checkpoint
  interruption.
- Operator queries for listing Tasks, Runs, and Interrupts.
- A framework-neutral `ActionGateway` plus LangGraph node/tool adapters, a
  standard MCP stdio server for Hermes and other hosts, and an OpenClaw/
  OpenCrew JSON action backend. Stable external request ids recover recorded
  terminal Effects on redelivery. These third-party compatibility adapters are
  explicitly experimental and are not core LIPAS product surfaces.
- A product-ingress `SecretPolicy` that rejects common raw credentials before
  any approval or Effect Claim is persisted and permits opaque `secret://`
  references.
- An allowlisted `EnvironmentSecretResolver` for `secret://env/NAME` values.
  References remain in Effect intents; values are resolved immediately before
  tool execution and redacted from outputs or exceptions before persistence.
- A Bubblewrap command backend for the local workbench with a minimal
  filesystem, writable workspace, cleared environment, and isolated network.
  `auto` and `bwrap` fail closed; `local` is an explicit trusted-code fallback.
- Persistent workbench `RunEvent` history for task/run creation, approvals,
  artifacts, verifications, state transitions, and reports, exposed as JSONL
  through `lipas task events`.
- A persistent local `TaskDispatcher` and `lipas task submit/worker` flow with
  FIFO discovery, atomic lease ownership, bounded Task concurrency, expired
  lease reclaim, cancellation closeout, approval slot release, deferred resume,
  a durable CLI approval inbox, and dispatch events. Each Run now receives an
  isolated Claim/Effect session; checkpointed legacy Runs retain their
  original session.
- Per-Run staged file `ChangeSet` delivery for CLI Tasks. Agents modify and
  verify a bounded snapshot while the original workspace stays unchanged;
  `lipas task diff/apply/discard` provides complete review, explicit delivery,
  baseline drift rejection, per-file atomic replacement, restartable apply,
  persistent delivery events, and report state.
- A provider-free local-task product lesson covering staged writes, durable
  command approval, restart, verification, diff review, and explicit apply.
- A single-source package version exposed through `lipas.__version__` and
  `lipas --version`.

### Changed

- The `0.20.x` line marks LIPAS's transition from a runtime-led project to its
  first independently usable local task product. The runtime remains the
  reliability foundation and advanced embedding surface.

### Fixed

- Core-only installations no longer import the optional Ollama/httpx adapter
  while starting the CLI. Version, help, task, and inspection commands now
  work without provider extras; custom-factory errors also remain reportable.
- Installed console scripts now resolve explicitly requested
  `module:callable` factories from the operator's working directory, so the
  documented local `agent:build_agent` and task-worker factory flows work from
  a wheel installation as well as from a source checkout.

### Safety

- Workspace paths are contained after symlink resolution; direct `.git`
  internals and likely secret files are denied. Command execution uses an
  allowlist, no shell expansion, a workspace cwd, a scrubbed environment,
  bounded time/output, and approval before execution.
- Common secret assignments, bearer tokens, provider tokens, and private keys
  are redacted from readable text, command evidence, Git diff, and reports.
- Synchronous Python tools run outside the event loop so lease heartbeats keep
  advancing. Cancellation leaves an orphan intent because a thread cannot be
  proven stopped; it no longer records a false terminal failure.

### Verified

- 547 tests pass, including the full CLI flow from staged write through
  verification approval and report delivery.
- Ruff passes on every changed source/test file and mypy passes the complete
  public package.

## [0.10.0] — 2026-07-18

### Added

- A durable ReAct phase runner over `ExecutionStore`, including run leases,
  versioned checkpoints, approval interruption/resume, cooperative
  cancellation, terminal-result restoration, and orphan-safe Effect recovery.
- A provider-free durable-execution lesson covering approval suspension and
  resume; the linear tutorial now includes the same recovery boundary.
- A versioned execution database and Claim-shaped transaction outbox for
  Task, Run, lease, checkpoint, Interrupt, cancellation, and settlement
  transitions. `repair_audit()` mirrors committed events with stable Claim ids.
- A public `ExecutionStore.cancel_task()` transition, making
  `TaskState.CANCELLED` reachable while cooperatively stopping an active Run.

### Changed

- Documentation now has one onboarding path: README for the copy-and-run
  start, the tutorial for progressive learning, the execution model for exact
  semantics, and the roadmap for unshipped product work. The duplicate Quick
  start pages and capstone tables were consolidated into those owners.
- The stable durable surface is `Agent.run_durable()`, `resume_durable()`,
  `ExecutionStore`, `ApprovalPolicy`, and the execution value/error types.
  The lower-level `DurableReActRunner` remains available from `lipas.durable`
  but is no longer presented as a top-level application API.
- Claim documentation now distinguishes the mutable event-preparation object
  from the immutable snapshot owned by a Claim store, and defines which SQLite
  store is authoritative for each kind of mutable control state.

### Fixed

- Provider tool-call correlation ids are now separate from internal Effect
  ids, so real Anthropic/OpenAI ids and repeated ids across runs cannot corrupt
  the Effect tape.
- OpenAI Responses, Anthropic, and Ollama now translate multi-turn typed tool
  calls/results into their provider wire shapes without silently flattening or
  misreporting terminal provider states.
- Tool replay consumes repeated identical recordings in source order and
  preserves recorded error observations.
- Retry usage and priced `cost_usd` are accumulated across every billed
  attempt. Deterministic spend claims let recovery finish partially recorded
  accounting without double charging the tape.
- Claim stores now snapshot admitted values and returned read models, so caller
  mutation cannot rewrite an append-only in-memory tape or diverge it from its
  SQLite source.
- Team handoff claims the message it just sent instead of accidentally leasing
  an older pending message, and durable runs can reclaim expired cancellation
  requests and restore already-settled results.
- OperationJournal and Team now commit Claim-shaped audit events to a local
  outbox with their authoritative SQLite transitions and repair the separate
  audit tape idempotently after a crash window.
- Durable checkpoints bind the stable Claim-store identity, preventing a
  resume against the wrong Effect tape from silently reissuing live work.
- Durable supervision now uses stable run/iteration-scoped claim identities;
  recovery repairs a crash between recommendation emission and checkpointing
  without duplicating supervisor or `goal_blocked` claims.
- Execution and mailbox leases reject boolean, infinite, and NaN durations or
  timestamps instead of creating permanently unrecoverable work.
- Effect rows validate aggregate retry usage and persisted tool spend before
  malformed recovery data can enter the tape. Terminal cancellation restores
  consistently, including Claim-store identity checks.
- OpenAI cached input is normalized into disjoint pricing buckets, and
  non-token incomplete responses are surfaced as typed provider failures.
- Malformed provider tool arguments now fail closed as terminal adapter errors
  across OpenAI, Anthropic, and Ollama instead of reaching defaulted tools.
- Model `max_tokens` truncation and malformed tool-use stops are no longer
  reported as natural Agent completion.
- The wheel now includes its declared PEP 561 `py.typed` marker.
- Execution databases fail closed on an incompatible schema version instead of
  interpreting unknown durable state.
- OperationJournal and Team mailbox databases now carry the same fail-closed
  schema compatibility gate, including safe adoption of legacy version-1
  databases. Mailbox also supports the same context-managed cleanup pattern as
  the other durable stores.
- Adapter request-translation failures now become recorded terminal error
  replies instead of escaping after an Effect intent; OpenAI Responses rejects
  unsupported `stop_sequences` explicitly rather than silently dropping them.
- Tool estimates cannot charge undeclared resource buckets, and public guard,
  retry-policy, retry-outcome, Agent-state, and final-result values reject
  structurally invalid states before they reach durable records.

### Verified

- 503 tests pass across execution, durable recovery, replay, adapters, budgets,
  Team, CLI, examples, and packaging-facing contracts.
- A real subprocess `SIGKILL` between a completed write Effect and its next
  checkpoint restores the recorded result after restart without repeating the
  write.
- The wheel builds successfully and imports its public execution and adapter
  surfaces from an isolated target installation.

## [0.9.8] — 2026-07-12

### Added

- A numbered, progressive ten-lesson example course: four practical
  single-Agent scenarios, then budget, strict replay, supervision, Team, and
  external-operation patterns. The older low-level, overlapping examples were
  removed so newcomers have one unambiguous learning path.
- Four portable, ready-to-copy `SKILL.md` templates: `research-brief`,
  `support-triage`, `daily-brief`, and `safe-external-actions`.
- Regression coverage that builds the practical examples without a provider,
  validates the supplied Skills, and checks path-based supervised sessions.

### Changed

- A Skill directory can now be passed directly as
  `Agent(..., skills="skills/name")`; advanced callers may still use a
  `SkillRegistry` explicitly.
- SQLite-backed sessions, mailboxes, and operation journals create missing
  parent directories and accept normal `pathlib.Path` values.
- A durable Agent now carries its declared budgets into its standard SQLite
  session. Previously, only in-memory Agents enforced those budgets.
- OperationJournal terminal outcomes are now immutable, so a stale
  reconciliation cannot rewrite a known success or failure. Mailbox
  acknowledgements now require an active, unexpired lease.
- A provider return that cannot be journaled (for example, an
  unserializable result) now becomes `uncertain` rather than looking like an
  unsubmitted operation.
- Provider-neutral request, reply, resource-estimate, and pricing values now
  reject malformed model names, stop reasons, token counts, and non-finite
  costs at the adapter boundary rather than letting them weaken budget checks.
- Capability budgets and recorded spend amounts now reject negative, boolean,
  infinite, and NaN values, preserving the meaning of a hard budget gate.
- LLM and tool runtime records now snapshot mutable request, reply, argument,
  and output values at their effect boundary, preventing later user/tool
  mutation from changing an in-memory audit tape or replay input.
- Tools with an invalid or failing `estimate=` function now fail closed before
  execution; the validated estimate is reused for spend recording so a
  non-deterministic estimator cannot bypass a hard budget.
- Tool estimates, guards, intent records, and execution now share one fully
  bound argument mapping, including Python default values.
- Guard and estimate callbacks now receive isolated input snapshots; policy or
  accounting code cannot mutate the request/arguments ultimately admitted to
  a provider or tool.
- The Team, OperationJournal, and Supervisor examples are self-contained
  where possible. The operation-recovery example now uses a fresh stable key
  per run, so it demonstrates uncertainty and reconciliation repeatedly.
- The practical Ollama examples now show an immediate progress line, bound
  their request size to their displayed budget, and print provider failures or
  non-natural termination instead of appearing to finish silently.
- README, Getting started, Execution model, and the examples index now form a
  progressive path from a first Agent to Skills, supervision, handoffs, and
  external-operation recovery.
- Documentation now distinguishes lower-level `LLMHarness.stream(...)` from
  the final-result-oriented `Agent` API, and states that visible stream output
  is not retried. Tool-estimate documentation now matches the fail-closed
  `estimate_invalid` behavior.

### Verified

- 363 tests pass across audit, replay, budget, supervision, mailbox, CLI,
  examples, Skills, and provider-adapter contracts.
- `lipas-0.9.8-py3-none-any.whl` builds successfully.

## [0.9.7] — 2026-07-12

### Changed

- Simplified the shipped runtime around the public `Agent` / `@tool` / `Team`
  model. The remaining modules correspond to real execution boundaries rather
  than compatibility layers, test utilities, or fragmented data shapes.
- Consolidated all provider-neutral adapter values—request, reply, content,
  usage, stream events, estimates, and pricing—in `lipas.adapter.types`; the
  public `lipas.adapter` facade remains the supported import surface.
- Moved repository-only fake adapters and fold-purity checks into `tests/`.
  The published `lipas` package contains no testing namespace.
- Consolidated the public documentation into README, Getting started, and the
  Execution model; removed competing beta/stability, CLI, mental-model, and
  conceptual-note documents.

### Removed

- Legacy adapter submodules (`content`, `request`, `reply`, `usage`,
  `streaming`, `estimate`, and `pricing`), the standalone supervisor projection
  module, and `lipas.testing`. These were intentional pre-1.0 simplifications,
  not compatibility shims.

### Verified

- 319 tests pass across audit, replay, budget, supervision, mailbox, CLI, and
  provider-adapter contracts.

## [0.9.6] — 2026-07-11

### Added

- `lipas` CLI: `init` creates an ordinary Python prototype; `chat` runs an
  Ollama-backed Agent or an explicit Python factory; `trace` and `effects`
  inspect the same durable claim session used by library code.
- The thin CLI onboarding and inspection surface, documented alongside the
  ordinary-Python entry point in the README rather than as a second DSL.
- `Agent.ollama(...)`, a short local-Agent constructor, and explicit string
  side-effect values such as `@tool(side_effect="read_only")`.
- `Agent.ask(...)` and context-manager support, so the first ordinary Python
  script can use `with Agent.ollama(...) as agent: agent.ask("...")` without
  introducing an event loop or manual session cleanup.
- `Team.ask_sync(...)` and context-manager support for the same ordinary
  script style at a durable handoff boundary; async services continue to use
  `await team.ask(...)`.
- Claim-idempotent in-memory and SQLite stores: re-delivery of the same
  logical claim is a no-op; reusing its id for different content raises a
  typed `ClaimIdConflict` instead of diverging between store backends.
- `examples/00_playground.py`, the recommended first runnable example using
  only `Agent.ollama(...)`, one tool, and a durable trace.

### Changed

- `lipas chat` now uses persistent line editing/history through the optional
  `cli` extra (with `readline` fallback) and shows a terminal spinner while an
  Agent is working.
- `lipas chat` now makes local Ollama timeouts explicit, supports `--host` and
  `--timeout`, and avoids implying that a localhost transport error is an
  internet request.
- Interactive chat now performs no implicit local timeout/network retry;
  `--retries` opts into extra attempts without changing library defaults.

- Claim-linked coordination: `Team` now owns a durable audit session, and an
  Agent invoked by a Team handoff records its stable `message_id` as
  `caused_by` on LLM and tool effect intents.
- Claim-linked external operations: `OperationJournal` can fold durable
  prepared/uncertain/succeeded/failed transitions into a supplied claim
  session and associate them with an originating `effect_id`.
- `docs/execution-model.md`, the concise normative description of claims,
  folds, effects, replay, coordination, and the external-effect boundary.
- `lipas.adapter` is now the canonical root-level provider-neutral interchange
  surface (`Request`, `Reply`, content, usage, and stream events).
- `Team`: a small facade that makes ordinary async Python functions and
  Agents durable named team members without introducing a workflow DSL.
- `project_supervisor(store)`: a tag-indexed read model for retry,
  termination, and escalation recommendations.
- Default `Agent(supervisor_policy=...)` wiring, so advisory supervisor
  termination/escalation is evaluated in the ReAct lifecycle.
- `Team.add(name, handler)`, the direct registration API for named members.
- A progressive documentation path centered on the README, Getting started,
  and the execution model.

- Project-facing prose and headings now use the LIPAS brand; Python imports,
  distribution name, module paths, and the `lipas` CLI intentionally remain
  lowercase.
- The public vocabulary is now `Agent` / `Tool` / `Team`; Team is a small
  durable-handoff facade, not a graph or workflow DSL.
- Ollama quickstart and examples now default to `gemma4:12b`; lower-level
  examples retain `LIPAS_OLLAMA_MODEL` as an explicit local override.
- Earlier reliable-core additions are consolidated in this release: `Team`,
  default Agent supervisor wiring, and `project_supervisor(...)`.

- The high-level authoring path is now one short vocabulary: `Agent`,
  `@tool`, and `Team`. Lower-level harnesses remain available for custom
  runtime work but are no longer required reading or the first examples.

### Removed

- The obsolete, overlapping pre-beta APIs (`DeclarativeAgent`, `LLM`,
  `Runtime`, `lipas.types`, deferred supervisor modules, and the unused
  `IdentityRow`) and their stale tests. The canonical API now owns one
  request/reply shape in `lipas.adapter`, one `Agent` entry point, and three
  runtime rows: history, capability, and effect.
- Compatibility aliases for pre-effect terminology (`CallNode`, `call_id`,
  and `TAG_CALL_*` / `F_CALL_ID`). Persisted string values remain readable as
  storage details; new Python code uses effect names exclusively.
- Duplicate serialization types and archived implementation notes that no
  longer matched the runtime.

### Verified

- 319 tests pass across audit, replay, budget, supervision, mailbox, CLI, and
  provider-adapter contracts.

## [0.8.0b1] — 2026-07-11

### Changed

- Operation journal now has explicit `pending` / `uncertain` / `failed` /
  `succeeded` recovery states and refuses accidental re-submission.
- Agent mailbox delivery now uses durable leases, ownership-checked
  acknowledgement, failure release, and expired-lease recovery.
- Public package metadata now declares beta maturity.

### Verified

- 373 tests passed, including restart recovery for operation journals and
  mailboxes.

## [0.5.0b1] — 2026-07-11

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
- **Deterministic fold test helper** — traps nondeterministic inputs inside
  fold strategies and raises `StrategyContractViolation`. It now lives with
  the repository tests rather than the shipped package.

### Changed
- Store contract realigned with database-style guarantees: callers
  trust the substrate's consistency rather than re-asserting it.
  Reduces ceremony in user-written strategies and agents.

---

## [0.1.0a1] — 2026-05-01 · First alpha

This is the first public release of LIPAS. It is an alpha: the core
algebra and harness contract are stable, but adapters are limited to
Ollama and APIs may evolve before 0.1.0 final.

### What this release is

LIPAS is an LLM agent runtime built around three guarantees that no
current framework provides together:

- **Side-effect classification is enforced, not optional.** Every tool
  declares `PURE` / `READ_ONLY` / `IDEMPOTENT_WRITE` / `EXTERNAL_WRITE`
  at registration. The harness uses this to gate retries, enforce guards,
  and determine replay safety.

- **Resource budgets are pre-flight constraints, not post-hoc
  observations.** A budget check runs before every LLM and tool call.
  If the estimated cost would exceed your declared limit, the call is
  not issued — a typed rejection claim is folded instead. You cannot
  accidentally overspend a budget that LIPAS knows about.

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
