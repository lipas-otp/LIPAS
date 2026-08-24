# SQLite storage and concurrency

> Language: [English](sqlite-storage.md) | [中文](sqlite-storage.zh-CN.md)
>
> The storage kernel shipped in 0.38 and is the supported backend for LIPAS 0.40.0.

LIPAS deliberately uses SQLite for local and moderate-concurrency deployments.
Agent work is dominated by model, network, sandbox, and tool latency; durable
control writes are small and short. PostgreSQL would add deployment and
operations cost without improving this normal local workload.

## Physical layout and authority

```text
workspace.db
  Task / Run / lease / checkpoint / interrupt
  Workbench / Session / Operation / Handoff / global audit outbox

runs/<run-id>/claims.db
  append-only model, Tool, Effect, usage, and Run evidence
```

`ExecutionStore` is the Task/Run control authority. A Run-local Claim tape is
evidence and replay input; it never decides lease, approval, or checkpoint
state. Transactional outboxes bridge committed control transitions to evidence
without attempting a cross-file distributed transaction.

## Shared connection policy

All normal stores open connections through the same kernel:

- `PRAGMA foreign_keys=ON`;
- `PRAGMA trusted_schema=OFF`;
- a 5-second bounded `busy_timeout`;
- WAL for file-backed writable databases;
- `synchronous=FULL` by default; `NORMAL` is an explicit performance choice;
- `query_only=ON` for read-only URI connections;
- automatic checkpoint after 1,000 WAL pages;
- `BEGIN IMMEDIATE` for CAS/fenced control transactions.

One normal acquisition attempt waits for at most the configured busy timeout.
Only a caller that explicitly requests more attempts can wait again. LIPAS
never automatically replays a transaction body because caller code might
contain an external side effect. No database transaction is held across a
model, network, sandbox, or Tool await.

## Concurrency model

- Several Runs may reason and call independent read Tools concurrently.
- Each durable Run uses its own ExecutionStore connection and evidence sink;
  the Workbench store is stable rather than dynamically reattached.
- SQLite serializes the brief commits to `workspace.db`. WAL allows readers to
  continue during those commits.
- Per-Run evidence tapes remove model/Tool event traffic from the global writer
  hotspot.
- Synchronous Tools share a lazy, fork-safe process executor with bounded worker
  and submission admission. Cancellation cannot kill a Python thread, so a
  timed-out call remains `uncertain` until reconciled.
- Dispatcher and Tool concurrency limits are admission control, not another
  durable queue. ExecutionStore remains the source of pending work.

Recommended envelope for one local workspace is up to 16 active Runs, with far
more Runs durably queued. The exact useful limit depends on model rate limits,
Tool cost, disk latency, and the number of write-heavy operations; benchmark the
real workload instead of treating the number as a database guarantee.

## Evidence paging and snapshots

Claim identity and sequence are allocated under one SQLite writer transaction.
Concurrent delivery of the same identity and payload is a no-op; reuse with a
different payload fails closed.

```python
from lipas.serialization.store_sqlite import SqliteClaimStore

with SqliteClaimStore("claims.db") as store:
    page = store.read_page(after_seq=-1, limit=100)
    cursor = page.next_cursor
    store.checkpoint_projection()
```

A projection snapshot records the merged reducer result and its last sequence.
Reopen loads the newest snapshot with the same reducer/context fingerprint and
replays only later Claims. Snapshots are acceleration structures:

- the append-only tape remains authoritative;
- snapshots never grant authority or prove an Effect;
- incompatible snapshots are ignored;
- corrupt, unanchored, or checksum-invalid snapshots fall back to tape replay;
- deleting snapshots only makes the next open slower;
- snapshot creation failure never makes a committed append appear failed.

`read_page()` and indexed `filter()` read historical Claims from SQLite without
keeping the complete tape resident. `log` remains available for compatibility
and intentionally materializes all Claims requested by that legacy surface.
Normal state transitions drain at most one bounded outbox batch; explicit
`repair_audit()` streams the complete remaining backlog. Legacy audit seeding
is stamped once rather than rescanning all Tasks, Operations, or handoffs at
every open.

## Scaling without a server database

Use simple partitions before adding infrastructure:

1. keep one `workspace.db` per independent workspace or user;
2. keep high-volume evidence per Run, as LIPAS already does;
3. bound active model, read Tool, and write Tool slots separately;
4. keep transactions short and move projection/audit catch-up out of request
   critical paths;
5. archive completed workspace directories with SQLite-consistent backup and
   verification when retention policy allows.

Do not place a writable SQLite workspace on a filesystem whose locking and
durability semantics are unknown. Do not run several machines against the same
database file. If a future product truly needs multi-machine scheduling, a
remote transactional backend can implement the same domain Store contracts;
that is a different deployment tier, not a hidden fallback in the local tier.

For a bounded local contention probe, use the optional benchmark worker count:

```python
from lipas import benchmark_execution_store

result = benchmark_execution_store(
    ".lipas/benchmark.db",
    operations=100,
    workers=4,
)
```

Each worker opens its own SQLite connection and reports transition latency. The
result is a diagnostic sample, not a throughput promise or a replacement for
the append-only evidence and execution authority.

DuckDB is excellent for analytics but not this OLTP control path. LMDB also has
one writer and would require rebuilding SQL constraints and diagnostics.
Redis is not a replacement for the durable authority. libSQL or a managed
SQLite-compatible service may become a remote option, but introduces a server
and must pass the same lease, CAS, outbox, crash, and uncertain-Effect contract
tests before LIPAS advertises it.
