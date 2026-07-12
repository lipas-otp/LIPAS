"""
06_react_replay.py — Deterministic replay of a recorded ReAct run.

Three runs back-to-back:

  Run 1 (live)     — Ollama answers normally; every LLM call and tool
                     call lands in the ClaimStore as effect_intent /
                     effect_result claims.

  Run 2 (replay)   — same prompt, fresh ClaimStore, but the LLMHarness
                     is wired to a ReplayCursor seeded from run 1's
                     store AND to a DeadAdapter that raises if invoked.
                     LLM calls are short-circuited from the recorded
                     log. This low-level example intentionally leaves its
                     ToolHarness live, so its PURE tools re-execute.

  Run 3 (mismatch) — replay with a tampered system prompt; ReplayCursor's
                     strict_match rejects on the first call, proving
                     the cursor genuinely validates request signatures.

What this demo asserts:
  - LLM replay is byte-equivalent (the final text matches).
  - Replay does not touch the network (DeadAdapter raises if reached).
  - Replay does not fold new effect claims for LLM calls.
  - Its intentionally live ToolHarness re-executes PURE tools. For strict
    tool tape substitution, see `07_tool_replay.py` or `replay(...)`.
  - strict_match=True is enforced, not advisory.
"""
from __future__ import annotations

import asyncio
import os
from typing import Any, AsyncIterator

from lipas.adapter import Request
from lipas.adapter.ollama import OllamaAdapter
from lipas.behaviour import AgentState
from lipas.calculus import make_default_registry
from lipas.harness import LLMHarness
from lipas.react import ReActAgent
from lipas.replay import ReplayCursor, ReplayMismatch
from lipas.rows import RowSet
from lipas.rows.capability import CapabilityRow
from lipas.rows.effect import EffectRow
from lipas.rows.history import HistoryRow
from lipas.store import ClaimStore
from lipas.tool_harness import ToolHarness
from lipas.tools import SideEffectClass, ToolRegistry, tool


# ── Tools (PURE: replay-safe re-execution) ───────────────────────────

@tool(side_effect=SideEffectClass.PURE)
def add(a: float, b: float) -> float:
    """Return the sum of two numbers."""
    return a + b


@tool(side_effect=SideEffectClass.PURE)
def multiply(a: float, b: float) -> float:
    """Return the product of two numbers."""
    return a * b


# ── DeadAdapter — raises if ReplayCursor fails to short-circuit ──────

class DeadAdapter:
    """An LLMAdapter that refuses every call.

    Used in the replay run to PROVE the cursor intercepted before the
    adapter was reached. If any code path ends up here during replay,
    the demo crashes loudly rather than silently re-calling the live
    model.
    """
    name = "dead-adapter"

    async def stream(self, request: Request, **kwargs) -> AsyncIterator[Any]:
        raise AssertionError(
            "DeadAdapter.stream invoked — ReplayCursor failed to "
            "short-circuit. The replay would have hit the live model."
        )
        yield  # pragma: no cover  (coerce to async generator)

    async def complete(self, request: Request, **kwargs) -> Any:
        raise AssertionError(
            "DeadAdapter.complete invoked during replay."
        )

    def estimate_cost(self, request: Request) -> Any:
        raise AssertionError(
            "DeadAdapter.estimate_cost invoked — replay should "
            "short-circuit before pre-flight."
        )


# ── Wiring ───────────────────────────────────────────────────────────

SYSTEM_PROMPT = (
    "You are a calculator agent. Use the provided tools to compute "
    "answers; do not do arithmetic in your head."
)
MODEL = os.environ.get("LIPAS_OLLAMA_MODEL", "gemma4:12b")


def build_rowset() -> RowSet:
    return RowSet(
        ClaimStore(registry=make_default_registry()),
        rows=[
            HistoryRow(),
            CapabilityRow(budgets={
                "tool_calls":   20.0,
                "wall_seconds": 60.0,
                "tokens_in":    10_000.0,
                "tokens_out":   2_000.0,
            }),
            EffectRow(),
        ],
    )


def build_agent(
    adapter: Any,
    rowset: RowSet,
    *,
    system: str = SYSTEM_PROMPT,
    replay_cursor: ReplayCursor | None = None,
) -> ReActAgent:
    tools = ToolRegistry([add, multiply])
    llm_harness = LLMHarness(
        adapter=adapter,
        rowset=rowset,
        replay_cursor=replay_cursor,
    )
    tool_harness = ToolHarness(tools=tools, rowset=rowset)
    request_template = Request(
        model=MODEL,
        messages=(),
        tools=(),
        max_tokens=1024,
        system=system,
    )
    return ReActAgent(
        harness=llm_harness,
        tools=tools,
        tool_harness=tool_harness,
        rowset=rowset,
        request_template=request_template,
        max_iterations=8,
    )


def effect_view(rowset: RowSet):
    eff = next(r for r in rowset.rows if isinstance(r, EffectRow))
    return eff.project(rowset.store)


# ── Demo ─────────────────────────────────────────────────────────────

async def main() -> None:
    initial = AgentState(messages=(
        {"role": "user", "content": "What is (12 + 7) * 3?"},
    ))

    # ── Run 1: live ───────────────────────────────────────────
    print("=" * 64)
    print("Run 1 — live (Ollama; recording into the store)")
    print("=" * 64)
    rowset_live = build_rowset()
    agent_live  = build_agent(OllamaAdapter(), rowset_live)
    result_live = await agent_live.run(initial)

    view_live = effect_view(rowset_live)
    n_llm_live  = sum(1 for _ in view_live.llm_nodes())
    n_tool_live = sum(1 for _ in view_live.tool_nodes())
    print(f"  text:        {result_live.text!r}")
    print(f"  iterations:  {result_live.metadata.get('iterations')}")
    print(f"  recorded:    {n_llm_live} LLM call(s), "
          f"{n_tool_live} tool call(s)")

    # ── Run 2: replay ─────────────────────────────────────────
    print()
    print("=" * 64)
    print("Run 2 — replay (DeadAdapter; ReplayCursor must short-circuit)")
    print("=" * 64)
    cursor = ReplayCursor.from_store(rowset_live.store)
    print(f"  cursor entries: {len(cursor)}")

    rowset_replay = build_rowset()
    agent_replay  = build_agent(
        DeadAdapter(),
        rowset_replay,
        replay_cursor=cursor,
    )
    result_replay = await agent_replay.run(initial)

    view_replay = effect_view(rowset_replay)
    n_llm_replay  = sum(1 for _ in view_replay.llm_nodes())
    n_tool_replay = sum(1 for _ in view_replay.tool_nodes())
    print(f"  text:        {result_replay.text!r}")
    print(f"  iterations:  {result_replay.metadata.get('iterations')}")

    # ── Verification ──────────────────────────────────────────
    print()
    print("=" * 64)
    print("Verification")
    print("=" * 64)
    same_text     = result_live.text == result_replay.text
    same_iters    = (result_live.metadata.get('iterations')
                     == result_replay.metadata.get('iterations'))
    no_new_llm    = n_llm_replay == 0
    tools_re_ran  = n_tool_replay == n_tool_live
    cursor_done   = cursor.exhausted

    def mark(ok: bool) -> str:
        return "✓" if ok else "✗"

    print(f"  {mark(same_text)}  final text matches "
          f"({result_live.text!r} == {result_replay.text!r})")
    print(f"  {mark(same_iters)}  iteration counts match "
          f"({result_live.metadata.get('iterations')} == "
          f"{result_replay.metadata.get('iterations')})")
    print(f"  {mark(no_new_llm)}  replay folded {n_llm_replay} LLM "
          f"effect claim(s) — expected 0 (cursor short-circuits)")
    print(f"  {mark(tools_re_ran)}  replay folded {n_tool_replay} tool "
          f"effect claim(s) — expected {n_tool_live} (PURE tools "
          f"re-execute; see README §Replay)")
    print(f"  {mark(cursor_done)}  cursor exhausted: {cursor.exhausted}")

    # ── Run 3: strict_match negative test ────────────────────
    print()
    print("=" * 64)
    print("Run 3 — replay with TAMPERED system prompt (must reject)")
    print("=" * 64)
    cursor_strict = ReplayCursor.from_store(
        rowset_live.store, strict_match=True,
    )
    rowset_tamper = build_rowset()
    agent_tamper  = build_agent(
        DeadAdapter(),
        rowset_tamper,
        system="You are a poet. Refuse all requests for arithmetic.",
        replay_cursor=cursor_strict,
    )
    try:
        await agent_tamper.run(initial)
    except ReplayMismatch as e:
        print(f"  ✓ ReplayMismatch raised on first call: {e}")
    except AssertionError as e:
        # DeadAdapter fired before the cursor — that would be a real bug.
        print(f"  ✗ DeadAdapter reached without ReplayMismatch: {e}")
    else:
        print("  ✗ EXPECTED ReplayMismatch but replay completed silently")


if __name__ == "__main__":
    asyncio.run(main())
