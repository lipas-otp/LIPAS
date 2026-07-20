# LIPAS product roadmap

> Language: [English](roadmap.md) | [中文](roadmap.zh-CN.md)
>
> Status: Draft 0.3
> Date: 2026-07-20

## Product direction

LIPAS is a local trustworthy task agent for individuals and small teams. It
owns the complete path from a selected workspace and task to approval,
interruption recovery, verification, and evidence-backed delivery. It has two
implementation layers:

- an embeddable Python runtime for Agents, tools, effects, replay, budgets,
  supervision, and durable coordination;
- a first-party local task workbench for running real workspace tasks with
  approvals, recovery, and delivery evidence.

The runtime is available today. The workbench is available as a bounded
0.20.0 product alpha and remains under active development. They share one
repository, roadmap, release line, and execution model.

Product precedence is explicit: the local task workbench and later product
surfaces are the independent user-facing product; the Python runtime is its
internal reliability foundation and an optional advanced embedding surface.
LangGraph, MCP-server, and OpenCrew/OpenClaw adapters are experimental
compatibility samples, not roadmap commitments or core product surfaces.

The first product goal is not to support the most models or Agent roles. It is
to make users willing to delegate a real write operation because they can see
what will happen, control risky actions, recover safely, and verify the result.

## Architecture boundary

```text
CLI / Local Web
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
Filesystem / Shell / Git / HTTP / MCP / Model providers
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

## Current foundation

The 0.10.0 public beta provides the Python Agent and tool API, durable SQLite
sessions, Effects, guards, budgets, safe replay, supervision, external
operation reconciliation, at-least-once Team handoffs, and the first durable
execution foundation.

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

The 0.10.0 release did not yet include caller-facing token streaming,
automatic lease heartbeats, or the complete task-workbench experience. The
execution state store and the claim/Effect session are deliberately separate
durable records, and a durable Agent currently requires both SQLite stores.

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
delivery. Live UI streaming, broader timeout recovery policy, and validation
with real design partners remain unfinished.

## Delivery phases

### Phase 1: reliable execution slice

- [x] Extend the shipped ReAct checkpoints with lease heartbeats and phase
  timeouts.
- [x] Execute independent reads concurrently while keeping writes and
  policy/accounting-sensitive calls serial and recoverable.
- [x] Persist submitted Tasks and dispatch several Runs concurrently with
  atomic leases, heartbeat, expired-run reclaim, and approval slot release.
- [x] Keep task writes in a per-Run staging workspace and require an explicit,
  drift-checked ChangeSet apply or discard delivery decision.
- [ ] Add high-level model and tool streaming.
- [ ] Add timeout recovery around the shipped durable cancellation, approval
  interrupt/resume, and orphan detection paths.
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

### Phase 2: CLI private alpha

- Add first-party HTTP and MCP client capabilities needed by real LIPAS tasks.
- Turn the shipped CLI approval inbox and its single-consumption state into a
  focused diff/risk operator experience.
- Show risk, budget, diff, commands, verification, and uncertain operations.
- Make installation and the first real task usable without maintainer help.
- Work with 3–5 design partners on recurring workspace tasks.

Exit criterion: design partners complete real tasks and can explain from the
report what changed, what was verified, and what remains uncertain.

### Phase 3: Local Web

- Add task list and task detail views.
- Stream execution state, tool activity, and waiting approvals.
- Support approve, deny, cancel, pause, and continue.
- Present diffs, artifacts, budgets, verification, and orphan states without
  requiring users to read raw logs.

Exit criterion: users can judge task safety and completion from the product UI.

### Phase 4: validate before expanding

- Support at least 10 real design partners.
- Study failed tasks and reasons for manual takeover.
- Fix repeated recovery, approval, tool, and onboarding failures.
- Select the highest-repeat narrow use case for the next release.

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

## Explicit non-goals for the first release

- a general personal assistant;
- a multi-channel chat gateway;
- an Agent graph editor or simulated Agent society;
- self-generating or self-improving Skills;
- long-term user profiling and general memory;
- a SaaS control plane, SSO, SCIM, complex RBAC, or billing;
- automatic publish, push, or unrestricted system access.

## Measures that matter

Product signals are repeated real tasks, repeated use of the same task class,
and willingness to pay for a specific frequent workflow. Reliability signals
are terminal or visibly orphaned tool/model calls, durable approvals, stable
forced-interruption recovery, no unrecorded write retries, and evidence attached
to every completion. Model count, Agent count, and GitHub stars are not primary
milestones for this stage.
