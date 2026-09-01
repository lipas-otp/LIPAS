# LIPAS product roadmap

> Language: [English](roadmap.md) | [中文](roadmap.zh-CN.md)
>
> Status: 0.63.0 capability-complete local-first refinement shipped. Releases
> 0.41 through 0.51 and the 0.60 productionization baseline have their
> reference contracts implemented and tested. The 0.63 pass makes the
> capability boundary easier to compose and inspect while retaining the same
> authority model. External provider/key-custody, loopback TLS, and partner
> evidence remain deployment gates on the path to 1.0.
> Date: 2026-09-01

## Product direction

LIPAS is a trustworthy Agent execution and delivery platform with a
local-first control plane. It owns the complete path from a conversation or
selected workspace task to approval, interruption recovery, verification, and
evidence-backed delivery. "Local-first" means that workspace data, authority,
policy, and evidence stay in an operator-controlled environment by default; it
does not require the model or every execution provider to run locally. It has
three implementation layers:

- an embeddable Python runtime for Agents, tools, effects, replay, budgets,
  supervision, and durable coordination;
- a first-party local task workbench for running real workspace tasks with
  approvals, recovery, and delivery evidence;
- an explicit execution boundary for local sandboxes, remote-compatible model
  endpoints, and future scoped worker pools.

The runtime and bounded workbench are available today as the 0.63.0
productionized local-first baseline. They share one repository, release line, composition root,
global workspace database, and execution model; per-Run evidence remains an
intentional isolation boundary. Remote-compatible model endpoints and a
provider-neutral HTTPS worker reference transport are available when explicitly
configured. Shared tenancy and a multi-machine control plane remain future
tiers, not hidden promises of 0.40.

Product precedence is explicit: the conversation and local task workbench are
the user-facing product surfaces; the Python runtime is their reliability
foundation and an optional embedding surface. The first-party product should
be conversation-first while keeping Task/Run/Approval/Effect as the durable
control-plane vocabulary.
LangGraph, MCP-server, and OpenCrew/OpenClaw adapters are experimental
compatibility samples, not roadmap commitments or core product surfaces.
The broader competitive position and investment sequence are explicit in the
[LIPAS strategy](strategy.md).

The first product goal is not to support the most models or Agent roles. It is
to let a user start with a natural-language request, see whether it is an
answer or an action, control risky steps, recover safely, and verify the result.

## Conversation-first operating model

Conversation is the front door, not a second execution authority:

```text
chat message
    ├── answer-only turn ─────── Session / RunHandle
    ├── actionable request ───── Task / durable Run
    ├── risky operation ──────── Approval or Input Interrupt
    └── completed work ───────── diff / verification / report / delivery
```

The same event cursor, cancellation, capability policy, and Effect evidence
must serve CLI chat, Local Web chat, Python embedding, and future desktop or
hosted chat surfaces. A conversational host may suggest a Task, but it cannot
silently grant a Tool, bypass approval, or create a second message authority.

## Architecture boundary

```text
CLI / Conversation UI / Local Web / Python host
       │
       ▼
Local-first control plane
  Conversation / Task / Run / Approval / Event / Evidence
       │
       ▼
Business layer
  Scenario / Skill / Connector contract
       │
       ▼
Task workbench
  Task / Workspace / Approval / Artifact / Report / Product policy
       │
       ▼
Python runtime
  Agent / Tool / Effect / Checkpoint / Guard / Budget / Replay / Operation
       │
       ▼
Execution plane
  Local sandbox / explicit model endpoint / scoped future worker
```

This is an internal architecture boundary, not a project boundary. The
workbench may depend on the runtime; the runtime must not depend on workbench
concepts. Tool/model execution evidence remains in the runtime Effect record;
the workbench adds only product-lifecycle events such as task creation,
approval, artifact, verification, and report delivery rather than duplicating
the Effect tape.

A capability moves into the runtime only when a real workbench flow needs it,
it contains no Task, Workspace, UI, or product-policy concepts, and it can be
tested independently. This keeps the runtime reusable without turning it into
an abstraction project detached from user needs.

## First users and vertical

Start with professionals and small engineering, operations, and data teams.
The first vertical is a local workspace task:

```text
select workspace and state a task
             ↓
inspect and propose a plan
             ↓
classify read / local write / external write risk
             ↓
request approval when required
             ↓
execute durably; cancel, pause, or recover
             ↓
deliver changes, verification, cost, and unresolved risk
```

Representative tasks include fixing a bounded defect, updating configuration
or documentation, checking release risk, transforming local data, and invoking
approved HTTP or MCP operations.

## Scenario growth model

Business breadth grows outside the execution kernel:

- Skills add instruction-only knowledge and are selected explicitly so a
  larger catalog does not inflate every prompt.
- Capabilities add bounded real-world actions and remain the only source of
  execution authority.
- `BusinessScenario` composes the minimal Skill bundle, lifecycle, and
  capability requirements without creating a second state machine.
- Durable Runs compose those contracts when a scenario needs approval,
  waiting, recovery, reconciliation, or delivery evidence.

The catalog introduced in 0.40 contains 17 Skills and 18 Scenarios across files, document
processing, coding/review/release, office and personal writing, email,
calendar, cloud drive, and ticket triage. Workspace Scenarios reuse bounded
Workbench Tools; draft Scenarios need no executable authority. Connector
Scenarios publish structural Tool and host-policy requirements but do not
pretend that provider access is bundled. Real external writes still require
scope, preview approval, idempotency, provider evidence, data-egress policy,
and uncertain-result reconciliation.

## Current foundation

The historical 0.10–0.39 slices provide the Python Agent and tool API, durable
SQLite sessions, Effects, guards, budgets, safe replay, supervision, external
operation reconciliation, coordination, and the first durable execution
foundation. The current release is described below by its live contracts, not
by those historical milestone labels. The 0.60 productionization pass adds an
explicit installation manifest, idempotent upgrade path, permission
hardening, release-readiness checks, local secret-file rotation, TLS-bound
operator/worker servers, and a durable bounded workflow executor. These
helpers make the local product installable and auditable; they do not create
external partner evidence or claim hosted multi-tenancy.

The source tree now contains the first complete durable ReAct slice.
`ExecutionStore` persists Task, Run, versioned Checkpoint, and Interrupt state;
run leases fence stale workers; checkpoint-and-suspend is atomic; and approval
resolution is durable and single-consumption. `Agent.run_durable()` connects
the ReAct reason/act/observe phases to that store. It checkpoints model replies,
each completed tool result, conversation state, and terminal results. Recovery
uses run-scoped stable effect identities, so a completed model or tool call is
restored from the Effect tape instead of submitted again; an intent without a
terminal outcome is surfaced as an orphan rather than retried blindly.
Cooperative cancellation is also checkpointed, and a cancel-requested expired
lease can be reclaimed to settle without issuing another external call.
Supervisor ticks have stable run/iteration-scoped claim identities and repair
the recommendation-to-checkpoint crash window idempotently.
ExecutionStore, OperationJournal, and the Team mailbox have explicit schema
compatibility gates. Their authoritative transitions write a Claim-shaped
local outbox atomically and can be mirrored into an attached Claim tape after
a crash. A real subprocess
`SIGKILL` test verifies that a completed write Effect is restored rather than
executed again when its following checkpoint was interrupted.

The execution state store and the claim/Effect session remain deliberately
separate durable records, and a durable Agent currently requires both SQLite
stores. Their crash window is covered by stable identities, transactional
outboxes, repair, and explicit uncertain/orphan states; they are not claimed
to be one distributed transaction.

The 0.20.0 product alpha begins the product release line and adds automatic
lease heartbeat, model/tool
phase timeouts, safe parallel execution for independent read tools, the first
Workspace/Approval/Artifact/Verification/Report product models, bounded
filesystem/Shell/Git capabilities, a persistent bounded multi-Task dispatcher,
staged ChangeSets with drift-checked apply/discard delivery, and the `lipas task`
CLI. Each Run owns an isolated Claim/Effect session while
the global execution store remains the queue. Workbench commands use fail-closed Bubblewrap filesystem/network isolation
by default; raw secrets are rejected before persistence, and allowlisted
environment references are resolved only at tool execution. Product-lifecycle
events are durable and available as JSONL. An end-to-end test covers task
creation, write approval, recovery, verification approval, recovery, and report
delivery. Those were the remaining gaps at the 0.20 milestone; 0.40 now ships
the local operator, explicit timeout reconciliation, and the design-partner
validation protocol (external partner validation remains an open experiment).

The post-LIPA architecture review has now landed two contract slices:
`LIPASRuntime` is the composition root; ordinary, Session, and durable calls
share `RunContext` and `AgentEvent`; durable event cursors support reconnect;
`InputPolicy` is authority-separated from approval; model capability
requirements fail explicitly; and behaviour-neutral `RunObserver`
recommendations are advisory by default. Schema v2 now consolidates compatible
global state into `workspace.db`, with explicit backup/verify/rollback tooling
and persistent audit diagnostics. Per-Run evidence remains isolated. New
multi-Agent work now uses `AgentCoordinator`, which maps deterministic handoff
Task/Runs onto `ExecutionStore`. Legacy Team mailbox ownership remains a
compatibility migration rather than a second authority for new work.
Durable SQLite-backed Agent members now pass that already-claimed handoff Run
into `Agent.run_durable()`: checkpoints, Approval/Input Interrupts, and Effect
recovery share one Run and one lease, with same-envelope resume/replay.

The 0.39 extension slice now also ships reconnectable aggregate event handles,
atomic shared budget reservations, explicit capability delegation,
dependency-free LangGraph/AutoGen handoff boundaries, and the
scaffold/conformance SDK. The historical 0.40 release added a token-protected
`LocalWebOperator` projection, deterministic fault-campaign helpers, and a
bounded local ExecutionStore transition benchmark. The hardening pass adds
bounded task-detail product projections, explicit cancellation/approval
aliases, immutable reusable fault plans, a named fault-matrix runner, a
dependency-free browser projection, and a multi-connection contention probe.
The browser remains intentionally thin and polls reconnectable event pages;
it is not a second scheduler or metrics authority.

The current integrity pass additionally binds approval and replay identities to
their request payload and causal parent, keeps provider request identity
separate from operation idempotency identity, treats redirected external
writes as uncertain, rejects blank adapter identifiers, and avoids reporting
an empty SLO window as healthy. These are hardening guarantees; deployment
evidence for TLS/key custody, provider accounts, and external partners remains
open. The 0.49 and 0.50 reference gates are implemented; the 0.63
deployment-evidence gate carries the remaining work on the path to 1.0.

The 0.32 model-access slice adds one first-party OpenAI-compatible Chat
Completions boundary instead of separate provider subsystems. Explicit URL,
model, API-key source, streaming mode, and token-limit field cover OpenAI,
Volcengine Ark, Alibaba Bailian, Tencent Hunyuan, DeepSeek, and private
compatible gateways. The adapter validates and redacts the transport boundary,
normalizes tools/usage/SSE/errors, and leaves unproven model capabilities
unknown. It does not change the Workbench authority model or make provider
availability a durability guarantee.

The completed 0.35 scenario slice adds immutable `BusinessScenario`,
`CapabilityRequirement`, `ScenarioAssessment`, and `ScenarioRegistry` values.
CLI and Python callers can inspect, compose, and validate 18 recipes while
loading only their selected Skills. Tool-less chat and the default Workbench
fail before model execution when capability requirements are missing or their
effect classes are dishonest. Connector assessment keeps account scope,
secrets, egress, approval, idempotency, provider evidence, and reconciliation
as explicit host obligations.

The completed 0.38 storage slice keeps deployment SQLite-first instead of
requiring PostgreSQL. A shared kernel gives every core Store the same WAL,
bounded busy timeout, transaction, and failure policy. Durable convenience
calls use Run-scoped evidence attachments instead of a Runtime-wide lock.
Claim tapes coordinate concurrent connections, provide indexed cursor pages,
and restore compatible deterministic projections from snapshots before
replaying only the delta. Snapshots remain disposable derived state; the
append-only evidence tape and ExecutionStore authority boundaries do not
change. The target is local and moderate concurrency, not a hidden
multi-machine database.

The historical coordination slice added stable `HandoffEnvelope` identity,
heartbeat/cancellation/terminal replay, fail-closed expired-lease policy, and
sequential, RoundRobin, bounded parallel, map/reduce, durable Selector, and
bounded Swarm composition. It reuses `ExecutionStore`; member registration is
host configuration, not another persistent scheduler. Ordinary Agent members
receive causal metadata and branch `RunContext`, but bridging their inner loop
received causal metadata and branch `RunContext`. The 0.39 durable-member slice
now bridges the inner loop to the already-claimed coordination Run's durable
checkpoints, Interrupts, and Effect recovery without a second claim.

## 0.40 hardening and product completeness

The release line now closes the previously important gaps with bounded,
provider-neutral capabilities:

- [x] First-party `HttpClient` with HTTPS/egress policy, request identity,
  idempotent external writes, and `uncertain` reconciliation through
  `OperationJournal`.
- [x] First-party `MCPClient`/`MCPHttpClient` alongside the existing audited
  MCP server; MCP transport state never becomes LIPAS authority.
- [x] Idempotent `EmailConnector` with provider reference, stable request
  identity, and provider lookup reconciliation.
- [x] A single operation reconciliation sweep plus Local Web projections for
  pending/uncertain operations, approval risk, preview/diff, budget, scope,
  and verification evidence.
- [x] Provider request identity on canonical model requests and each LLM
  retry attempt; aggregate billed usage remains in the Effect result.
- [x] Background convergence for async timeout orphans and an explicit
  `ToolHarness.reconcile_orphan()` closeout for sync tools that cannot be
  force-killed.
- [x] Deterministic checkpoint payload migration hooks and explicit schema
  compatibility gates; unknown future versions still fail closed.
- [x] Provider-free `doctor`/`tour --offline` onboarding plus an install and
  design-partner validation playbook (see [onboarding](onboarding.md)).

The release still does not claim that an arbitrary Python tool is sandboxed or
that a provider without idempotency/reconciliation can offer exactly-once
delivery.

## Delivery phases

### Phase 1: reliable execution slice

- [x] Extend the shipped ReAct checkpoints with lease heartbeats and phase
  timeouts.
- [x] Execute independent reads concurrently while keeping writes and
  policy/accounting-sensitive calls serial and recoverable.
- [x] Give core SQLite stores one WAL/timeout/transaction policy and classify
  contention, read-only, disk-full, and corruption failures explicitly.
- [x] Remove the composition-root durable lock with stable Workbench ownership
  and one Run-scoped evidence attachment per durable call.
- [x] Add concurrent Claim admission, bounded cursor pages, indexed catch-up,
  and rebuildable projection snapshots without compacting away evidence.
- [x] Persist submitted Tasks and dispatch several Runs concurrently with
  atomic leases, heartbeat, expired-run reclaim, and approval slot release.
- [x] Keep task writes in a per-Run staging workspace and require an explicit,
  drift-checked ChangeSet apply or discard delivery decision.
- [x] Add high-level model and tool event streaming with durable catch-up.
- [x] Add Session, RunHandle, and a run-wide context for cancellation and
  absolute deadlines.
- [x] Separate missing user input from capability approval.
- [x] Add explicit model capability requirements and diagnostics.
- [x] Add a hardened OpenAI-compatible Chat Completions route for Python, CLI,
  and Task workers without provider/model fallback.
- [x] Introduce a behaviour-neutral, read-only RunObserver boundary.
- [x] Add ExecutionStore-backed handoff Runs and a bounded multi-Agent
  coordination standard library without another scheduler database.
- [ ] Retire legacy Team mailbox authority after a documented compatibility
  migration; do not dual-write one logical handoff meanwhile.
- [x] Physically consolidate compatible control, event, product, and evidence
  tables behind `LIPASRuntime` without losing per-Run budget isolation.
- [x] Add timeout recovery around the shipped durable cancellation, approval
  interrupt/resume, and orphan detection paths, including explicit uncertain
  reconciliation and sync-tool orphan closeout.
- [x] Add Task, Workspace, Run, Approval, Artifact, Verification, and Report
  application models.
- [x] Add bounded filesystem, Shell, and Git capabilities.
- [x] Add fail-closed OS isolation for first-party command execution.
- [x] Reject raw secrets before persistence and resolve allowlisted references
  only at tool execution.
- [x] Persist task lifecycle events for stream-friendly product consumption.
- [x] Run an end-to-end `inspect → change → verify → report` flow from the CLI.

Exit criterion: the same CLI workspace task recovers across long calls and
process termination without silently losing state or repeating a completed
write; path escapes are denied; every write and command has approval and
evidence; and the report states changes, verification, and uncertainty.

### Phase 2: CLI and extension alpha

- [x] Expand the explicit catalog to 17 Skills and 18 file, engineering,
  office, personal, and connector Scenario contracts.
- [x] Define a distributable Scenario/Connector package manifest, scaffold command,
  and offline conformance checks, including provenance, connector scope,
  approval/reconciliation declarations, and version compatibility fixtures.
- [x] Strengthen bidirectional LangGraph and AutoGen action/handoff adapters without
  importing their graph/team state models into the LIPAS core.
- [x] Add an optional coordination standard library above ExecutionStore with
  bounded Selector, RoundRobin, parallel map/reduce, and Swarm policies.
- [x] Bridge an already-claimed handoff Run into durable Agent checkpoints,
  Approval/Input Interrupts, and Effect recovery without double claiming.
- [x] Add aggregate coordination event cursors, fan-in cause navigation, and
  explicit shared budget/capability policy.
- [x] Add deterministic fault-campaign and local transition-benchmark helpers
  with a multi-connection contention mode and an isolated named fault-matrix
  runner for process-kill, SQLite busy/corruption, cancellation, redelivery,
  and uncertain-member fixtures.
- [x] Add first-party HTTP and MCP client capabilities needed by real LIPAS
  tasks.
- [x] Add the first approved, idempotent email delivery connector after data
  egress policy and uncertain-operation reconciliation became product-visible.
- [x] Turn the CLI approval inbox and single-consumption state into a focused
  diff/risk operator experience.
- [x] Show risk, budget, diff, commands, verification, and uncertain
  operations.
- [x] Make installation and the first provider-free task usable without
  maintainer help (`doctor`, `tour --offline`, migration plan/verify).
- [ ] Work with 3–5 external design partners on recurring workspace tasks;
  the protocol and measurement fixtures are shipped, but external validation
  is intentionally not fabricated by the repository.

Exit criterion: design partners complete real tasks and can explain from the
report what changed, what was verified, and what remains uncertain.

### Phase 3: 0.40 Local Web beta

- [x] Add the dependency-free `LocalWebOperator` HTTP projection with loopback
  default, redacted lease state, and bearer-token mutation guard.
- [x] Add bounded task detail views with Run events, Interrupts, artifacts,
  ChangeSet diff state, reports, and product events.
- [x] Stream execution state, tool activity, and waiting approvals through
  bounded reconnectable event pages and a polling browser projection.
- [x] Support explicit approve/deny aliases and task/run cancellation. Pause
  and continue remain cooperative worker operations rather than unsafe
  operator-side lease manipulation.
- [x] Present diffs, artifacts, budgets, verification, orphan and uncertain
  states without requiring users to read raw logs.

Exit criterion: users can judge task safety and completion from the product UI.

### Phase 4: validate before expanding

- After the 0.49 pilot, expand from the initial 3–5 partners to at least 10
  only when recurring-task evidence is stable.
- Study failed tasks and reasons for manual takeover.
- Fix repeated recovery, approval, tool, and onboarding failures.
- Select the highest-repeat narrow use case for the next release.

## 0.41 Conversation kernel

Goal: make Conversation, Message, and the chat-to-Task link durable resources
over the existing SQLite authority. A caller-visible message identity is
idempotent, has one event cursor, and can be promoted to one Task/Run without
creating a second scheduler or permission system.

Implemented and independently checked:

- [x] Additive schema migration with fail-closed future-version handling;
- [x] Idempotent message append, cross-conversation ownership checks, and
  deterministic message → Task → Run promotion;
- [x] Unified cursor projection for messages, AgentEvents, tool activity, and
  approval/input cards;
- [x] Python, CLI, and HTTP projections share the same message/event contract.

Regression gate: `tests/test_v041_conversation.py` and the existing execution
and storage suites pass. Exit means a retried message can inspect or resume the
same Run from every supported API, with no duplicate execution. Open work is a
richer hosted identity layer; it is not part of this release.

## 0.42 Local Web conversation operator

Goal: let a new user complete inspect → plan → approve → verify → deliver from
a local browser while retaining the 0.41 authority. The operator is a bounded
projection, not a new queue or database.

Implemented and independently checked:

- [x] Conversation list/timeline/composer, task promotion, and bounded detail;
- [x] Authenticated SSE catch-up with cursor reconnect and polling fallback;
- [x] Approval/input cards, tool activity, diffs, reports, and safe
  content-addressed attachments;
- [x] Browser and Python clients use the same event and mutation contracts.

Regression gate: `tests/test_v041_conversation.py`, `tests/test_v040_beta.py`,
and operator route tests pass. Exit means users can judge safety and completion
without reading raw logs or writing Python. Hosted tenancy remains open.

## 0.43 Hybrid execution

Goal: make execution location explicit through a fenced remote Worker while
keeping control, policy, and evidence host-owned.

Implemented and independently checked:

- [x] Capability declaration, HMAC attestation, HTTPS-gated transport,
  lease/heartbeat, cancellation, and attempt fencing;
- [x] `RemoteExecutionResult` persists worker events, checkpoints, and Effect
  observations before settling the canonical Run;
- [x] Redelivery and worker loss converge to one evidence-backed Run.

Regression gate: the 0.43 cases in `tests/test_release_contracts.py` cover
attestation, lease mismatch/expiry, bounded responses, and replay. Exit means a
lost or redelivered worker cannot create a second claim. Production certificate
rotation and cross-region routing remain host responsibilities.

## 0.44 Shared team workspace

Goal: support multiple operators with explicit identities, shared Tasks and
Conversations, delegated approvals, policy scopes, and auditable revocation.

Implemented and independently checked:

- [x] `WorkspacePolicyStore` stores immutable identities, bounded delegation,
  revocation, and policy audit in the same SQLite authority;
- [x] Coordinator and Runtime use one ownership boundary; mailbox state is
  compatibility-only and cannot grant permission;
- [x] Delegation is scoped to action, resource, and expiry.

Regression gate: the 0.44 cases in `tests/test_release_contracts.py` cover
delegation, revocation, and audit. Exit means two operators can collaborate
without ambiguous ownership. External identity providers and tenant isolation
remain later-tier work.

## 0.45 Production connector vertical

Goal: make HTTP, MCP, and Email writes honest under timeout, retry, and
redelivery, then validate one repeatable external workflow.

Implemented and independently checked:

- [x] Explicit operation/provider request identity, idempotency, rate limits,
  secret references, and provider references;
- [x] timeout → uncertain → reconcile for HTTP/MCP/Email, including orphan
  convergence and deterministic local vertical fixtures;
- [x] Connector descriptors and conformance checks do not silently switch
  providers or claim unsupported capabilities.

Regression gate: the 0.45 cases in `tests/test_release_contracts.py` plus
connector suites cover duplicate prevention and reconciliation. Exit means
the reference workflow has no unrecorded duplicate write. Real SaaS account
and provider-SLA evidence remains deployment work.

## 0.46 Plan/Handoff interoperability

Goal: host LangGraph, AutoGen, or another workflow as one scoped LIPAS Run,
without importing a second Task/Run authority.

Implemented and independently checked:

- [x] `ExternalRunEnvelope` and `execute_external()` carry plan, handoff,
  identity, deadline, cancellation, and terminal evidence;
- [x] External graph/team state stays outside LIPAS while every handoff is
  represented by a LIPAS Task/Run identity;
- [x] Blank or unstable framework identities fail closed.

Regression gate: the 0.46 cases in `tests/test_release_contracts.py` cover
identity, cancellation, lease renewal, and terminal replay. State/checkpoint
adapters and cross-framework fixtures remain open.

## 0.47 Observability and evaluation

Goal: let operators measure cost, latency, SLOs, replay, and incidents from
derived projections instead of reading raw databases.

Implemented and independently checked:

- [x] Execution metrics, cost ledger, incident projection, evaluation fixtures,
  and bounded SLO windows derive from `ExecutionStore`;
- [x] Empty or incomplete windows cannot be reported as healthy;
- [x] Replay and failure evidence retain causal Run/Effect identity.

Regression gate: the 0.47 cases in `tests/test_release_contracts.py` plus
performance and execution suites pass. Export dashboards, durable billing,
benchmark datasets, and incident workflow remain open.

## 0.48 Extension ecosystem

Goal: make third-party Scenario, Skill, and Connector packages discoverable
and trustworthy without giving them implicit authority.

Implemented and independently checked:

- [x] Canonical HMAC-SHA256 artifact/manifest signatures and provenance are
  verified before certification;
- [x] Authenticated `ExtensionRegistryService` exposes metadata, revoke, and
  rollback without importing or executing package code;
- [x] Conformance results are explicit and reproducible.

Regression gate: the 0.48 cases in `tests/test_release_contracts.py` cover
signature tampering, trust policy, revocation, and service authentication.
Key custody/rotation, resolver/install sandbox, and third-party certification
remain deployment evidence.

## 0.49 Release candidate and design-partner validation

Goal: turn the independently tested boundaries into an installable, upgradeable
product and measure it with 3–5 external design partners. Local fixtures never
count as partner evidence.

Current state: backup/restore is integrity-checked and lease-fenced. The
repository now ships an explicit `install`/`upgrade` path, a 0600 installation
manifest, `release check` diagnostics, and an evidence bundle that captures
`runs/**` (including each Run's `claims.db`) with a manifest, hashes, and
SQLite checks. Compatibility policy, rollback drills, certificate/secret
rotation, and external acceptance remain deployment work. The release gate is
two consecutive weeks of recurring vertical tasks with no unexplained unsafe
delivery.

## 0.50 Agentic Execution System baseline

Goal: join conversation-first agency, deterministic/agentic orchestration, and
one Runtime Semantics layer. `EffectProposal → Harness → EffectObservation`
must remain durable evidence owned by the Run.

Current state: `execute_effect_for_run()` closes that proposal-to-observation
path and `LIPASRuntime.execute_workflow()` now runs a compiled bounded plan as
one durable Task/Run with lease heartbeat, per-step checkpoints, and
reconnectable step events. The full gate remains
open until a user can start from chat, work autonomously in a real workspace,
mix fixed workflow with adaptive action, collaborate through owned
Tasks/Effects, recover after failure, and accept verified delivery.

## 0.51 Bounded autonomous workflow compiler

Goal: compile a host-owned Goal and constraints into a mixed Plan whose fixed
parts are reviewable and whose adaptive parts have an explicit bound.

Implemented and independently checked:

- [x] `WorkflowGoal` and `WorkflowConstraint` provide strict JSON planning
  inputs without treating constraints as authority;
- [x] `AutonomousWorkflowCompiler` emits a deterministic `CompiledWorkflow`
  backed by the existing `AgentPlan`/`PlanStep` handoff boundary;
- [x] fixed/adaptive modes are inspectable, adaptive count is capped, and
  unknown or cyclic dependencies fail closed;
- [x] `LIPASRuntime.compile_workflow()` keeps compilation side-effect free;
  it does not create a Task, Run, Effect, or Tool claim.
- [x] `LIPASRuntime.execute_workflow()` executes a compiled plan through a
  host callback as one lease-fenced Task/Run, renews the lease, and persists
  per-step checkpoints for resume; world-changing callbacks remain required to
  use the normal Effect bridge.

Regression gate: `tests/test_v051_workflow.py` covers deterministic output,
constraint snapshots, bounded adaptation, dependency validation, and Runtime
workspace defaults. Model-assisted plan synthesis, provider-backed execution,
and production acceptance remain later evidence work.

## Historical 0.60 Productionized local-first single-workspace baseline

Goal: make the local-first control plane installable, recoverable, observable,
and ready for a measured path to 1.0 without pretending that repository tests
are external production evidence.

Implemented and regression-tested in this tree:

- [x] idempotent `install`/`upgrade`, restrictive installation metadata, and
  machine-readable `release check` diagnostics;
- [x] integrity-checked SQLite/evidence backup bundles, offline verification,
  lease-fenced restore, and conservative crash recovery;
- [x] bounded `lipas soak` durability rehearsal with terminal-state invariants;
- [x] TLS 1.2+ configuration and live certificate/trust-context reload for
  Operator and remote Worker endpoints;
- [x] `ManagedSecretResolver` integration boundary with bounded redaction and
  provider-key re-resolution after rotation;
- [x] explicit live-provider workflow evidence and digest-verified
  `DesignPartnerSignoff` artifacts that cannot promote local fixtures to
  external acceptance.

Verification gate: 825 tests pass, with Ruff, mypy, compileall, and whitespace
checks clean. Remaining 1.0 evidence gates are real provider runs, external
KMS/HSM custody, loopback-capable TLS rotation drills, a planned soak period,
and independently signed design-partner workflows.

## 0.63 Capability-complete local-first refinement

Goal: make the growing capability set easier to understand and reuse without
creating another authority, permission system, or execution path.

Implemented and regression-tested in this tree:

- [x] a short architecture guide that maps the request path, module ownership,
  authoritative stores, and the read-only chat/Workbench boundary;
- [x] shared Workbench helpers for path validation, file requirements, digest
  calculation, evidence recording, and atomic writes;
- [x] bounded, dependency-optional document, code, archive, Web, and local
  knowledge capabilities exposed through one Workbench policy boundary;
- [x] safer archive validation that rejects extraction-root members and closes
  archive handles on validation errors;
- [x] provider-free capability smoke example and bilingual documentation/index
  coverage.

Verification gate: 852 tests pass, Ruff and targeted mypy checks are clean, and
the package version, changelog, README, and `PKG-INFO` metadata agree on
0.63.0. Real provider, key-custody, TLS, and partner evidence remains a
deployment concern rather than an implied local test result.

## Release gates: one independent gate per release

The releases above are separate contracts. Each has its own target, regression
coverage, exit criterion, and explicitly open work; completion of one release
does not silently mark another complete. “Implemented” means importable and
tested in this tree, not production evidence.

LIPAS's unifying primitive is therefore:

```text
Conversation → Task → Run → Effect → Artifact/Report → Delivery
```

No milestone should add a second message store, scheduler, permission system,
or hidden retry policy. New execution locations are adapters around the same
control plane, not new Agent semantics.

## 0.50 north-star execution model

The final 0.50 integration is not “LangGraph inside Codex” or “AutoGen beside
LIPAS”. It is one Agent Operating Runtime with three explicit layers:

```text
Agent / Harness / Tool / Worker
  perceive → reason → propose Effect → act → observe
                         │
                         ▼
Plan / Handoff / Graph adapter / Team
  deterministic where known; agentic where uncertain
                         │
                         ▼
Task / State / Effect / Resource / Policy
  admit → reserve → execute → recover → replay → audit → deliver
```

The Agent proposes; Runtime decides; an execution adapter acts; the world is
observed; and the resulting Artifact/Report closes the Task. This is the
meaning of **autonomous workflow**: fixed workflow and autonomous action can
coexist under the same Effect, budget, approval, recovery, and evidence
semantics. Conversation is the human-facing entry point, not the source of
truth; messages, graph state, and memory cannot prove permission or completion.

## Safety defaults

- Deny filesystem access outside the selected workspace by default.
- Keep secret values out of prompts, traces, and reports.
- Record the actual command, exit status, and structured Shell risk class.
- Require approval by default for deletion, publishing, pushing, messaging, and
  external writes.
- Use stable idempotency keys for external writes.
- Mark ambiguous external outcomes as uncertain instead of retrying blindly.
- Never claim completion without verification evidence or an explicit note
  that verification was not performed.
- Never replay a live write by default.

## Explicit non-goals for the current 0.63 release

- a general personal assistant;
- a multi-channel chat gateway;
- an Agent graph editor or simulated Agent society;
- self-generating or self-improving Skills;
- long-term user profiling and general memory;
- a SaaS control plane, SSO, SCIM, complex RBAC, or billing (these are future
  organization-tier concerns, not hidden 0.63 capabilities);
- automatic publish, push, or unrestricted system access.

## Measures that matter

Product signals are repeated real tasks, repeated use of the same task class,
and willingness to pay for a specific frequent workflow. Reliability signals
are terminal or visibly orphaned tool/model calls, durable approvals, stable
forced-interruption recovery, no unrecorded write retries, and evidence attached
to every completion. Model count, Agent count, and GitHub stars are not primary
milestones for this stage.
