# lipas

An auditable Python agent runtime: every LLM call, tool invocation,
rejection, replay choice, and budget charge is an append-only claim. This
makes a run inspectable and replayable without hiding side effects behind an
opaque framework.

> **Status: alpha.** Single-agent ReAct, SQLite persistence, side-effect-aware
> tool replay, Ollama and injected-client Anthropic adapters are implemented.
> OpenAI Responses, auditable handoffs, and recovery-oriented external-operation
> journals are available. Provider-level exactly-once remains impossible without
> provider idempotency and reconciliation support.

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

## Quickstart: natural Python agent code

```bash
pip install -e '.[ollama,dev]'
ollama serve
ollama pull qwen2.5
```

```python
import asyncio
from lipas import Agent
from lipas.adapter import OllamaAdapter
from lipas.tools import SideEffectClass, tool

@tool(side_effect=SideEffectClass.READ_ONLY)
def lookup_customer(customer_id: str) -> str:
    """Look up a customer without changing external state."""
    return f"customer={customer_id}"

agent = Agent(
    adapter=OllamaAdapter(),
    model="qwen2.5",
    instructions="Answer concisely; use tools when useful.",
    tools=[lookup_customer],
    session_path="runs/support.db",  # omit for in-memory use
)

result = asyncio.run(agent("Find customer C-42"))
print(result.text)
agent.close()
```

`Agent` is deliberately thin. Use `DeclarativeAgent`, `LLMHarness`, and
`ToolHarness` directly when you need custom rows, guards, replay wiring, or a
different behaviour loop.


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

### Portable Markdown skills

`lipas` reads `SKILL.md` with YAML front matter. It requires only the portable
`name` and `description` fields and preserves all other front matter verbatim.
That means a skill directory can be shared with Claude Code-style skills and
Codex/ChatGPT-oriented Markdown skill tooling; fields such as `allowed-tools`,
`user-invocable`, `metadata`, or framework-specific fields are not rejected or
silently discarded. LangGraph applications can consume the same Markdown files
and inject their body into their own prompts.

```markdown
---
name: finance-review
description: Review financial changes carefully.
---
Always state assumptions and flag irreversible actions.
```

```python
from lipas import SkillRegistry, discover_skills

skills = SkillRegistry(discover_skills("./skills"))
agent = Agent(..., instructions="You are helpful.", skills=skills)
```

Skills are appended to the system prompt in deterministic path order. LIPAS
does not execute tool permissions from Markdown front matter: tool authority
continues to be declared and audited in Python, preventing prompt text from
silently expanding side-effect permissions.

### Readable audit logs

The claim tape remains the source of truth, but it can now be rendered for
people or exported to any log pipeline without a provider SDK:

```python
from lipas import render_trace, write_jsonl

print(render_trace(rowset.store))       # concise Markdown table
with open("run.jsonl", "w", encoding="utf-8") as out:
    write_jsonl(rowset.store, out)       # one structured claim per line
```

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

**One-line summary:** LLM replies replay deterministically; tool replay is
policy-driven and defaults to substituting a recorded result rather than
touching the live system.

### LLM replay

- `ReplayCursor` short-circuits the harness — recorded `Reply`
  returned, no network, no token spend, no new claims.
- `ReplayingAdapter` drives the harness normally with recorded
  replies, folding a fresh audit trail. Use this to re-run against
  tighter budgets or stricter guards.
- `strict_match=True` (default) rejects replays whose `model` or
  `system` don't match the recorded intent.
  See `examples/06_react_replay.py` Run 3.

### Tool replay

| Class | Behavior | Safe to replay? |
|---|---|---|
| `PURE` | Recomputed; result identical | ✅ Yes |
| `READ_ONLY` | Re-fetched; result may differ | ⚠️ Idempotent, not deterministic |
| `IDEMPOTENT_WRITE` | Second write is a no-op | ✅ Safe in steady state |
| `EXTERNAL_WRITE` | **Re-fires the side-effect** | ❌ Re-sends email, re-charges card |

`ToolReplayer` is implemented. The default `replay(...)` session uses strict
tape substitution: a recorded tool output is returned without live execution.
`BEST_EFFORT` may re-execute missing calls; `LIVE_REROUTE` is explicit and
refuses external writes unless the caller opts in. This is replay safety, not
an exactly-once delivery guarantee.

---

## Current limitations and compatibility

- **External effects:** `OperationJournal` persists a caller-provided idempotency
  key before submission and marks interrupted submissions as `uncertain`.
  Reconciliation remains mandatory; exactly-once is only as strong as the
  provider's idempotency contract.
- **Streaming:** `LLM.stream(...)` yields normalized `Delta`, `ToolUseDelta`,
  and terminal `Done` events while preserving the final audit record. A stream
  is not retried after visible output.
- **Multi-agent:** `Mailbox` and `AgentOrchestrator` provide named, auditable,
  at-least-once handoffs. Receivers must use the stable message id for replay
  and idempotency; distributed ownership is deliberately explicit.
- **Provider support:** Ollama, injected-client Anthropic, and the optional-SDK
  OpenAI Responses adapter are supported.

---

## Roadmap

1. **Compatibility and test convergence:** finish migration of the remaining
   pre-P3 test assertions and unify provider content shapes.
2. **OpenAI hardening:** add provider contract fixtures, current pricing data,
   and optional SDK convenience construction.
3. **Operation recovery:** add provider-specific reconciliation and explicit
   compensation adapters on top of `OperationJournal`.
4. **Multi-agent hardening:** add delegated capability policies and replay
   fixtures across mailbox delivery boundaries.
5. **Supervisor projection:** replace the deferred
   `lipas.calculus_supervisor` tripwire with a tag-aware projection API and
   migrate retry/escalation queries from O(N) log scans to that projection.

---

## License

[Apache License 2.0](LICENSE)
