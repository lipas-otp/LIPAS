# LIPAS product roadmap

> Language: [English](roadmap.md) | [中文](roadmap.zh-CN.md)
>
> Status: Draft 0.2  
> Date: 2026-07-18

## Product direction

LIPAS is one trustworthy AI execution system with two layers:

- an embeddable Python runtime for Agents, tools, effects, replay, budgets,
  supervision, and durable coordination;
- a first-party local task workbench for running real workspace tasks with
  approvals, recovery, and delivery evidence.

The runtime is available today. The workbench is in development. They share
one repository, roadmap, release line, and execution model.

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
concepts. Both layers use the runtime Effect record rather than maintaining
parallel audit-event models.

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

Caller-facing token streaming, automatic lease heartbeats, and the complete
task-workbench experience remain roadmap work. The execution state store and
the claim/Effect session are deliberately separate durable records, and a
durable Agent currently requires both SQLite stores.

## Delivery phases

### Phase 1: reliable execution slice

- Extend the shipped ReAct checkpoints with lease heartbeats and timeout
  handling.
- Add high-level model and tool streaming.
- Add timeout recovery around the shipped durable cancellation, approval
  interrupt/resume, and orphan detection paths.
- Add Task, Workspace, Run, Approval, Artifact, and Report application models.
- Add bounded filesystem, Shell, and Git capabilities.
- Run an end-to-end `inspect → change → verify → report` flow from the CLI.

Exit criterion: the same task can recover after process termination without
silently losing state or repeating a completed write.

### Phase 2: CLI private alpha

- Add HTTP and MCP capabilities.
- Persist the approval inbox and approval consumption.
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
