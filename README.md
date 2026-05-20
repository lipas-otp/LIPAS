# lipas

An LLM agent runtime where every effect is a recorded fact —
**so you can replay any run, audit any decision, and never get a
surprise bill.**

```python
from lipas.tools import tool, SideEffectClass, ToolHarness
from lipas.store import ClaimStore

@tool(side_effect=SideEffectClass.PURE)
def add(a: float, b: float) -> float:
    return a + b

store = ClaimStore()
harness = ToolHarness(store=store, tools=[add])

result = harness.call("add", {"a": 2, "b": 3})
print(result)                       # 5
print(len(store.claims))            # 3 — intent, result, spend
```

That's it. Three claims in an append-only store, queryable by tag,
ready to replay. No decorator soup, no config file, no daemon.

> **Status:** Single-agent, in-memory/SQLite, Ollama-tested.
> See [Known limits](#known-limits-alpha) before production use.

---

## Why lipas

Four things you get for free, just by registering a tool:

| You write | lipas gives you |
|---|---|
| `@tool(side_effect=PURE)` | retries are safe, replay is deterministic |
| `CapabilityRow(budgets={...})` | pre-flight rejection — never overrun |
| `Guard.check(...)` | typed deny reasons folded as claims |
| `harness.call(...)` | full `intent → result → spend` audit trail |

Each maps to a runnable example — see [Quickstart](#quickstart).

---

## Quickstart

```bash
pip install -e .
python examples/0x_xxx.py
```

Note, you may need to install ollama and pull models:

```bash
ollama serve &
ollama pull gemma4  # or any chat model you have
```

> If `gemma4` isn't on your version, run `ollama list` and substitute.


---

## Five-minute tour

### Claims — the only state primitive

Every event is a `Claim` folded into an append-only `ClaimStore`.
LLM call, tool result, budget spend, guard rejection — same shape,
same merge rule, same query API. Claims are idempotent: redelivery
is a no-op, order doesn't matter.

If you want to know **why** that gives you deterministic replay,
read `assist/one-calculus.md`. If you just want to use lipas,
you don't have to.

### SideEffectClass — required, not optional

```python
@tool(side_effect=SideEffectClass.PURE)
def add(a: float, b: float) -> float: ...

@tool(side_effect=SideEffectClass.EXTERNAL_WRITE)
def send_email(to: str, subject: str, body: str) -> dict: ...
```

The harness reads `side_effect` and decides — without your code —
whether to retry, whether to run guards, whether replay is safe.
Forgetting the annotation is a registration-time error, not a
runtime surprise.

### Budgets — enforced before the call fires

```python
from lipas.rows.capability import CapabilityRow

CapabilityRow(budgets={
    "tool_calls":   20.0,
    "wall_seconds": 60.0,
    "tokens_in":    10_000.0,
    "tokens_out":   2_000.0,
})
```

Pre-flight: `current_spend + estimated_cost > limit` ⇒ call is
rejected and a typed claim is folded. The fold-time gate catches
any out-of-band bypass. The ledger is never falsified.

### Guards — uniform across LLM and tool

```python
class NoExternalOnWeekends(Guard):
    def check(self, target, rowset) -> GuardVerdict:
        if isinstance(target, ToolTarget) \
                and target.tool.side_effect == SideEffectClass.EXTERNAL_WRITE \
                and datetime.now().weekday() >= 5:
            return GuardVerdict.deny("weekend_policy", detail={"day": "weekend"})
        return GuardVerdict.allow()
```

First `deny` wins. Reason and detail are folded as claims, not logged.

---

## Replay

**One-line summary:** LLM calls replay deterministically;
tool calls re-execute.

### LLM replay

- `ReplayCursor` short-circuits the harness — recorded `Reply`
  returned, no network, no token spend, no new claims.
- `ReplayingAdapter` drives the harness normally with recorded
  replies, folding a fresh audit trail. Use this to re-run against
  tighter budgets or stricter guards.
- `strict_match=True` (default) rejects replays whose `model` or
  `system` don't match the recorded intent.
  See `examples/06_react_replay.py` Run 3.

### Tool re-execution

| Class | Behavior | Safe to replay? |
|---|---|---|
| `PURE` | Recomputed; result identical | ✅ Yes |
| `READ_ONLY` | Re-fetched; result may differ | ⚠️ Idempotent, not deterministic |
| `IDEMPOTENT_WRITE` | Second write is a no-op | ✅ Safe in steady state |
| `EXTERNAL_WRITE` | **Re-fires the side-effect** | ❌ Re-sends email, re-charges card |

`examples/06_react_replay.py` uses only `PURE` tools, so its replay
is fully deterministic. For `EXTERNAL_WRITE` tools, treat replay as
LLM-deterministic-only until v0.2.

`ToolReplayer` — which substitutes recorded outputs for non-`PURE`
tools — arrives in Phase 4 alongside the supervision tree and
write-ahead log. The side-effect algebra is already in place; only
the policy layer is missing.

---

## Known limits

- **Crash-safety window:** the gap between tool execution and
  `effect_result` fold is documented but not closed. Phase 4's
  write-ahead log closes it.
- **No streaming to caller:** streamed LLM responses are assembled
  internally; the caller receives the completed `Reply`.
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