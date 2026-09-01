# Changelog

## Unreleased

No changes recorded yet.

## [0.63.0] — 2026-09-01 · Capability-Complete Local-First Refinement

### Added

- Ollama quickstart and examples now default to `phi4-mini`.
- Added a concise architecture guide covering the request path, module
  ownership, authoritative stores, and the deliberate read-only chat versus
  full Workbench capability boundary.
- Added a provider-free workspace capability example combining CSV profiling,
  arithmetic, Markdown conversion, safe archive handling, atomic file writes,
  and scoped lexical knowledge retrieval.
- Added regression coverage for the architecture index, invalid workspace
  argument types, and archive member names that resolve to the extraction root.

### Changed

- Centralized Workbench path, file, digest, evidence, and atomic-write helpers
  so document, data, archive, and file tools share one policy path.
- Kept document parsers, computation, archive, web, and knowledge helpers
  dependency-optional and authority-free; Workbench remains the single place
  that supplies workspace policy, staging, approval, and evidence.
- Corrected the package metadata and bilingual documentation indexes to the
  0.63.0 release line, while retaining 0.60 as a historical milestone.

### Fixed

- Closed ZIP/TAR handles when member validation fails instead of leaking an
  archive descriptor.
- Rejected `.` and empty-component archive members that could target the
  temporary extraction root.
- Restored strict type-checking for optional XLSX conversion branches and
  aligned the Python execution sandbox protocol with the shared sandbox type.

## [0.60.0] — 2026-08-30 · Productionized Local-First Single Workspace

### Added

#### 0.60 productionization pass (local-first single workspace)

- Added idempotent `install_workspace()`/`upgrade_workspace()` helpers, a
  0600 `.installation.json` manifest, atomic permission hardening, and a
  machine-readable `release check` report. The CLI now exposes `install`,
  `upgrade`, `backup`, `restore`, and `release check` commands.
- Added `FileSecretResolver` for atomic local secret rotation and `TLSConfig`
  for TLS 1.2+ Operator/remote Worker endpoints. Non-loopback servers now fail
  closed without authentication and TLS.
- Added bounded `execute_compiled_workflow()` and
  `LIPASRuntime.execute_workflow()`, which run a compiled plan as one durable
  Task/Run with step lifecycle events while preserving the Effect bridge for
  world-changing callbacks.
- Added Local Web `/api/metrics`, `/api/incidents`, and `/api/cost` projections
  for operator dashboards.
- Added a manifest- and SHA-256-verified workspace evidence bundle that
  captures `workspace.db`, per-Run `runs/**` tapes, and installation metadata;
  bundle restore stages both control state and evidence under an exclusive
  lease and rewrites
  installation paths for a new home. SQLite/Run evidence permissions are
  hardened to 0600/0700, and MCP JSON-RPC notifications now correctly carry no
  response id.
- Workflow execution now renews short leases and persists per-step checkpoints;
  an expired owner can reclaim the same Run and replay only unfinished steps.
- Added read-only `WorkspaceStorage.verify_bundle()` and `lipas verify-bundle`
  for offline manifest/hash/SQLite validation. Bundle restore now writes a
  durable pending marker and recovers conservatively after a process crash;
  a mixed old/new workspace is never opened as current.
- Workflow cancellation is now a first-class result (`cancelled`) and the
  Runtime settles the underlying durable Run as `CANCELLED`, including when
  lease heartbeat observes an operator cancellation request.
- Added `ExecutionSoakReport`/`run_execution_soak()` and the `lipas soak` CLI
  for bounded local durability rehearsals with terminal-state invariants.
- Added `ManagedSecretResolver` as an explicit KMS/HSM/secret-manager
  integration boundary, plus certificate fingerprints and live TLS context
  reload for Operator and remote Worker servers (including client trust
  context reload). Managed provider keys can be re-resolved after rotation;
  custom secret namespaces and bounded fallback redaction are supported. These contracts record
  deployment evidence but do not claim custody of external keys or provider
  availability.
- Added explicit `run_provider_workflow(..., live=True)` evidence for one
  durable Agent/Task/Run against a real configured provider, with prompt
  secret rejection, provider/model identity binding, usage aggregation, and
  terminal outcome classification. Added verified `DesignPartnerSignoff`
  artifacts so local fixtures cannot be promoted to external acceptance
  without an operator-supplied partner record.

#### 0.51 · Bounded autonomous workflow compiler

- Added `WorkflowGoal`, `WorkflowConstraint`, and `WorkflowStep` contracts,
  plus `AutonomousWorkflowCompiler`, which deterministically produces an
  inspectable `CompiledWorkflow`/`AgentPlan` from a goal and bounded
  constraints.
- Fixed and adaptive steps share the existing Plan/Handoff boundary;
  adaptive steps are explicitly capped and dependency cycles are rejected.
  Compilation is side-effect free and grants no Task, Run, Effect, or Tool
  authority. `LIPASRuntime.compile_workflow()` provides the same helper with
  the Runtime workspace as its default.

#### 0.41 · Conversation kernel

- Added durable `Conversation`, `Message`, and `ConversationEvent` resources
  to the existing SQLite authority. Message identity, additive schema
  migration, stable per-conversation cursors, and fail-closed future-version
  checks are explicit.
- Added `LIPASRuntime.create_conversation()`, `append_message()`,
  `conversation_events()`, and idempotent `promote_message_to_task()`. A
  retried actionable message maps to one deterministic Task/Run.

#### 0.42 · Local Web conversation operator

- Added local Web conversation routes for list/detail/messages/events, explicit
  event append, and message promotion.
- Added cursor-based SSE catch-up, stateless HMAC bearer authentication, and
  bounded content-addressed attachments with SHA-256/idempotent upload
  semantics. SSE remains a projection, not a second event authority.

#### 0.43 · Hybrid execution

- Added a first-party HTTPS-gated `RemoteWorkerHTTPClient`/
  `RemoteWorkerHTTPServer` transport with HMAC-SHA256 worker capability
  attestation, fenced leases, and structured remote results.
- Worker events, checkpoints, and Effect observations remain persisted by the
  host-owned Run authority; TLS and key custody remain deployment obligations.

#### 0.44 · Shared team workspace

- Added host-owned `WorkspaceIdentity`, bounded `ApprovalDelegation`, and
  durable policy/audit boundaries without mailbox authority.

#### 0.45 · Production connector vertical

- Added explicit timeout → uncertain → reconcile evidence, provider request
  identity, provider references, connector descriptors, and deterministic
  HTTP/MCP/Email fixtures.
- `DesignPartnerCase` and `run_design_partner_validation()` produce structured
  local-fixture reports; they do not count as external partner evidence.

#### 0.46 · Plan/Handoff interoperability

- Added `AgentPlan`/`PlanStep` and external Plan/Handoff boundaries so graph or
  team hosts can be represented by one scoped LIPAS Run.

#### 0.47 · Observability and evaluation

- Added ExecutionStore-derived `ExecutionMetrics` and `SLOReport` projections
  with cost, replay, incident, and bounded-window evidence.

#### 0.48 · Extension ecosystem

- Added canonical HMAC-SHA256 artifact/manifest verification before
  certification. `ExtensionRegistryService` exposes authenticated metadata,
  registration, revocation, and rollback endpoints without importing or
  executing package code.

#### 0.49 · Release candidate preparation

- Added integrity-checked, lease-fenced workspace backup/restore. Installer,
  compatibility policy, and external partner acceptance remain open.

#### 0.50 · Agentic Execution System bridge

- North-star semantics are now explicit in the public API: an
  `EffectProposal` is admitted by `AgentRuntime.decide_effect()` into an
  `EffectDecision`; `AgentRuntime.execute_effect()` now passes that decision
  into the existing LLM/Tool Harness, which persists proposal provenance on
  the Effect intent and projects a durable `EffectObservation`. It does not
  create a parallel scheduler.
- Repeated proposals recover the existing terminal Effect without a second
  provider/tool call. Intent-only Effects project as `uncertain`; they cannot
  be reported as successful until reconciliation closes the orphan.
- Runtime admission now validates capability iterables, boolean approval
  inputs, and finite non-negative budget snapshots. Effect contracts expose
  structural `as_dict()` views for host-side audit/export without claiming
  that serialization itself grants authority.
- Proposal provenance is namespaced on the intent and preserves `caused_by`;
  arbitrary metadata cannot shadow reserved audit fields. Orphan reconciliation
  accepts the product-facing proposal identity and still closes the mapped
  historical claim exactly once. Reusing an identity with changed provenance
  now fails closed instead of silently returning the old result.
- Approval/replay identity is now bound to the request payload and causal
  parent, including direct Action Gateway and LLM/Tool Harness redelivery.
- HTTP provider request identity is explicit and separate from the operation
  idempotency key; blank identities and redirected external writes fail closed
  as `uncertain`. LangGraph/OpenClaw adapters reject blank fallback identities.
- Input interrupts now have a distinct Local Web answer action instead of being
  presented as approval, and an empty execution window cannot be reported as a
  healthy SLO.
- LangGraph tool calls and MCP notifications now fail closed without a stable
  replay identity; Email reconciliation cannot promote a found result without
  a provider reference; Run creation re-checks Task state inside its write
  transaction.
- The 0.41 conversation projection now rejects cross-conversation Task/Run
  ownership conflicts instead of allowing ambiguous links.
- The 0.43 remote boundary accepts structured `RemoteExecutionResult` values
  and persists worker events, fenced checkpoints, and Effect observations before
  settling the canonical Run.
- Release 0.44 adds the SQLite-backed `WorkspacePolicyStore` for identity,
  delegation, revocation, and policy audit.
- Release 0.45 adds connector descriptors and a bounded process-local rate
  limiter.
- Release 0.46 adds `ExternalRunEnvelope`/`execute_external()` so a LangGraph
  or AutoGen host can be represented by one LIPAS Run.
- Release 0.47 adds cost, incident, evaluation, and bounded SLO projections.
- Release 0.48 adds host trust policy, real HMAC signer verification,
  authenticated registry metadata/revocation endpoints, and rollback.
- Release 0.49 adds integrity-checked, lease-fenced workspace backup/restore.
  These releases do not imply an installer, hosted tenancy, or external
  partner acceptance.
- Release 0.50 adds `LIPASRuntime.execute_effect_for_run()`, which clones a Harness
  onto the Run-owned durable evidence tape and closes the proposal→observation
  persistence gap without changing the low-level compatibility bridge.

### Fixed

- Concurrent first opens of a file-backed SQLite workspace now retry WAL mode
  selection within the shared busy policy instead of surfacing a spurious
  `database is locked` during connection bootstrap.
- Local operator shutdown tests now tolerate the expected reset/refusal race
  when the final wake-up connection arrives as the server is closing.
- Resource accounting now rejects non-finite/overflowing aggregates before
  folding claims; Run-scoped Harness clones reset replay bookkeeping so
  evidence tapes cannot share mutable cursor state.
- Workflow constraints are canonicalized and copied to every compiled step;
  equivalent dependency declarations produce stable plan fingerprints.
- Workspace policy and extension-registry startup now check future schema
  versions before creating business tables, and all projected event kinds
  reject CR/LF so reconnectable SSE cannot be used as a field-injection
  channel.
- Benchmark value objects now reject non-representable counters and report
  only finite throughput values.

### Verification

- Added restart/migration, duplicate promotion, cursor catch-up, execution
  projection, and local Web conversation route tests.
- The full suite passes 825 tests; Ruff, mypy, compileall, and whitespace
  checks pass as well. Real loopback TLS rotation, external provider custody,
  and design-partner acceptance remain deployment evidence obligations.

## [0.40.0] — 2026-08-24 · Local Operator & Recovery Beta

### Added

- First-party `HttpClient` and `MCPClient`/`MCPHttpClient` capability boundaries.
  HTTP writes require egress policy and stable idempotency keys; provider
  transport failures become journalled `uncertain` operations.
- Provider-neutral `EmailConnector` with provider request identity, delivery
  reference, duplicate suppression, and lookup-based reconciliation.
- Local Web approval/risk and operation projections, including explicit
  `/api/approvals`, `/api/operations`, `/api/operations/{key}/reconcile`, and
  `/api/runs/{id}/reopen` operator boundaries.
- Canonical model `Request.request_id`, OpenAI-compatible request headers, and
  one auditable `llm_attempt` claim per retry. Effect results retain aggregate
  billed usage so failed/retried calls cannot disappear from cost accounting.
- Checkpoint payload migration registry (`register_checkpoint_migration`) and
  legacy envelope upgrade; unknown future schemas still fail closed.
- Async timeout orphan convergence and explicit `ToolHarness.reconcile_orphan`
  closeout for synchronous calls that cannot be force-killed.
- Production-minded installation/onboarding and design-partner validation
  protocols are consolidated under `docs/onboarding.md`.

- The Local Web operator now projects task detail together with Run events,
  Interrupts, artifacts, ChangeSet diff state, reports, and product events.
  Task cancellation and explicit `/approve`/`/deny` aliases delegate to the
  existing ExecutionStore transitions; stale mutations return a conflict
  instead of an opaque server error.
- Fault plans are immutable and reusable: each campaign run starts with a
  fresh occurrence counter, so repeated recovery drills remain deterministic.
- The SQLite transition benchmark can use several independent connections to
  measure bounded writer contention (`workers=N`) without adding a metrics
  authority or implying distributed throughput.
- The local operator now serves a dependency-free `/`/`/ui` browser projection,
  bounded reconnectable `/api/runs/{id}/events` pages, and an evidence summary
  for pending approvals/inputs, orphan runs, uncertainty, budgets, verification,
  risks, and event activity. It remains a projection over ExecutionStore.
- Extension manifests now declare provenance, connector scope, approval and
  reconciliation obligations, and an optional maximum LIPAS version.
  `run_conformance()` checks semantic-version compatibility and connector safety
  contracts offline. `run_fault_matrix()` isolates named process, SQLite,
  cancellation, redelivery, and uncertain-member fixtures.
- Reconciliation closeout now persists an operator observation in the operation
  audit event; `found=true` requires a provider reference and Run reopening
  requires JSON evidence. MCP SSE responses reject mismatched request ids,
  and process-level HTTP interrupts preserve their original signal after
  marking the write uncertain.
- `LLMHarness.reconcile_orphan()` now closes an intent-only model Effect from
  an observed Reply/error without issuing a second provider request.

### Fixed

- Durable phase timeouts and logical cancellation racing an unsettled Effect
  now persist `recovery_required`/`reconcile_before_resume` diagnostics. The
  operator API requires uncertainty acknowledgement, reconciliation
  acknowledgement, and recorded evidence before reopening the Run; timeout is
  never treated as a safe retry.
- Concurrent first opens of the same SQLite execution database no longer race
  on bootstrap metadata and raise a false unique-constraint error. Bootstrap is
  idempotent and still fails closed for an unsupported schema version.

### Verification

- 711 tests are collected and pass in the normal environment (the loopback
  socket test may be skipped in restricted sandboxes), including the operator
  projection/authorization boundary,
  terminal event replay, identity-conflict checks, reusable fault plans, and
  multi-connection SQLite contention.

## [0.39.0] — 2026-08-24 · Coordination, Extension SDK & Operator Beta

### Added

- `AgentCoordinator`, an optional ExecutionStore-backed multi-Agent standard
  library. Stable `HandoffEnvelope` values map to deterministic Task/Runs, so
  handoff lease, attempt, cancellation, terminal value/error, and public events
  use the existing execution authority instead of a new mailbox or graph
  database.
- Sequential, RoundRobin, bounded parallel, map/reduce, durable Selector, and
  bounded Swarm-style `Transfer` policies. Selector decisions and reducer work
  are ordinary durable handoffs; parallel results preserve branch order and can
  expose explicit partial failures.
- Runtime and operator surfaces: `LIPASRuntime.coordinator()`, standalone
  coordinator lifecycle, member inspection, `get_handoff_run()`, persisted
  `cancel_handoff()`, and `handoff_started/completed/failed/cancelled` Agent
  event types.
- Durable SQLite-backed Agent members can now run on the already-claimed
  coordination Run. Agent checkpoints, lease heartbeat, Effect recovery, and
  Approval/Input Interrupts share that Run; resuming the same envelope never
  performs a second claim.
- Aggregate `CoordinationEventHandle` pages merge per-Run AgentEvents with a
  reconnectable per-Run cursor map. `SharedBudgetPolicy` reservations are
  atomic and durable, while `CapabilityPolicy` makes member delegation an
  explicit registration contract.
- The 0.39 extension SDK adds `ExtensionManifest`, editable
  `scaffold_extension()`, and offline `run_conformance()` checks. Dependency-
  free `LangGraphHandoffNode` and `AutoGenHandoffHandler` boundaries delegate
  to the same LIPAS coordination Run without importing framework state models.
- A provider-free multi-Agent lesson and bilingual coordination guide covering
  member contracts, replay, redelivery safety, nesting, current limits, and the
  durable Agent-member roadmap.
- 0.40 beta foundations: `LocalWebOperator` projects Tasks, Runs, Interrupts,
  and event cursors over a loopback HTTP boundary without exposing lease
  tokens; mutation routes require an explicit bearer token. `FaultPlan`,
  `FaultCampaign`, and `benchmark_execution_store()` provide deterministic
  recovery drills and bounded local SQLite measurements without adding a queue
  or metrics authority.

### Changed

- New multi-Agent code uses `AgentCoordinator`; legacy `Team` remains a mailbox
  compatibility facade rather than a second Task/Run API. Member registration
  is host-owned configuration while all coordination state is projected from
  `ExecutionStore`.
- Agent members receive stable `caused_by`, coordination/sender/recipient
  metadata, and a branch `RunContext`. Explicit member contract versions join
  the request fingerprint. Inputs are deep-snapshotted before fingerprinting/
  invocation, and only bounded JSON-compatible terminal results can be replayed.
- Resolving an Approval/Input Interrupt returns the same handoff Run to `pending`;
  dispatching the same envelope resumes its checkpoint. Completed model/tool
  Effects replay from the Agent claim tape, while uncertain external Effects
  fail closed.
- Shared reservations are idempotent by `(scope, handoff_id)` and are never
  silently refunded after a failed member. Capability declarations do not
  infer permissions from Skills, Memory, or external framework metadata.

### Safety and limits

- Stable handoff identity reuse with different input fails closed. One live
  lease owns a handoff, heartbeat observes persisted cancellation, and an
  expired lease is not redelivered unless the whole member invocation declares
  `redelivery_safe=True`. Lease loss never authorizes cancellation of a
  possible replacement owner.
- Durable member failures are terminal and are not silently retried. Ordinary
  exception details are not persisted as public results; oversized or
  non-serializable values and spoofed internal result markers fail instead of
  recording an unreplayable success.
- Ordinary `Agent` members without a SQLite-backed session retain the compatible
  non-durable path. Durable Agent recovery is restricted to the stable SQLite
  claim tape; ordinary async members still require an explicit `redelivery_safe`
  declaration before expired-lease reclaim.

### Verified

- 679 tests pass, including stable identity/version conflicts, terminal replay,
  cross-connection lease/heartbeat races, crash-style expired ownership,
  cancellation settlement without redelivery, all coordination policies,
  one-claim durable Agent approval/input resume and replay, payload mutation
  isolation, bounded serialization, and Runtime lifecycle.
- Mypy passes all 69 public source files. Ruff, bytecode compilation, the
  provider-free Tour, release metadata/README mirroring, and whitespace checks
  pass.

## [0.38.0] — 2026-08-22 · SQLite Concurrency & Evidence Store

### Added

- A shared SQLite storage kernel configures every normal durable connection
  with foreign keys, a bounded busy timeout, WAL, power-loss-oriented
  `synchronous=FULL`, untrusted schemas, and automatic WAL checkpoint policy.
  Read-only URI connections are also forced into query-only mode. SQLite
  failures are classified as busy, constraint, read-only, disk-full,
  corruption, or other without hiding the original database exception.
- `SqliteClaimStore.read_page()` provides cursor-based bounded evidence reads.
  `checkpoint_projection()` persists a versioned, rebuildable projection
  snapshot, while the append-only Claim tape remains authoritative. Compatible
  snapshots allow reopen to replay only the delta instead of all history.
- Claim tapes now coordinate concurrent connections with `BEGIN IMMEDIATE`,
  database-assigned monotonic sequence admission, durable claim-id lookup, and
  safe idempotent redelivery. Tag/source/kind sequence indexes make catch-up and
  audit queries proportional to the requested slice.
- Concurrency tests cover four competing Claim writers, concurrent stable-id
  redelivery, snapshot/delta reopen, transaction rollback, WAL policy, and two
  durable Runs crossing the model boundary at the same time.

### Changed

- `LIPASRuntime.run_durable()` and `resume_durable()` create a Run-scoped
  `ExecutionStore` evidence attachment. The Workbench control store remains
  stable, removing the Runtime-wide durable lock and allowing unrelated Runs
  to execute concurrently without redirecting or closing one another's audit
  sink.
- `Workbench.execution_scope()` is now the only Workbench-owned Run evidence
  attachment. The mutable `attach_rowset()`/`attach_global_rowset()` path was
  removed so CLI, examples, and applications cannot replace and close the
  authoritative control handle behind another Run.
- Execution outbox repair uses the Claim unique index rather than rebuilding a
  complete in-memory identity set. Run-scoped repair uses an indexed Run filter,
  claimable Run discovery has a state/lease/creation index, normal transitions
  drain only a bounded batch, and explicit repair streams the complete backlog.
  Legacy outbox reconstruction is stamped once instead of rescanning all
  authoritative rows on every Store open.
- Synchronous Tools use one lazy, bounded process executor with context
  propagation and a bounded submission gate. Forked children create their own
  executor instead of inheriting invalid parent thread state. Short-lived CLI,
  test, and notebook event loops no longer repeatedly create executor pools;
  timeout still leaves an unkillable synchronous action visibly uncertain
  rather than recording a false terminal result.
- SQLite is the supported local and moderate-concurrency backend for 0.38.
  PostgreSQL is not required, and no remote backend or silent fallback has been
  introduced.

### Safety and limits

- SQLite still has one physical writer per database file. LIPAS scales local
  work by short transactions, WAL readers, bounded admission, workspace-level
  databases, and per-Run evidence tapes; it does not claim multi-machine or
  unbounded write concurrency.
- Projection snapshots never delete or replace evidence and are ignored when
  their reducer/configuration fingerprint is incompatible. Snapshot payloads
  are checksummed and anchored to an existing Claim sequence; a corrupt cache
  falls back to the append-only tape. An optional snapshot failure cannot turn
  an already committed Claim into a retryable application failure.
- A normal write waits for at most one configured SQLite busy timeout. Extra
  writer-acquisition attempts require an explicit caller choice, and transaction
  helpers never replay a body or an external side effect.
- Concurrent `OperationJournal.execute()` calls now derive submission ownership
  from the atomic insert. A loser cannot call the provider after observing the
  winner's pending operation. Workbench evidence rejects cross-Task Run ids and
  conflicting event-id reuse; ChangeSet snapshot/apply paths detect source and
  staged-file races before delivery.

### Verified

- 645 tests pass, including the complete Scenario, compatible-provider,
  durable recovery, Workbench, migration, crash-window, and integration suite.
- Mypy, Ruff, bytecode compilation, release metadata, and documentation checks
  pass.

## [0.35.0] — 2026-08-22 · Business Scenario Contracts Alpha

### Added

- `lipas model check` validates an OpenAI-compatible URL, model, API-key
  environment source, transport mode, token-limit field, and honest default
  capabilities without sending a network request. Explicit `--live` performs
  one minimal, potentially billable provider contract probe and reports
  normalized usage or a classified, credential-redacted error.
- Compatible CLI routes accept explicit `--no-api-key` for trusted local
  no-auth gateways. Credential flags are mutually exclusive, provider-only
  arguments fail when they would be ignored, and a custom probe prompt is
  rejected unless `--live` is present.
- A provider-neutral catalog of 17 business Skills covering file/document
  work, coding/review/release, email/report/meeting/notice/proposal/calendar,
  personal letters/speeches/greetings, and connector operating method.
  `SkillRegistry.from_sources(...)` composes explicit built-ins with portable
  local `SKILL.md` paths without auto-injecting the whole catalog.
- `lipas skill list/show` inspect knowledge without running a model. Repeatable
  `--skill` and `--skill-path` select knowledge for built-in chat and Task
  Agents; custom task factories can accept the composed `skills=` registry.
- Immutable `BusinessScenario`, `CapabilityRequirement`,
  `ScenarioAssessment`, and `ScenarioRegistry` contracts. Eighteen packaged
  recipes cover draft-only, staged-workspace, and connector workflows while
  keeping authority in normal Tools and durable Runs.
- `lipas scenario list/show/check` exposes lifecycle, Skill bundle, required
  Tool names and input fields, effect classes, approval point, and idempotency/reconciliation
  obligations without running a model. Repeatable `--scenario` composes a
  minimal Skill set for chat, Task, worker, and resume paths.
- A bilingual product strategy defines LIPAS's trustworthy-execution position
  alongside LangGraph and AutoGen, the remaining graph/team/ecosystem gaps,
  architectural guardrails, and a sequenced path through 0.40.

### Changed

- Packaged Skill discovery is cached per process, Skill metadata is exposed as
  an immutable mapping, and prompt size remains proportional to explicitly
  selected business knowledge.
- Tool-less chat and the default Task Workbench fail before model execution
  when a selected Scenario lacks required capabilities. Custom factories can
  accept composed `skills=` and optional `scenarios=` without a second DSL.

### Safety

- Email and personal-writing Skills are draft/instruction only. They cannot
  contact recipients or grant file, shell, network, or delivery authority;
  future sending connectors remain subject to Tool approval, idempotency, and
  uncertain-result reconciliation.
- Email, calendar, cloud-drive, and ticket connector Scenarios are contracts,
  not bundled provider access. Tool name/input/effect checks explicitly do not claim
  to prove account/object scope, secret handling, data-egress policy, human
  approval, provider evidence, or uncertain-result reconciliation.

### Verified

- 628 tests pass, including Scenario composition, capability absence and
  effect-class mismatch rejection, draft-only authority boundaries, CLI
  selection, and the complete pre-existing durable/runtime suite.
- Mypy passes all 61 public source files. Ruff, bytecode compilation,
  documentation-link validation, README/PKG-INFO mirroring, the provider-free
  durable Tour, and whitespace checks pass.

## [0.32.0] — 2026-08-11 · Compatible Model Endpoints Alpha

### Added

- `OpenAICompatibleAdapter` for the de-facto `/chat/completions` contract used
  by OpenAI, Volcengine Ark, Alibaba Bailian, Tencent Hunyuan, DeepSeek,
  private gateways, and other compatible providers. A versioned API root or
  complete endpoint URL, model name, and Bearer API key are explicit.
- `Agent.openai_compatible(...)` as the high-level factory, with the same
  Session, Effect, tool, cancellation, deadline, durable execution, and
  Workbench semantics as existing Agents.
- Compatible endpoint flags for `lipas chat` and every Task command that
  executes a model: `--base-url`, `--api-key-env`, `--model-streaming`, and
  `--max-tokens-field`.
- Compatibility-first single-response parsing plus opt-in SSE streaming for
  text, reasoning, tool-call arguments, terminal usage, comments, and common
  SSE data framing.
- A bilingual provider guide with secure Python/CLI examples and current URL
  shapes for the named provider families.
- A provider-neutral `lipas[compatible]` installation extra; the existing
  `lipas[openai]` extra remains an equivalent compatibility alias.

### Changed

- Generic compatible endpoints now advertise two honest transport capability
  identities: `openai-compatible` is single-response and
  `openai-compatible-stream` is real SSE. Model-specific tool, structured
  output, reasoning, context, and locality capabilities remain unknown until
  the application registers a tested route. Vision is explicitly unsupported
  by the current text/tool-only adapter boundary.
- Non-streaming is the compatible-provider default. Streaming, usage stream
  options, custom non-authentication headers, and
  `max_completion_tokens` are explicit rather than silently guessed.

### Safety

- Endpoint validation rejects non-HTTP(S), relative, credential-bearing,
  query-bearing, and fragment-bearing URLs, and appends
  `/chat/completions` exactly once.
- The CLI accepts only an API-key environment-variable name; it never accepts
  a plaintext API key argument. Authorization, host, content framing, and
  adapter-owned request fields cannot be overridden through custom headers or
  request extras.
- Exact API-key values echoed by a provider are redacted from HTTP error
  bodies. Authentication, rate-limit, timeout, network, 4xx/5xx, content
  filter, malformed JSON/tool calls/usage, multiple choices, unknown finish
  reasons, and incomplete streams all terminate through the audited Reply
  error contract.

### Verified

- 610 tests pass, including provider URL shapes, message/tool translation,
  deterministic legacy tool ids, SSE assembly, capability validation, CLI
  construction, error classification, and credential redaction.
- Mypy passes the complete public package. Ruff passes every 0.32-changed
  source and test; bytecode compilation, documentation links, the provider-free
  authority Tour, and whitespace checks pass.

## [0.31.0] — 2026-08-10 · Unified Local Runtime Alpha

### Added

- `LIPASRuntime.open()` as the lifecycle owner for execution, Claims,
  operations, artifacts, and persistent audit checks.
- Provider-neutral `AgentEvent`, persisted durable event cursors, `Session`,
  optimistic SQLite conversation snapshots, and cancellable `RunHandle`.
- Run-wide `RunContext` identity, cooperative cancellation, absolute monotonic
  deadlines, and a scoped context accessor for async and sync tools.
- Authority-separated durable `InputPolicy` interrupts.
- Honest `ModelCapabilities`, explicit `ModelRequirements`, and explainable
  compatibility reports.
- Behaviour-neutral read-only `RunObserver` snapshots and advisory recorded
  recommendations.
- Workspace schema v2: compatible execution, product, operation, handoff,
  conversation, and global evidence tables now share `workspace.db` behind
  `LIPASRuntime`. Per-Run Claim/Effect tapes remain isolated deliberately.
- Explicit `lipas migrate plan/apply/verify/rollback` commands. Migration uses
  consistent SQLite backups, assembles a temporary target, verifies row
  counts, integrity, foreign keys, event cursors, and control invariants, then
  atomically activates it. Legacy files are retained.
- Read-only `lipas doctor` and `lipas audit` diagnostics, with an explicit
  `audit --repair` path for recoverable audit mirrors.
- A provider-free `lipas tour --offline` vertical that proves Input cannot
  execute its tool body or grant write authority, Approval authorizes one
  write, and the resumed Run produces durable events, an Artifact, a Report,
  and a healthy audit in a disposable workspace.

### Changed

- Ordinary and durable ReAct execution now reuse terminal and state-transition
  reducers. Existing Supervisor and Workbench APIs remain compatible.
- Every first-party `lipas task` command now opens state through
  `LIPASRuntime`; CLI, Python API, Sessions, Handoffs, Operations, and the
  Workbench no longer select independent global database paths.
- Opening legacy state never performs an implicit migration. It fails with an
  actionable migration command, while direct legacy Store constructors remain
  available through the compatibility window.
- Removed the obsolete duplicated getting-started documents; the tested
  tutorial is the single onboarding path.
- `doctor` now probes the default sandbox instead of treating discovery on
  `PATH` as operational capability. Migration verification remains a focused
  storage-only check, and read-only audit explicitly reports Claim lint as
  not run.

### Fixed

- Runtime, Agent, Workbench, and schema-bootstrap cleanup now attempts every
  owned resource while preserving the original execution/composition error.
- Durable event catch-up and terminal reconnect tolerate a failed UI event
  sink without hiding the persisted Run result.
- Runtime Claim audit/repair now covers every registered per-Run evidence tape
  rather than linting and repairing only the global audit projection.
- Runtime durable convenience calls are serialized so concurrent callers
  cannot replace and close one another's attached ExecutionStore.
- Workbench RowSet replacement is failure-atomic. The legacy unscoped
  `attach_rowset(rowset)` mirror is deprecated in favour of an explicit Run.
- The offline Tour uses one event loop for its complete suspend/resume flow.

### Safety

- Workspace migration is copy-on-write and preserves both a migration-time
  backup and the original v1 files. Rollback preserves all v2-only writes in a
  separate backup instead of deleting them.
- Persistent audit checks detect SQLite corruption, foreign-key violations,
  event cursor gaps, invalid waiting/interrupt relationships, and escaped
  Run-evidence paths.
- Runtime lifetime leases, stale migration-lock diagnosis, WAL checkpointing,
  active-writer refusal, and verified SQLite backup prevent rollback from
  moving live or incomplete workspace state.

### Verified

- 577 tests pass, including schema-v2 migration/rollback, per-Run evidence
  isolation, concurrent worker recovery, and the provider-free authority tour.
- Mypy passes the complete public package. Ruff passes every 0.31-changed
  source, test, and example; bytecode compilation and whitespace checks pass.

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
