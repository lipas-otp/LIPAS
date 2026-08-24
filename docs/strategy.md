# LIPAS strategy: a third major Agent system

> Language: [English](strategy.md) | [中文](strategy.zh-CN.md)
>
> Date: 2026-08-23

LIPAS should not win by becoming a smaller LangGraph and AutoGen at the same
time. Its durable position is:

```text
LangGraph  = explicit graph and state orchestration
AutoGen    = conversational multi-Agent coordination
LIPAS      = trustworthy execution, recovery, and evidence-backed delivery
```

The category claim is not “safer prompts.” LIPAS should make it normal for an
Agent action to have one identity, an honest authority boundary, a durable
intent, a terminal result or visible uncertainty, resumable human input, and
evidence that a user can inspect before accepting delivery.

## Current comparative position

| Surface | LIPAS now | Main gap |
| --- | --- | --- |
| Trustworthy single-Agent execution | strong Effect, approval, replay, budget, cancellation, recovery, staged delivery | broader crash/fault campaigns and long-running production evidence |
| Graph orchestration | ordinary Python plus durable Run/Handoff primitives and bounded fan-out/fan-in | conditional graphs, subgraphs, graph visualization and state migration |
| Multi-Agent coordination | ExecutionStore-backed policies, one-claim durable Agent bridge, aggregate event handle, shared budget reservations, capability delegation, and dependency-free LangGraph/AutoGen handoff boundaries | nested fault campaigns, richer visual projections and graph migration |
| Business breadth | 18 declarative Scenarios and 17 Skills | production provider connectors and repeatable real-user workflows |
| Model/provider access | Ollama and hardened OpenAI-compatible endpoints | more contract-tested native adapters, embeddings and multimodal boundaries |
| Developer experience | Python API, CLI, Doctor, Tour, trace, reports, extension scaffold/conformance SDK, and LocalWebOperator projection | visual timeline, debugger and live evaluation |
| Ecosystem | MCP/action plus dependency-free LangGraph/AutoGen handoff boundaries | registry, certification, examples and community distribution |
| Deployment | local process, SQLite, bounded worker concurrency | remote workers, queues, tenancy, operational telemetry and controlled scaling |

LIPAS should close these gaps selectively. A graph DSL, social-agent role
system, universal memory, or cloud control plane does not belong in the core
merely because another framework has it.

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
