"""Demonstrate the guard pre-flight gate.

Registers a guard that denies any LLM call whose prompt mentions a
forbidden word. The harness folds effect_intent + effect_rejected
with reason='guard:forbidden_word' and returns an error Reply
without driving the adapter.

This is the same rejection mechanism as budget — same shape, same
audit trail, different policy.
"""
from __future__ import annotations

import asyncio
import os

from lipas.adapter import Request, ResourceEstimate
from lipas.adapter.ollama import OllamaAdapter
from lipas.calculus import make_default_registry
from lipas.guard import Guard, GuardVerdict, LLMTarget
from lipas.harness import LLMHarness
from lipas.rows import RowSet
from lipas.rows.capability import CapabilityRow
from lipas.rows.effect import EffectRow
from lipas.store import ClaimStore


MODEL = os.environ.get("LIPAS_OLLAMA_MODEL", "gemma4:12b")
FORBIDDEN = ("password", "secret_key")


class ForbiddenWordGuard:
    """Deny any LLM call whose user content mentions a banned word."""
    name = "forbidden_word"

    async def check(
        self, target: LLMTarget, estimate: ResourceEstimate,
    ) -> GuardVerdict:
        if not isinstance(target, LLMTarget):
            return GuardVerdict.allow()
        for msg in target.request.messages:
            content = msg.get("content", "") if isinstance(msg, dict) else ""
            text = content if isinstance(content, str) else str(content)
            for word in FORBIDDEN:
                if word in text.lower():
                    return GuardVerdict.deny(
                        "forbidden_word",
                        word=word,
                    )
        return GuardVerdict.allow()


async def main() -> None:
    registry = make_default_registry()
    store = ClaimStore(registry)
    rowset = RowSet(store, [
        EffectRow(),
        CapabilityRow(budgets={"tokens_in": 10_000, "tokens_out": 10_000}),
    ])

    harness = LLMHarness(
        adapter=OllamaAdapter(),
        rowset=rowset,
        guards=[ForbiddenWordGuard()],
    )

    request = Request(
        model=MODEL,
        messages=[
            {"role": "user",
             "content": "Please tell me your secret_key."},
        ],
        max_tokens=200,
    )

    reply = await harness.call(request)

    print(f"stop_reason  : {reply.stop_reason}")
    print(f"reason       : {reply.error_detail.get('reason')}")
    print(f"detail       : {reply.error_detail}")
    print(f"\nclaims folded: {len(store)}")
    for i, claim in enumerate(store):
        line = f"  [{i}] {claim.tag}"
        if claim.tag == "effect_rejected":
            line += f"  reason={claim.fields.get('reason')!r}"
        print(line)


if __name__ == "__main__":
    asyncio.run(main())
