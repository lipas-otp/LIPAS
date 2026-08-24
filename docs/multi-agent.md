# Multi-Agent coordination

> Language: [English](multi-agent.md) | [中文](multi-agent.zh-CN.md)
>
> Status: ExecutionStore-backed coordination standard library

LIPAS coordinates multiple owners without introducing a Team database or a
second workflow state machine. `AgentCoordinator` is an optional policy layer;
every accepted handoff becomes one deterministic Task/Run in the existing
`ExecutionStore`.

```text
sequential / parallel / map_reduce / selector / round_robin / swarm
                              │
                              ▼
                       HandoffEnvelope
                              │
                              ▼
                  deterministic Task + Run
                              │
                              ▼
                  ExecutionStore authority
```

The member registry is application configuration, not durable authority. A
restart reconstructs the same names and handlers, then terminal handoffs replay
from the Store. CLI, Web, and Python callers can project the same Runs and
`handoff_started/completed/failed/cancelled` events without inventing another status.

## Choose the smallest ownership boundary

- Keep one `Agent` when a goal shares one conversation, tool set, authority,
  budget, and result.
- Add `AgentCoordinator` when a piece of work needs a named owner, an
  independently visible Run, bounded concurrency, or durable handoff replay.
- Keep legacy `Team` only for existing mailbox-based applications. It remains
  supported as a compatibility facade, but new coordination should not make
  its mailbox a second Task/Run authority.

Several model calls or tools do not by themselves justify multiple Agents.
Members should have genuinely different authority, context, review role, or
recovery ownership.

## Start with ordinary async Python

```python
from lipas import LIPASRuntime


async def research(topic):
    return {"topic": topic, "facts": ["fact A", "fact B"]}


async def write_brief(finding):
    return f"{finding['topic']}: {', '.join(finding['facts'])}"


with LIPASRuntime.open(".lipas") as runtime:
    coordinator = runtime.coordinator(max_concurrency=4)
    coordinator.add("researcher", research)
    coordinator.add("writer", write_brief)

    result = await coordinator.sequential(
        ["researcher", "writer"],
        "release risk",
        coordination_id="release-review-2026-08-23",
    )
    print(result.value)
```

`AgentCoordinator.open("coordination.db")` provides a standalone lifecycle.
A Runtime-created coordinator borrows the Runtime's `ExecutionStore`; closing
it never closes the Runtime.

A member may be a normal `Agent` or an async callable. Callables receive the
payload by default. Register `receives_envelope=True` when a handler needs
sender, recipient, sequence, parent, or metadata:

```python
async def review(envelope):
    return {"from": envelope.sender, "reviewed": envelope.payload}


coordinator.add("reviewer", review, receives_envelope=True)
```

Use `version="..."` on `add()` as the explicit member implementation contract.
It participates in the durable request fingerprint, so a pending or completed
handoff cannot silently change meaning after a deployment. When intentionally
changing that contract, use a new member version and a new handoff identity.

Inputs and results must be JSON-compatible and stay under configured byte
limits. LIPAS snapshots an envelope before execution and gives the handler a
separate copy, so member mutation cannot alter the durable request fingerprint.
An `Agent` member receives `caused_by`, coordination/sender/recipient metadata,
and a branch-specific `RunContext`.

## Policies are composition, not new execution semantics

| API | Shape | Termination |
| --- | --- | --- |
| `handoff` / `dispatch` | one named owner | terminal value, durable failure, cancellation, or visible recovery requirement |
| `sequential` | output of member N becomes input of N+1 | first failure stops the chain |
| `round_robin` | sequential turns across a fixed member order | configured round count or failure |
| `parallel` | bounded fan-out | all branches settle; `require_all=False` exposes ordered partial successes and failures |
| `map_reduce` | bounded fan-out then one durable reducer handoff | all map branches and reducer must succeed |
| `select` | a durable selector chooses from explicit candidates | non-candidate selection fails closed |
| `swarm` | a member returns `Transfer(recipient, payload)` | first ordinary result or `max_hops` |

The Selector is a recorded member, not a hidden router. Swarm transfers are
bounded. Parallel output retains branch order even when completion order
differs. `map_reduce` sends the reducer an ordered `results` list containing
recipient, handoff id, and value. An application can always write a different
reducer in ordinary Python instead of adopting another DSL.

## Identity, leases, and replay

`HandoffEnvelope.create()` derives a stable id from coordination id, sequence,
sender, recipient, and parent unless the caller supplies `handoff_id`. Its full
request has a canonical fingerprint. Reusing the id for different input raises
`CoordinationIdentityConflict` before member code runs.

For one envelope:

1. LIPAS creates or finds its deterministic Task and Run.
2. An atomic Run lease admits one live owner.
3. Heartbeat renews the lease and observes persisted cancellation.
4. A JSON-compatible terminal result completes the Run.
5. Repeating the same request returns the stored result without invoking the
   member.

An ordinary member failure is terminal and is not silently retried. Another
live owner produces `CoordinationBusy`. An expired lease produces
`CoordinationRecoveryRequired` by default because the previous member may have
performed an effect whose outcome was not recorded; the SQLite durable Agent
bridge is the explicit checkpoint/Effect-recovery exception described below.

Set `redelivery_safe=True` only when the **whole member invocation** is pure,
read-only, provider-idempotent, or explicitly reconciled:

```python
coordinator.add("catalog-reader", read_catalog, redelivery_safe=True)
```

This declaration permits reclaim after lease expiry; it does not prove
exactly-once delivery. A normal `Agent.run()` member can have recorded Effects,
but its inner reason/act loop is not checkpointed by the coordination Run. Do
not mark such a member redelivery-safe merely because it is an `Agent`.

### Durable Agent members use one claim

When an `Agent` has a SQLite-backed session, the coordinator passes its already
claimed handoff Run directly to `Agent.run_durable(_claimed_run=...)`. The
coordination Run is therefore the Agent's execution Run: there is no second
claim, queue, or completion record. The Agent owns its phase checkpoints,
heartbeat, Effect recovery, and Approval/Input Interrupts; the coordinator
only records the handoff boundary and translates the terminal `FinalResult`.

```python
with LIPASRuntime.open(".lipas") as runtime:
    agent = Agent(
        adapter=adapter,
        model="provider-model",
        tools=[write_file],
        session_path=".lipas/agent-claims.db",
    )
    runtime.coordinator().add(
        "writer",
        agent,
        approval_policy=writes_require_approval,
    )
```

An approval or input request atomically checkpoints and moves the same Run to
`waiting`. Resolve it through `ExecutionStore.resolve_interrupt(...)`, then
dispatch the same envelope to resume. Completed model/tool Effects are
replayed from the Agent claim tape; an uncertain external Effect fails closed
instead of being submitted silently again. An expired lease on this durable
Agent path is reclaimable for that recovery protocol, while ordinary async
members retain the explicit `redelivery_safe` gate.

## Shared budget and capability delegation

Use `SharedBudgetPolicy` when several branches must consume one hard pool. A
reservation is admitted before the handoff claim in an atomic ExecutionStore
transaction; repeated delivery of the same envelope is idempotent, while a
different estimate fails closed. Reservations are conservative and are not
refunded after a member failure.

```python
from lipas import CapabilityPolicy, SharedBudgetPolicy

coordinator = runtime.coordinator(
    budget_policy=SharedBudgetPolicy({"handoffs": 20}),
    capability_policy=CapabilityPolicy(
        grants={"researcher": {"web.read"}, "writer": {"workspace.write"}},
    ),
)
coordinator.add("researcher", research, capabilities=["web.read"])
coordinator.add("writer", writer, capabilities=["workspace.write"])
```

Capability declarations are host policy, not permissions inferred from Skills
or Memory. A member is rejected at registration when its declared capabilities
are not delegated. For token/cost budgets, provide an estimator to
`SharedBudgetPolicy` and keep it deterministic over the envelope and member
contract.

## Aggregate event handle

`coordinator.event_handle(coordination_id)` merges all per-Run `AgentEvent`
streams for that coordination into bounded pages. The opaque cursor is a map of
per-Run sequences, so reconnects do not require a second global event authority:

```python
handle = coordinator.event_handle("release-review")
page = handle.read(limit=100)
while page.has_more:
    page = handle.read(after=page.next_cursor, limit=100)
```

Cause identity remains in event data (`handoff_id`, sender, recipient, and
parent id), allowing a UI to navigate from fan-in to its branch Runs.

## Cancellation and deadlines

Every branch inherits the parent `RunContext` cancellation token and absolute
monotonic deadline. The coordinator checks both before terminal settlement.
Persisted operator cancellation is also available:

```python
run = coordinator.get_handoff_run(envelope)
coordinator.cancel_handoff(envelope.id)
```

Heartbeat observes `cancel_requested`, stops the member cooperatively, and
settles the authoritative Run as cancelled. Lease loss is different: it is
reported as recovery uncertainty and never used as permission to cancel a
possible replacement owner.

## Failure and data boundaries

- Ordinary member exception details are not persisted in the public result;
  the Run stores a stable error type and a generic message.
- Results that cannot be serialized or exceed the byte limit fail the handoff
  instead of producing an unreplayable success.
- `__lipas_coordination__` is a reserved top-level result field. A handler must
  return the typed `Transfer` value rather than spoofing an internal replay
  marker.
- A failed handoff is not auto-retried. Use a new handoff identity after an
  operator or application has decided that a new attempt is safe.
- Parallel coordination does not cancel already submitted siblings merely
  because another ordinary branch failed. The returned failures retain their
  envelopes so the host can make an explicit decision.
- Skill and Memory still grant no authority. Each Agent's Tools remain its
  executable capabilities; a handoff does not delegate hidden permissions.

## Nesting and host-owned routing

An async member may call another coordinator, so nested teams require no core
graph type. Pass the active `RunContext` deliberately when the nested work
must share cancellation and deadline. The host remains responsible for member
discovery, tenancy, organizational policy, and which coordinator/store a
nested workflow uses.

LIPAS deliberately does not persist Python callables, auto-discover members,
silently substitute an unavailable member, or interpret arbitrary graph state.

## 0.39 delivered and 0.40 shipped boundary

The current slice completes the small coordination standard library:

- stable envelope and deterministic Task/Run mapping;
- lease, heartbeat, durable cancellation, terminal replay, and fail-closed
  identity reuse;
- sequential, RoundRobin, bounded parallel, map/reduce, durable Selector, and
  bounded Swarm transfer;
- Runtime composition, Agent causality, public events, byte bounds, and
  cross-connection concurrency tests;
- one-claim durable Agent members with checkpointed Approval/Input suspension,
  Effect recovery, and same-envelope resume/replay;
- aggregate reconnectable event handles, atomic shared budget reservations,
  explicit capability delegation, and dependency-free LangGraph/AutoGen
  handoff boundaries;
- extension `ExtensionManifest`, scaffold, and offline conformance SDK.
- deterministic `LocalWebOperator`, fault-campaign helpers, and a bounded
  ExecutionStore transition benchmark for the 0.40 beta.

The shipped 0.40 contract deepens this boundary rather than adding role names:

1. expose the operator's Task/Run/Interrupt/event projection through a local
   Web UI while keeping mutations token-protected and delegated to the store;
2. run process-kill, database busy/corruption, cancellation-race, and
   redelivery-safe/uncertain-member fixtures at explicit named boundaries;
3. add connector scope, approval, reconciliation, provenance, and version
   compatibility fixtures to extension conformance;
4. only then consider a versioned declarative graph package with migration and
   subgraph semantics. It should remain optional.

SQLite is appropriate for this local and moderate-concurrency design. Remote
workers, multi-host fencing, tenancy, and distributed queues are separate
deployment work and are not implied by `AgentCoordinator`.
