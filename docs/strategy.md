# LIPAS strategy: a third major Agent system

> Language: [English](strategy.md) | [中文](strategy.zh-CN.md)
>
> Date: 2026-08-30

## Current 0.63 capability-complete position

The 0.63 refinement turns the repository's local-first contracts into an
installable and recoverable single-workspace product path. Installation and
upgrade are idempotent, workspace/evidence bundles are integrity-checked,
durability soak is bounded and measurable, TLS contexts can rotate without a
listener restart, and managed-secret, live-provider, and design-partner
interfaces are explicit. The remaining 1.0 work is deployment evidence:
real provider runs, external KMS/HSM custody, loopback-capable TLS drills,
long-duration soak, and independently signed partner workflows.

LIPAS should not win by becoming a smaller LangGraph and AutoGen at the same
time. Its durable position is a conversation-first, trustworthy execution and
delivery platform with a local-first control plane:

```text
LangGraph  = explicit graph and state orchestration
AutoGen    = conversational multi-Agent coordination
LIPAS      = conversational, trustworthy execution, recovery, and evidence-backed delivery
```

The category claim is not “safer prompts.” LIPAS should make it normal for a
natural-language request to become either an answer or a governed Task, and for
every Agent action to have one identity, an honest authority boundary, a durable
intent, a terminal result or visible uncertainty, resumable human input, and
evidence that a user can inspect before accepting delivery. Local-first means
that control, policy, and evidence remain host-controlled; the model and an
approved execution step may be local or explicitly remote.

## Current comparative position

| Surface | LIPAS now | Main gap |
| --- | --- | --- |
| Trustworthy single-Agent execution | strong Effect, approval, replay, budget, cancellation, recovery, staged delivery, and bounded soak | long-running external production evidence |
| Graph orchestration | ordinary Python plus durable Run/Handoff primitives and bounded fan-out/fan-in | conditional graphs, subgraphs, graph visualization and state migration |
| Multi-Agent coordination | ExecutionStore-backed policies, one-claim durable Agent bridge, aggregate event handle, shared budget reservations, capability delegation, and dependency-free LangGraph/AutoGen handoff boundaries | nested fault campaigns, richer visual projections and graph migration |
| Business breadth | 18 declarative Scenarios, 17 Skills, and connector contracts | repeatable real-user workflows with external accounts |
| Model/provider access | Ollama and hardened OpenAI-compatible endpoints | more contract-tested native adapters, embeddings and multimodal boundaries |
| Developer experience | Python API, CLI, Doctor, Tour, trace, reports, soak, provider workflow probe, extension scaffold/conformance SDK, and LocalWebOperator projection | visual timeline, debugger and live evaluation |
| Ecosystem | MCP/action, dependency-free LangGraph/AutoGen handoff boundaries, and signed extension registry/certification | package distribution, revocation operations, examples, and community adoption |
| Conversational product | persisted CLI chat, Sessions, RunHandles, and Local Web projections | one chat-to-Task surface with streaming approvals, artifacts, and reports |
| Deployment | local-first control plane, SQLite, bounded worker concurrency, install/upgrade, backup/restore, TLS rotation, and explicit remote model endpoints | external key custody, partner evidence, queues, tenancy, operational telemetry and controlled scaling |

LIPAS should close these gaps selectively. A graph DSL, social-agent role
system, universal memory, or cloud control plane does not belong in the core
merely because another framework has it.

The next product move is conversation-first integration. The chat surface is a
front door and navigation layer, not a second authority: answer-only turns use
Session/RunHandle; actionable turns create or link a Task/Run; risky actions
become Approval/Input Interrupts; completion is a diff, verification, report,
and explicit delivery. This is the shortest path from a capable runtime to a
product that a new user can understand without learning Python first.

## The 0.50 category: Agentic Execution System

The long-term product should unify two capabilities at a higher layer instead
of embedding one framework inside another:

- Codex/WorkBuddy contribute agency: an Agent operates in a real workspace,
  observes the world, chooses actions, checks results, and keeps working until
  the goal is delivered;
- LangGraph contributes explicit orchestration: durable state, checkpoints,
  conditional paths, human gates, and recoverable composition;
- AutoGen and Microsoft Agent Framework contribute collaboration patterns:
  named members, workflow boundaries, stateful handoff, and bounded
  multi-Agent coordination;
- LIPAS contributes the runtime semantics that make every proposed action
  accountable: Effect identity, capability, budget, approval, recovery,
  replay, audit, observation, and delivery evidence.

The resulting stack is:

```text
┌─────────────────────────────────────────────────────────────┐
│ Execution: Agent / Harness / Tool / Worker                  │
│   perceive → reason → propose Effect → act → observe        │
├─────────────────────────────────────────────────────────────┤
│ Orchestration: Plan / Handoff / Graph adapter / Team         │
│   deterministic steps where known; agentic steps where not   │
├─────────────────────────────────────────────────────────────┤
│ Runtime Semantics: Task / State / Effect / Resource / Policy │
│   admit, reserve, execute, recover, replay, audit, deliver   │
└─────────────────────────────────────────────────────────────┘
                              │
                              ▼
                            World
                              │
                              ▼
                         Observation
```

An Agent does not directly control the world. It proposes an
`EffectProposal`; the Runtime returns an `EffectDecision`; an existing
Harness, connector, or Worker performs the admitted effect; and the result is
recorded as an `EffectObservation`. This is the key boundary that prevents a
multi-Agent system from becoming a conversation club where nobody owns the
actual change.

The 0.50 target is therefore **autonomous workflow**: deterministic workflow
where the path and contract are known, agentic planning and action where the
path is uncertain, and one Runtime semantics layer across both. Graph state,
chat history, model memory, and member messages are context or orchestration
inputs; only Task/Run/Effect/Artifact/Report transitions establish what the
system is allowed to claim about the world.

### Ownership rules for the long term

| Concern | Owner | Must not become |
| --- | --- | --- |
| What should change | Agent / Plan / Handoff | implicit permission |
| Whether it may change | Runtime policy and Approval/Input | a model convention |
| How it changes | Harness / Tool / Connector / Worker | a second scheduler |
| What changed | Effect + Observation + Artifact | chat history |
| Who owns the next step | Task/Run/Handoff identity | an untracked message |
| How we know it is done | Verification / Report / Delivery | a terminal LLM sentence |

This is the architectural north star for 0.50 and beyond. Framework adapters
are useful only when they preserve these ownership rules; copying their graph
DSLs or conversation protocols into core would move LIPAS in the wrong
direction.

## Long-term direction after 0.50

The next horizon should deepen the same layers rather than introduce a new
category of Agent object:

| Horizon | Focus | Proof of maturity |
| --- | --- | --- |
| 0.50 | semantic convergence | deterministic and autonomous steps share Effect admission, recovery, replay, and delivery evidence |
| 0.51 | autonomous workflow compiler | a Goal plus constraints can produce a mixed Plan whose fixed parts remain inspectable and whose adaptive parts remain bounded (reference compiler implemented) |
| 0.52 | execution fabric | local and remote Workers use the same lease, fencing, checkpoint, cancellation, and uncertain-effect protocol |
| 0.53 | world-state/evaluation | Artifacts, observations, verification, cost, and quality metrics support replayable task evaluation and regression gates |
| 0.54 | extension distribution | certified Skills, Scenarios, Tools, and connectors can be discovered, upgraded, revoked, and audited without core edits |
| 0.55+ | controlled scale | shared workspaces, tenancy, policy federation, and enterprise operations are added only after local/hybrid semantics are proven |

The strategic moat is not the number of built-in Agents or graph nodes. It is
the amount of real-world work that can be delegated while preserving an
explainable chain from goal to proposal, admission, effect, observation,
verification, and delivery. That chain should be the compatibility target for
every future provider, framework, and UI.

## Five investment lines

### 1. Make trustworthy execution the reference implementation

- Finish one authoritative Task/Run/Handoff lifecycle and retire independent mailbox ownership.
- Make timeout recovery, lease fencing, cancellation, orphan handling, and external-write reconciliation work under repeated process failure.
- Publish conformance tests for Tool side effects, adapters, checkpoints, connectors, and Scenario capability declarations.
- Add stable export/import and explicit migrations for every durable schema.
- Measure recovery time, duplicate-write incidence, uncertain operations, approval latency, and verified completion—not only test count.

### 2. Turn Scenarios into an extension ecosystem

A distributable business package should contain only the layers it needs:

```text
package manifest
├── Skills                 instruction knowledge
├── Scenarios              lifecycle and capability contracts
├── Tools / connectors     optional executable authority
├── host policy            scope, secrets, approval, egress
└── conformance tests      success, denial, crash, redelivery, reconciliation
```

The next SDK work is manifest/version compatibility, discovery without prompt
injection, a scaffold command, offline validation, package signing/provenance,
and a registry format. Installation must never enable a connector or grant
authority automatically.

### 3. Add an optional orchestration standard library

Implement a small behaviour-neutral coordination protocol above
`ExecutionStore`, followed by a few proven policies: sequential handoff,
RoundRobin, Selector, bounded parallel map/reduce, and Swarm-style transfer.
Every coordination branch remains a Run with shared context, events, and
cancellation. SQLite-backed Agent members now reuse that Run for checkpoints,
Approval/Input Interrupts, and Effect recovery; shared budgets and capability
delegation remain explicit. Coordination must not introduce another mailbox
database or hidden state calculus.

The first standard-library slice is now implemented: stable envelopes map to
deterministic Task/Runs with leases, heartbeat, persisted cancellation,
terminal replay, bounded policies, and explicit redelivery safety. The durable
Agent-member bridge now lets an already-claimed handoff Run host checkpoints,
Approval/Input Interrupts, and Effect recovery without a second claim. The
aggregate event handles, shared reservations, capability delegation, and
dependency-free LangGraph/AutoGen handoff adapters, and the extension
scaffold/conformance SDK are now part of the 0.39 boundary. `LocalWebOperator`,
`FaultCampaign`, `run_fault_matrix()`, and `benchmark_execution_store()` establish
the shipped 0.40 boundary. The hardening pass now projects bounded task detail (including
product evidence), returns explicit state conflicts for stale operator writes,
freezes reusable fault plans, and measures shared SQLite writer contention;
ordinary non-durable Agent members retain the explicit redelivery gate.

The 0.50 Runtime bridge is now a concrete vertical slice: an
`EffectProposal` is admitted by `AgentRuntime`, passed to the matching
Harness, persisted with proposal provenance, and projected back as an
`EffectObservation`. This closes the proposal-to-tape path, but it does not
close the full 0.50 product gate: remote transport, a measured external
vertical, operator-grade evaluation, and design-partner evidence remain open.

The release audit is intentionally independent:

- **0.41** — conversation links reject ambiguous Task/Run ownership and
  projection cursors are reconnectable.
- **0.42** — the local operator provides authenticated SSE, attachments, and
  approval/input projections.
- **0.43** — remote workers persist structured events, checkpoints, and Effect
  observations under lease fencing.
- **0.44** — shared identity and delegation are durable, scoped, and revocable.
- **0.45** — connectors expose explicit descriptors, throttling, and
  timeout-to-reconcile evidence.
- **0.46** — external graphs are hosted as one fenced Run through Plan/Handoff.
- **0.47** — cost, incident, and evaluation projections derive from the
  existing store.
- **0.48** — extension trust supports provenance, certification, and
  revocation/rollback.
- **0.49** — backup/restore is integrity-checked; installation and partner
  acceptance remain open.
- **0.50** — the Runtime bridge is durable; the full autonomous workspace gate
  remains open.

LangGraph and AutoGen adapters should be bidirectional boundaries: those hosts
may orchestrate LIPAS Actions, and LIPAS may invoke an external graph/team as
one scoped capability. LIPAS does not need to copy their complete DSLs.

### 4. Build the operator and developer product

- Local Web: Tasks, event timeline, tool activity, approvals, inputs, diff, artifacts, budget, verification, orphan and connector reconciliation.
- Scenario wizard: choose a recipe, model endpoint, workspace, connector scope, and policy; show missing capabilities before starting.
- Debugger: reconnectable event stream, deterministic replay, checkpoint inspection, cause/effect navigation and redacted exports.
- Fast path: lazy imports, bounded prompt composition, indexed event catch-up, startup benchmarks, concurrent read Tools, and no unnecessary store opens.
- Onboarding: one provider-free Tour per core business vertical, plus Doctor checks that distinguish configuration, loading, generation, sandbox, and connector failures.

### 5. Prove usefulness with real verticals

Start with three repeatable verticals instead of dozens of shallow demos:

1. repository maintenance and release readiness;
2. document/report/meeting workflows inside a local workspace;
3. one scoped external workflow, preferably email draft → approval → delivery → reconciliation.

Calendar, cloud drive, and ticket providers follow only after the connector
contract and UI make scope, data egress, approval, provider evidence, and
uncertain results visible. Each vertical needs design partners, task fixtures,
quality criteria, failure cases, and a measured human-acceptance rate.

## Suggested release sequence

| Release | Primary outcome |
| --- | --- |
| 0.35 | public Scenario contract, broad instruction catalog, capability checks |
| 0.38 | SQLite concurrency kernel, concurrent durable Runs, paged/snapshotted evidence |
| 0.39 | package/scaffold/conformance SDK, durable Agent-member bridge, stronger framework adapters |
| 0.40 | Local Web operator, browser projection, named fault matrix, performance and extension conformance hardening (shipped) |
| 0.41 | conversation kernel and chat-to-Task promotion over the existing Run authority (implemented in the current preview) |
| 0.42 | local Web conversation product and provider-free first-use flow (cursor-streaming preview implemented) |
| 0.43 | scoped hybrid execution and remote Worker protocol |
| 0.44 | shared team workspace, identity, delegated approval, and audit export |
| 0.45 | production connector contracts and one measured external vertical |
| 0.46 | unified Plan/Handoff boundary with LangGraph/AutoGen interoperability |
| 0.47 | observability, evaluation, cost, incident, and SLO surfaces |
| 0.48 | extension provenance, registry shape, and conformance certification |
| 0.49 | release candidate hardening and design-partner acceptance |
| 0.50 | stable Agentic Execution System baseline: conversation-first agency, deterministic/agentic orchestration, and one Runtime Semantics layer |
| 0.60 | historical productionized local-first single-workspace baseline |
| 0.63 | capability-complete refinement: unified Workbench helpers, bounded document/code/archive/web/knowledge tools, architecture guide, and provider-free capability example |

Release numbers are sequencing guidance, not permission to weaken contracts.
A production connector should slip rather than silently retry an uncertain
write or hide missing scope. The 0.36–0.38 reliability work was deliberately
consolidated into 0.38 before adding another operator surface.

## Architectural guardrails

- One Task/Run authority; views and adapters do not create parallel truth.
- Durability changes storage semantics, not Agent meaning.
- Skill and Memory never grant authority or prove an Effect.
- User Input supplies facts; Approval grants a specific action.
- Recommendations are read-only until a behaviour or host accepts them.
- Unknown model or connector capability remains unknown.
- External writes require stable identity, preview, approval, provider evidence, and reconciliation.
- Scenarios compose core contracts; the core never imports business policy.

## Success signals

The most important signals are repeated real tasks, verified acceptance of a
delivered result, successful interruption recovery, zero unrecorded duplicate
writes, understandable approval decisions, connector reconciliation time, and
third-party packages that pass conformance without core changes. Agent count,
graph-node count, model count, and GitHub stars are supporting signals, not the
definition of maturity.
