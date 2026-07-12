"""
Minimal ReAct + ToolHarness demo with PURE tools.

Shows the lower-level ReAct wiring end-to-end:
  - effect_intent / effect_result claims for tool_calls
  - resource_spent claims under tool_calls + wall_seconds buckets
  - HistoryRow iteration claims
  - lineage walk via EffectView
"""
from __future__ import annotations

import asyncio
import os

from lipas.adapter import Request
# Replace with your adapter of choice; OllamaAdapter is a good local default.
from lipas.adapter.ollama import OllamaAdapter
from lipas.behaviour import AgentState
from lipas.calculus import make_default_registry
from lipas.harness import LLMHarness
from lipas.react import ReActAgent
from lipas.rows import RowSet
from lipas.rows.capability import CapabilityRow
from lipas.rows.effect import EffectRow
from lipas.rows.history import HistoryRow
from lipas.store import ClaimStore
from lipas.tool_harness import ToolHarness
from lipas.tools import SideEffectClass, ToolRegistry, tool

MODEL = os.environ.get("LIPAS_OLLAMA_MODEL", "gemma4:12b")


# ── Tools (PURE: replay-safe re-execution, no side effects) ───────────

@tool(side_effect=SideEffectClass.PURE)
def add(a: float, b: float) -> float:
    """Return the sum of two numbers."""
    return a + b


@tool(side_effect=SideEffectClass.PURE)
def multiply(a: float, b: float) -> float:
    """Return the product of two numbers."""
    return a * b


# ── Wiring ────────────────────────────────────────────────────────────

def build_agent() -> tuple[ReActAgent, RowSet]:
    registry = make_default_registry()

    rowset = RowSet(
        ClaimStore(registry=registry),
        rows=[
            HistoryRow(),
            CapabilityRow(budgets={
                # Tight budgets to exercise the gate without blowing
                # the demo.  tool_calls=20 lets the LLM iterate; the
                # token buckets bound the LLM side.
                "tool_calls":   20.0,
                "wall_seconds": 60.0,
                "tokens_in":    10_000.0,
                "tokens_out":   2_000.0,
            }),
            EffectRow(),
        ],
    )

    adapter = OllamaAdapter()  # http://localhost:11434, no API key needed
    tools   = ToolRegistry([add, multiply])

    llm_harness = LLMHarness(adapter=adapter, rowset=rowset)
    tool_harness = ToolHarness(tools=tools, rowset=rowset)

    request_template = Request(
        model=MODEL,
        messages=(),         # filled per-iteration by ReActAgent
        tools=(),            # filled per-iteration
        max_tokens=1024,
        system=(
            "You are a calculator agent. Use the provided tools to compute "
            "answers; do not do arithmetic in your head."
        ),
    )

    agent = ReActAgent(
        harness=llm_harness,
        tools=tools,
        tool_harness=tool_harness,
        rowset=rowset,
        request_template=request_template,
        max_iterations=8,
    )
    return agent, rowset


# ── Run ───────────────────────────────────────────────────────────────

async def main() -> None:
    agent, rowset = build_agent()

    initial = AgentState(messages=(
        {"role": "user", "content": "What is (12 + 7) * 3?"},
    ))

    result = await agent.run(initial)

    print(f"\n=== Final ===")
    print(f"stop_reason: {result.stop_reason}")
    print(f"text:        {result.text!r}")
    print(f"iterations:  {result.metadata.get('iterations')}")

    # Inspect what got folded.
    cap = next(r for r in rowset.rows if r.__class__.__name__ == "CapabilityRow")
    print(f"\n=== CapabilityRow projection ===")
    for bucket, info in cap.project(rowset.store).items():
        print(f"  {bucket:14s}  spent={info['spent']:.3f} / "
              f"limit={info['limit']:.0f}  (overrun={info['overrun']:.3f})")

    eff = next(r for r in rowset.rows if r.__class__.__name__ == "EffectRow")
    view = eff.project(rowset.store)
    print(f"\n=== EffectView ===")
    print(f"  total nodes:  {len(view.nodes)}")
    print(f"  llm calls:    {sum(1 for _ in view.llm_nodes())}")
    print(f"  tool calls:   {sum(1 for _ in view.tool_nodes())}")
    print(f"  orphans:      {len(view.orphans)}")
    print(f"  rejected:     {len(view.rejected)}")

    # ── 关键诊断 ──
    from lipas.effect import (
        F_STATUS, F_ERROR, F_REASON, F_DETAIL, F_KIND,
        TAG_EFFECT_INTENT, TAG_EFFECT_RESULT, TAG_EFFECT_REJECTED,
    )
    print(f"\n=== Effect nodes (raw) ===")
    for c in rowset.store:                      # 如果不可迭代换 .claims() / .all()
        if c.tag in (TAG_EFFECT_INTENT, TAG_EFFECT_RESULT, TAG_EFFECT_REJECTED):
            print(f"  [{c.tag}] kind={c.fields.get(F_KIND)}")
            if c.tag == TAG_EFFECT_RESULT:
                print(f"      status={c.fields.get(F_STATUS)}")
                print(f"      error ={c.fields.get(F_ERROR)}")
            if c.tag == TAG_EFFECT_REJECTED:
                print(f"      reason={c.fields.get(F_REASON)}")
                print(f"      detail={c.fields.get(F_DETAIL)}")

if __name__ == "__main__":
    asyncio.run(main())
