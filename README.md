# lipas

lipas is an LLM agent runtime where every effect is a recorded fact.
Tool calls declare their side-effect class. Budgets are enforced
pre-flight. LLM calls can be replayed deterministically.

Status: alpha — Ollama tested, single-agent, in-memory store. See
Known limits below.

---

## What makes lipas different

**Side-effect classification is required, not optional.**
Every tool declares `PURE` / `READ_ONLY` / `IDEMPOTENT_WRITE` /
`EXTERNAL_WRITE` at registration. The harness uses this to gate retries,
enforce guards, and determine replay safety — without any user code.

**Budgets are enforced before the call fires.**
A pre-flight check compares current spend + estimated cost against your
declared limit. If it would exceed, the call is not issued, and a typed
rejection claim is folded. You get a bill-shaped surprise exactly never.

**LLM calls are deterministically replayable.**
Record a run. Replay it. The LLM adapter is short-circuited — no network,
no tokens, no variance. Use this for debugging, counterfactual testing,
or re-running a session against tighter budgets to see where it would
have been rejected.

**The audit trail is structural, not textual.**
Every call produces `effect_intent` → `effect_result` → `resource_spent`
claims in an append-only store. Query by tag, filter by tool name,
walk effect lineage — without parsing logs.

---

## What's in this alpha version

- Single-agent ReAct loop with full effect-log audit trail
- Side-effect-aware tool harness with pre-flight budget gate
- Deterministic LLM replay via `ReplayCursor` and `ReplayingAdapter`
- Custom policy guards — `allow` / `deny` with structured reason,
  uniform across LLM and tool calls

**Not in the alpha version:** streaming output, multi-agent orchestration,
supervision tree (Phase 4), tool-side replay substitution (Phase 4+),
persistent claim store (in-memory only).

---

## Quickstart

Requires [Ollama](https://ollama.com) for the default examples.
No API key needed.

```bash
# 1. Start Ollama and pull a model
ollama serve &
ollama pull gemma4           # or: the other models

# 2. Install lipas
pip install -e .
pip install httpx            # required by OllamaAdapter

# 3. Run demos in order
python examples/01_single_call.py        # happy path + audit trail
python examples/02_budget.py             # pre-flight budget rejection
python examples/03_guard.py              # guard rejection
python examples/04_replay.py             # single-call LLM replay
python examples/05_react_calculator.py   # ReAct + ToolHarness end-to-end
python examples/06_react_replay.py       # replay an entire ReAct run
```

> **Ollama version:** tested with Ollama ≥ 0.3. If `gemma4` is
> unavailable on your version, substitute any chat model you have
> pulled (`ollama list` to check).

---

## Core concepts

### Claim

The atomic unit of state. Every event in lipas — an LLM call, a tool
result, a resource spend, a guard rejection — is a `Claim` folded into
an append-only `ClaimStore`. Claims are idempotent: re-delivering the
same claim is always a no-op, and the delivery order does not matter.

Guarantees are neither bolted on nor feature list — they're consequences.
An algebraic operation (claim merge, ⊕) and a three-row partition of
agent state. You don't need to read the foundations to use lipas —
but if you want to know why deterministic replay is achievable at
all, or convince yourself the invariants actually hold, start with
assist/one-calculus.md and assist/three-rows.md .

### SideEffectClass

```python
from lipas.tools import tool, SideEffectClass

@tool(side_effect=SideEffectClass.PURE)
def add(a: float, b: float) -> float:
    """Return the sum of two numbers."""
    return a + b

@tool(side_effect=SideEffectClass.EXTERNAL_WRITE)
def send_email(to: str, subject: str, body: str) -> dict:
    """Send email. Non-idempotent — requires guard approval."""
    ...
```

`side_effect` is **required**. The harness uses it to decide:
- whether to retry on failure (only `PURE`, `READ_ONLY`, `IDEMPOTENT_WRITE`)
- whether to run guards (always for `EXTERNAL_WRITE`)
- whether replay is safe (see [Replay](#replay))

### Budgets

```python
from lipas.rows.capability import CapabilityRow

CapabilityRow(budgets={
    "tool_calls":   20.0,
    "wall_seconds": 60.0,
    "tokens_in":    10_000.0,
    "tokens_out":   2_000.0,
})
```

Budgets are per-bucket hard limits. The pre-flight check runs before
every call. The fold-time gate catches any bypass (e.g. out-of-band
claims). Overruns are recorded truthfully — the ledger is never falsified.

### Guards

```python
from lipas.guard import Guard, GuardVerdict, LLMTarget, ToolTarget

class NoExternalOnWeekends(Guard):
    def check(self, target, rowset) -> GuardVerdict:
        if isinstance(target, ToolTarget):
            if target.tool.side_effect == SideEffectClass.EXTERNAL_WRITE:
                if datetime.now().weekday() >= 5:
                    return GuardVerdict.deny(
                        "weekend_policy", detail={"day": "weekend"}
                    )
        return GuardVerdict.allow()
```

Guards run in the pre-flight pipeline, after budget checks. First
`deny` wins. The reason and detail are folded as a typed claim.

---

## Replay

**One-line summary:** LLM calls replay deterministically; tool calls
re-execute (in this alpha version).

### LLM replay

`ReplayCursor` short-circuits the LLM harness: the recorded `Reply`
is returned without invoking the adapter, without folding new effect
claims, without touching the network.

`ReplayingAdapter` is the alternative: drive the harness normally with
recorded replies as a fake adapter. A fresh audit trail is folded into
a new store — useful for re-running against changed budgets or guards.

`strict_match=True` (default) rejects replays where `model` or
`system` don't match the recorded intent. See `examples/06_react_replay.py`
Run 3 for the rejection demo.

### Tool re-execution (alpha version)

| Class | Re-execution behavior | Safe to replay? |
|---|---|---|
| `PURE` | Recomputed; result identical | ✅ Yes |
| `READ_ONLY` | Re-fetched; result may differ | ⚠️ Idempotent, not deterministic |
| `IDEMPOTENT_WRITE` | Second write is a no-op | ✅ Safe in steady state |
| `EXTERNAL_WRITE` | **Re-fires the side-effect** | ❌ Re-sends email, re-charges card |

`examples/06_react_replay.py` uses only `PURE` tools, so its replay
is fully deterministic. For runs with `EXTERNAL_WRITE` tools, treat
replay as LLM-deterministic-only until v0.2.

`ToolReplayer` — which substitutes recorded outputs for non-`PURE`
tools — arrives in Phase 4 alongside the supervision tree and
write-ahead log. The side-effect algebra is already in place; only the
policy layer is missing.

---

## Known limits (alpha)

- **Crash-safety window:** the gap between tool execution and
  `effect_result` fold is documented but not closed. Phase 4's
  write-ahead log closes it.
- **In-memory store only:** `ClaimStore` does not persist across
  process restarts. Persistent backends in a future release.
- **No streaming to caller:** the harness assembles streamed LLM
  responses internally; the caller receives the completed `Reply`.
- **Single agent only:** multi-agent orchestration is out of scope
  until the supervision tree (Phase 4) is stable.

---

## Roadmap

| Phase | Contents | Status |
|---|---|---|
| 0 | Claim calculus, store | ✅ Done |
| 1 | Rows, basic types, tool harness | ✅ Done |
| 2 | Side-effect algebra, LLM harness, guards, replay | ✅ Done |
| 3 | ReAct agent, ToolHarness, effect log, Ollama adapter | ✅ Done |
| 4 | Supervision tree, policy DSL, ToolReplayer, write-ahead log | 🔲 Next |
| 5 | OpenAI / Anthropic adapters, CLI trace viewer, OTel export | 🔲 Planned |
| 6 | Multi-agent, persistent store, compensation | 🔲 Future |

---

## License

[Apache License 2.0](LICENSE)

