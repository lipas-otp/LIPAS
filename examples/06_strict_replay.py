"""Lesson 06 — replay a recorded tool result without a second live call.

Run::

    python -m examples.06_strict_replay

This is the lower-level pattern behind safe replay.  The first call reads a
local exchange-rate source.  The second call uses ``STRICT_TAPE`` and returns
the recorded value without executing the tool again.  A missing tape entry is
rejected rather than silently reaching a live system.

Use this only when you are wiring a custom runtime.  Ordinary Agent scripts
need only declare a tool's side-effect class and use the session replay API.
"""
from __future__ import annotations

import asyncio

from lipas import tool
from lipas.calculus import make_default_registry
from lipas.replay_tools import ReplayMissing, ReplayMode, ToolReplayer
from lipas.rows import RowSet
from lipas.rows.capability import CapabilityRow
from lipas.rows.effect import EffectRow
from lipas.rows.history import HistoryRow
from lipas.store import ClaimStore
from lipas.tool_harness import ToolHarness
from lipas.tools import ToolRegistry


live_calls = 0


@tool(side_effect="read_only")
def lookup_exchange_rate(base: str, quote: str) -> dict[str, object]:
    """Stand in for a real rate API; the counter proves replay skips it."""
    global live_calls
    live_calls += 1
    return {"base": base, "quote": quote, "rate": 7.24, "source": "demo"}


def fresh_rowset() -> RowSet:
    """The three standard projections used by a normal durable session."""
    return RowSet(
        ClaimStore(registry=make_default_registry()),
        [HistoryRow(), CapabilityRow(budgets={"tool_calls": 10}), EffectRow()],
    )


async def run_demo() -> None:
    global live_calls
    live_calls = 0
    tools = ToolRegistry([lookup_exchange_rate])

    # Record one normal tool call.
    recorded = fresh_rowset()
    live = ToolHarness(tools=tools, rowset=recorded)
    first = await live.call(
        tool_name="lookup_exchange_rate",
        arguments={"base": "USD", "quote": "CNY"},
    )
    print("live result:", first["content"])
    print("live executions:", live_calls)

    # Build a strict replayer from the completed effect tape.
    effect_row = next(row for row in recorded.rows if isinstance(row, EffectRow))
    replay = ToolReplayer(
        view=effect_row.project(recorded.store),
        mode=ReplayMode.STRICT_TAPE,
    )
    replayed = fresh_rowset()
    replay_harness = ToolHarness(tools=tools, rowset=replayed, tool_replayer=replay)
    second = await replay_harness.call(
        tool_name="lookup_exchange_rate",
        arguments={"base": "USD", "quote": "CNY"},
    )
    print("replayed result:", second["content"])
    print("live executions after replay:", live_calls, "(still 1)")

    # A request absent from the tape refuses to fall through to live code.
    try:
        await replay_harness.call(
            tool_name="lookup_exchange_rate",
            arguments={"base": "EUR", "quote": "CNY"},
        )
    except ReplayMissing as error:
        print("unrecorded request refused:", error)


def main() -> None:
    asyncio.run(run_demo())


if __name__ == "__main__":
    main()
