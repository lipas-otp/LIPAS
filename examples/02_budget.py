"""Demonstrate the budget pre-flight gate.

Sets a tight ``tokens_out`` budget and issues a request whose
worst-case output (max_tokens) blows past it. The harness folds
effect_intent + effect_rejected and returns a synthesized error
Reply WITHOUT touching Ollama.

Key observable: the audit trail shows two claims (intent + rejected)
and zero resource_spent — the call never went out.
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


async def main() -> None:
    registry = make_default_registry()
    store = ClaimStore(registry)
    rowset = RowSet(store, [
        EffectRow(),
        CapabilityRow(budgets={
            "tokens_in":   100_000,
            "tokens_out":     50,    # << tight: max_tokens=500 will not fit
        }),
    ])

    harness = LLMHarness(
        adapter=OllamaAdapter(),
        rowset=rowset,
    )

    request = Request(
        model=MODEL,
        messages=[{"role": "user", "content": "Write a long essay."}],
        max_tokens=500,  # worst-case output > tokens_out limit
    )

    reply = await harness.call(request)

    print(f"stop_reason  : {reply.stop_reason}")
    print(f"error_detail : {reply.error_detail}")
    print(f"\nclaims folded: {len(store)}")
    for i, claim in enumerate(store):
        print(f"  [{i}] {claim.tag}")
        if claim.tag == "effect_rejected":
            print(f"        reason={claim.fields.get('reason')!r}")
            print(f"        detail={claim.fields.get('detail')!r}")

    print(f"\ncapability   : {rowset.get('capability').project(store)}")


if __name__ == "__main__":
    asyncio.run(main())
