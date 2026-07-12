"""Resource-leak test utility for LLMAdapter implementations.

P2.1 deferred adapter lifecycle (no close()/__aenter__) on the
condition that adapters survive repeated construct-use-discard
without resource growth. This utility enforces that condition.

Methodology
-----------
We do NOT count `len(gc.get_objects())` — that metric is dominated
by Python/pytest/asyncio internal bookkeeping and grows nontrivially
per iteration even in fully stateless code.

Instead we count instances of *specifically tracked types* — by
default the adapter class itself, optionally extended with the
underlying HTTP client (e.g. httpx.AsyncClient) or any other
resource holder. Those instances MUST drop to (near) zero once the
references created in each iteration go out of scope and gc runs.
A linear leak (one retained instance per iteration) produces a
count of ~iterations and fails loudly.

Async-generator caveat
----------------------
asyncio installs an asyncgen finalizer hook that, when an async
generator's refcount hits zero, defers cleanup by scheduling
`agen.aclose()` as a Task on the loop. Until that Task actually
runs, the scheduled coroutine keeps the generator (and therefore
its captured `self` adapter) alive. Naive `gc.collect()` cannot
see past this — they're live, not garbage. We must yield to the
loop a few times before measuring so those finalizer Tasks drain.

(Adapters and helpers like `complete()` SHOULD still explicitly
`aclose()` their streams via `contextlib.aclosing` — relying on
the finalizer hook leaks resources for an indeterminate time and
breaks under loop shutdown.)

Every adapter integration test suite MUST invoke
`assert_no_leak_under_repeated_use` against its adapter, passing
`track_types` for any client/transport objects the adapter holds.
"""
from __future__ import annotations
import asyncio
import gc
from typing import Awaitable, Callable, Sequence, TypeVar

T = TypeVar("T")


def _full_collect() -> None:
    # Multiple passes: first pass may queue finalizers whose execution
    # frees further objects; second pass collects those.
    for _ in range(3):
        gc.collect()


async def _drain_loop(ticks: int = 50) -> None:
    """Yield to the event loop repeatedly so deferred work
    (notably asyncgen finalizer tasks scheduled via the asyncio
    finalizer hook) gets a chance to run to completion."""
    for _ in range(ticks):
        await asyncio.sleep(0)


def _count_instances(types: Sequence[type]) -> int:
    if not types:
        return 0
    types_t = tuple(types)
    return sum(1 for o in gc.get_objects() if isinstance(o, types_t))


async def assert_no_leak_under_repeated_use(
    construct: Callable[[], T],
    call: Callable[[T], Awaitable[None]],
    *,
    iterations: int = 100,
    warmup: int = 3,
    track_types: Sequence[type] | None = None,
    max_residual: int = 2,
) -> None:
    """Construct an adapter, call it, drop it — `iterations` times.
    Assert that no instances of the tracked types remain live.

    Parameters
    ----------
    construct : zero-arg factory producing a fresh adapter instance.
    call      : async fn that exercises the adapter once. Must
                complete and must not retain its argument.
    iterations: number of construct-use-discard cycles.
    warmup    : leading cycles to absorb one-time allocations
                (lazy imports, type caches, etc.) before measurement.
    track_types : types whose live instances are counted post-run.
                Defaults to (type(construct()),) — the adapter
                class itself. Real adapters should pass e.g.
                (AnthropicAdapter, httpx.AsyncClient) so transport
                leaks are caught even if the adapter itself is freed.
    max_residual : max allowed live instances of tracked types after
                gc. Default 2 tolerates pytest/asyncio holding a
                stray reference momentarily; a real linear leak
                produces ~iterations and fails loudly.

    Raises AssertionError on suspected leak, with diagnostic context.
    """
    # Resolve track_types via a throwaway probe instance.
    if track_types is None:
        probe = construct()
        track_types = (type(probe),)
        del probe
        await _drain_loop()
        _full_collect()

    # Warmup — absorb first-touch allocations.
    for _ in range(warmup):
        a = construct()
        await call(a)
        del a
    await _drain_loop()
    _full_collect()

    # Measured run.
    for _ in range(iterations):
        a = construct()
        await call(a)
        del a

    # CRITICAL: drain before counting. asyncio's asyncgen finalizer
    # hook schedules aclose() as a Task; until those Tasks run, the
    # generators (and any `self` they captured) are not garbage,
    # they're live work. Yield to the loop until the queue empties.
    await _drain_loop()
    _full_collect()
    # Second drain+collect: aclose tasks completing may release
    # the last refs to objects that themselves had finalizers.
    await _drain_loop()
    _full_collect()

    live = _count_instances(track_types)
    assert live <= max_residual, (
        f"after {iterations} construct-use-discard cycles, "
        f"{live} instances of {tuple(t.__name__ for t in track_types)} "
        f"remain live (allowed <= {max_residual}); "
        f"suspect resource leak in adapter under test"
    )
