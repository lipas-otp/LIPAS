"""
examples/loop_with_supervisor.py

Minimal end-to-end shape with the real Supervisor / Policy API,
running against a local Ollama model.

Run:
    ollama serve            # in another terminal
    python -m examples.loop_with_supervisor
"""
from __future__ import annotations

import asyncio
from typing import Optional

from lipas.adapter.ollama import OllamaAdapter
from lipas.calculus import BeliefContext
from lipas.llm import LLM
from lipas.rows import EffectRow, EffectView, RowSet
from lipas.store import ClaimStore
from lipas.supervisor import (
    Policy,
    PolicyRule,
    Predicate,
    Supervisor,
    SupervisorAction,
    TerminateAction,
)
from lipas.supervisor_gate import SupervisorGate
from lipas.tools import tool, SideEffectClass


# ── 1. Tool ─────────────────────────────────────────────────────────

@tool(side_effect=SideEffectClass.READ_ONLY)
def search(query: str) -> str:
    """Search the web for `query` and return a short summary."""
    return f"(fake) top result for: {query}"


# ── 2. A trivial predicate ──────────────────────────────────────────
#
# Predicates take (EffectView, BeliefContext) and return ONE action
# (RetryAction | TerminateAction | EscalateAction) or None.

def never_terminate() -> Predicate:
    def predicate(view: EffectView, ctx: BeliefContext) -> Optional[SupervisorAction]:
        return None
    return predicate


# ── 3. Loop ─────────────────────────────────────────────────────────

async def run(user_question: str, *, max_turns: int = 20) -> str:
    store  = ClaimStore()
    rowset = RowSet(store=store, rows=[EffectRow()])
    llm = LLM(
        adapter = OllamaAdapter(timeout_s=500.0),                  # localhost:11434, no pricing
        rowset  = rowset,
        model   = "gemma4:26b",
        system  = "You are a careful research assistant.",
        tools   = [search],
    )

    policy = Policy.of(
        PolicyRule(name="never_terminate", predicate=never_terminate()),
    )
    supervisor = Supervisor(
        policy     = policy,
        rowset     = rowset,
        session_id = "example-session-1",
    )
    gate = SupervisorGate(supervisor=supervisor, rowset=rowset)

    ...
    messages = [{"role": "user", "content": user_question}]


    reply = None

    for turn in range(max_turns):
        print(f"[turn {turn}] calling llm...", flush=True)
        reply = await llm(messages, tools=[...])
        if reply.is_error:
            print(f"[error] {reply.error_detail}")
            break

        print(f"[turn {turn}] stop_reason={reply.stop_reason} "
              f"tool_calls={len(reply.tool_calls)} text={reply.text!r}",
              flush=True)
        messages.append(reply.as_assistant_message())

        tool_results = []
        for call in reply.tool_calls:
            print(f"[turn {turn}] invoking tool {call.name}({call.arguments})", flush=True)
            payload = await call.invoke()
            tool_results.append({
                "type":        "tool_result",
                "tool_use_id": call.id,
                "content":     str(payload),
            })
        if tool_results:
            messages.append({"role": "user", "content": tool_results})

        if not reply.tool_calls and reply.stop_reason == "end_turn":
            print(f"[turn {turn}] end_turn, breaking", flush=True)
            break

        if not gate.should_continue():
            print(f"[turn {turn}] gate stop", flush=True)
            break

    print(f"[done] final reply.text={reply.text if reply else None!r}", flush=True)
    return reply.text if reply else ""


if __name__ == "__main__":
    print(asyncio.run(run("Why is the sky blue?")))
