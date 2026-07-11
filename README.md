# lipas

An auditable Python agent runtime: every LLM call, tool invocation,
rejection, replay choice, and budget charge is an append-only claim. This
makes a run inspectable and replayable without hiding side effects behind an
opaque framework.

LIPAS takes a small cue from OTP-style systems: isolate work into explicit
agents, communicate through messages, make supervision visible, and treat
failure recovery as a first-class part of the runtime. It is a Python agent
runtime, not a BEAM replacement.

> **Status: public beta — 0.9.3.** ReAct is the default single-agent runner;
> named multi-agent handoffs run through a durable leased mailbox. SQLite
> persistence, side-effect-aware tool replay, Ollama, injected-client Anthropic,
> and OpenAI Responses are available. Provider-level exactly-once remains
> impossible without provider idempotency and reconciliation support.

---

## Why lipas

**LIPAS is an auditable agent runtime.** You write ordinary Python
code; LIPAS adds explicit side effects, budgets, replay, supervision, and
durable handoffs only where you need them. Start with `Agent`; add `Team` only
when work must be delegated.

```text
Agent       = one assistant that thinks and uses tools
@tool       = an explicit capability/effect
Team        = named assistants/functions communicating through a durable mailbox
```

Read the [Agent and Team mental model](docs/mental-model.md) before choosing an
API. It is the shortest route to understanding how the pieces fit together.

### Reliable-core guarantees

Within one Agent, LIPAS closes the normal reason/act loop around the same
audited rowset: guards decide authority before calls, capability budgets gate
spend, effect claims record intent/result, and replay policy decides what may
touch the live world. `Supervisor` policies are now available directly on
`Agent`, so termination and escalation recommendations are recorded and can
halt the normal ReAct lifecycle.

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

`Agent` is deliberately thin and represents one ReAct loop. Use
`DeclarativeAgent`, `LLMHarness`, and `ToolHarness` directly when you need
custom rows, guards, replay wiring, or a different behaviour loop.

For a small team, the same ordinary-Python style is enough:

```python
from lipas import Team

async def researcher(prompt):
    return {"finding": f"researched: {prompt}"}

team = Team.open("runs/team.db").add("research", researcher)
finding = await team.ask("research", "release risks")
team.close()
```

### Team handoff

`Team` gives named members a durable mailbox. Delivery is at-least-once: a
crashed member's lease expires and the message can be reclaimed. An
acknowledged message cannot be run again. Pass `message_id=` whenever a
handoff must retain a stable idempotency/replay key.

### Supervised agents and team members

Each named Team member can be an ordinary async Python function or an `Agent`-
compatible callable. There is no graph or workflow DSL to learn. Attach a
`SupervisorGate` only when that member needs advisory retry, halt, or human
escalation policy. `project_supervisor(rowset.store)` then gives application
code an indexed view of the recommendations.


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
  key before submission. A crashed/failed submission is `uncertain` and cannot
  be resent under the same key until reconciliation; a proven absent provider
  operation becomes `failed` and requires an intentional new key. Exactly-once
  remains only as strong as the provider's idempotency contract.
- **Streaming:** `LLM.stream(...)` yields normalized `Delta`, `ToolUseDelta`,
  and terminal `Done` events while preserving the final audit record. A stream
  is not retried after visible output.
- **Multi-agent:** `Mailbox` and `AgentOrchestrator` provide named, auditable,
  at-least-once handoffs with claim leases, acknowledgement ownership, release
  on handler failure, and expired-lease recovery. Receivers must use the stable
  message id for replay and idempotency; distributed ownership is explicit.
- **Provider support:** Ollama, injected-client Anthropic, and the SDK-optional
  OpenAI Responses adapter are supported.
- **Team boundaries:** authority and budgets are enforced within each agent
  cell today. Delegated cross-cell capability/budget policy and mailbox replay
  are not yet part of the default runtime contract.
- **Provider operations:** `OperationJournal` supplies durable state and
  reconciliation semantics, but provider-specific external-write tools must
  explicitly pass its idempotency key until first-party adapters are added.

---

## Roadmap

1. **Provider hardening:** add recorded OpenAI/Anthropic/Ollama fixtures for
   rate limits, malformed responses, interruption, current pricing, and SDK
   convenience construction where it adds value.
2. **Operation integrations:** add provider-specific reconciliation and
   compensation adapters on top of `OperationJournal`.
3. **Multi-agent policy:** add delegated capability boundaries, mailbox replay
   fixtures, and cross-agent budget policy.
4. **1.0 convergence:** stabilize the normalized adapter types, claim/session
   migration rules, and the public Python API without introducing a DSL.

`Team` and `project_supervisor(...)` provide the supervision path today.
The remaining work is policy enforcement across agent boundaries and API
stability, not another workflow engine.

For a complete first project using ordinary Python functions, an agent, replay,
and a mailbox team member, see [Getting started](docs/getting-started.md).
For focused runnable scenarios, see the [examples guide](examples/README.md).

---

## License

[Apache License 2.0](LICENSE)
