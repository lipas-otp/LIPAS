"""Minimal end-to-end: send one real request to local Ollama.

Demonstrates the four core moves of LLMHarness:
  - fold effect_intent before the call
  - drive the adapter via call_with_retry
  - fold effect_result with the assembled Reply
  - fold resource_spent for token usage

Prerequisite:
  $ ollama serve            # daemon listening on :11434
  $ ollama pull gemma4      # or whatever LIPAS_OLLAMA_MODEL points at

Run:
  $ python examples/01_single_call.py
"""
from __future__ import annotations

import asyncio
import os

from lipas.adapter import Request
from lipas.adapter.ollama import OllamaAdapter
from lipas.calculus import make_default_registry
from lipas.harness import LLMHarness
from lipas.rows import RowSet
from lipas.rows.capability import CapabilityRow
from lipas.rows.effect import EffectRow
from lipas.store import ClaimStore


MODEL = os.environ.get("LIPAS_OLLAMA_MODEL", "gemma4")


def reply_text(reply) -> str:
    """Pull text out of an Anthropic-shape content tuple."""
    parts = []
    for block in reply.content:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(block.get("text", ""))
    return "".join(parts)


def print_audit(store: ClaimStore) -> None:
    """One line per folded claim, fields truncated for readability."""
    print("\n--- audit trail ---")
    for i, claim in enumerate(store):
        keys = ", ".join(sorted(claim.fields.keys()))
        print(f"  [{i}] {claim.tag:<22} fields={{{keys}}}")


async def main() -> None:
    # --- substrate -----------------------------------------------------
    registry = make_default_registry()
    store = ClaimStore(registry)
    rowset = RowSet(store, [
        EffectRow(),
        CapabilityRow(budgets={
            # Generous budgets — we're demonstrating the happy path.
            "tokens_in":  10_000,
            "tokens_out": 10_000,
        }),
    ])

    # --- harness -------------------------------------------------------
    adapter = OllamaAdapter()  # localhost:11434, no pricing
    harness = LLMHarness(adapter=adapter, rowset=rowset)

    # --- one call ------------------------------------------------------
    request = Request(
        model=MODEL,
        messages=[
            {"role": "user",
             "content": "In one sentence: what is a join-semilattice?"},
        ],
        max_tokens=200,
        temperature=0.2,
    )

    reply = await harness.call(request)

    # --- inspect -------------------------------------------------------
    print(f"model        : {reply.model}")
    print(f"stop_reason  : {reply.stop_reason}")
    print(f"usage        : in={reply.usage.input} out={reply.usage.output}")
    print(f"text         : {reply_text(reply).strip()}")

    if reply.stop_reason == "error":
        print(f"error_detail : {reply.error_detail}")

    print_audit(store)
    print(f"\ncapability projection: {rowset.get('capability').project(store)}")


if __name__ == "__main__":
    asyncio.run(main())
