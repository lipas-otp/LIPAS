"""Demonstrate replay: re-run a recorded session without hitting Ollama.

Phase 1: live call — folds effect_intent + effect_result + spend into
         store_a; the adapter actually talks to Ollama.

Phase 2: replay   — builds a ReplayCursor over store_a's effect view,
         attaches it to a NEW harness pointing at a NEW (empty) store.
         The harness short-circuits on the cursor and returns the
         recorded Reply byte-equivalent. Note: nothing is folded into
         store_b — replay is a tape, not a re-run.

Sanity check: store_b stays empty, and reply_replayed.usage equals
reply_live.usage (verbatim reproduction).
"""
from __future__ import annotations

import asyncio
import os

from lipas.adapter import Request
from lipas.adapter.ollama import OllamaAdapter
from lipas.calculus import make_default_registry
from lipas.harness import LLMHarness
from lipas.replay import ReplayCursor
from lipas.rows import RowSet
from lipas.rows.capability import CapabilityRow
from lipas.rows.effect import EffectRow
from lipas.store import ClaimStore


MODEL = os.environ.get("LIPAS_OLLAMA_MODEL", "gemma4:12b")


def fresh_substrate() -> tuple[ClaimStore, RowSet]:
    registry = make_default_registry()
    store = ClaimStore(registry)
    rowset = RowSet(store, [
        EffectRow(),
        CapabilityRow(budgets={"tokens_in": 10_000, "tokens_out": 10_000}),
    ])
    return store, rowset


async def main() -> None:
    request = Request(
        model=MODEL,
        messages=[{"role": "user", "content": "Reply with the word OK."}],
        max_tokens=50,
    )

    # ── Phase 1: live ───────────────────────────────────────────
    store_a, rowset_a = fresh_substrate()
    harness_live = LLMHarness(
        adapter=OllamaAdapter(),
        rowset=rowset_a,
    )
    reply_live = await harness_live.call(request)
    print(f"[live]    stop_reason={reply_live.stop_reason} "
          f"usage_out={reply_live.usage.output} "
          f"claims={len(store_a)}")

    # ── Phase 2: replay ─────────────────────────────────────────
    store_b, rowset_b = fresh_substrate()
    cursor = ReplayCursor.from_store(store_a)

    harness_replay = LLMHarness(
        adapter=OllamaAdapter(),  # never actually called
        rowset=rowset_b,
        replay_cursor=cursor,
    )
    reply_replayed = await harness_replay.call(request)

    print(f"[replay]  stop_reason={reply_replayed.stop_reason} "
          f"usage_out={reply_replayed.usage.output} "
          f"claims={len(store_b)}  (expected 0)")

    # ── verification ────────────────────────────────────────────
    assert len(store_b) == 0, "replay must not fold into the new store"
    assert reply_replayed.usage == reply_live.usage, \
        "replay must reproduce usage verbatim"
    print("\nOK: replay reproduced the live reply with no new folds.")


if __name__ == "__main__":
    asyncio.run(main())
