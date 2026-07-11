# The LIPAS mental model: One Agent, then a Team when needed

LIPAS has one central idea:

> Start with one **Agent**. Add a **Team** only when a task genuinely needs
> named collaborators with separate responsibility or recovery boundaries.

You do not design a graph first. You write an `Agent`, then add only the
reliability boundaries the job needs.

```text
prompt/message
     │
     ▼
Agent ── reason ──► model
     │                       │
     ├── act ───────────────► tools / external systems
     │                       │
     └── record ◄────────── intent, result, spend, decision
                              │
                         replay / supervise / inspect
```

## The two business concepts to learn

| Concept | What it means | Use it when |
|---|---|---|
| `Agent` | One assistant that reasons, calls tools, and returns an answer | Almost always: begin here |
| `Team` | A reliable mailbox for named members | Only when work is delegated between distinct responsibilities |

`@tool` is simply an Agent's hand in the outside world. It is not another
actor: it is an explicit Python capability the Agent may invoke.

Add a member directly:

```python
team.add("researcher", research_agent_or_function)
```

Everything else strengthens one of those objects:

| Reliability concern | LIPAS mechanism |
|---|---|
| May this call happen? | `Guard` |
| Can we afford it? | `budgets` / `CapabilityRow` |
| What happened? | claim tape / trace |
| Can we safely run it again? | `replay()` / `ToolReplayer` |
| Did an external operation finish? | `OperationJournal` + reconciliation |
| Should this agent stop or escalate? | `Supervisor` policy |

## Start small; add boundaries only when needed

The normal progression is deliberately ordinary Python:

```python
# 1. One agent.
agent = Agent(adapter=adapter, tools=[lookup_customer])
answer = await agent("Find C-42")

# 2. Persist and audit it.
agent = Agent(..., session_path="runs/support.db", budgets={"tokens_out": 2000})

# 3. Make tool effects explicit.
@tool(side_effect=SideEffectClass.EXTERNAL_WRITE)
def send_email(...): ...

# 4. Add a team member only when a handoff is useful.
team = Team.open("runs/team.db").add("research", researcher)
finding = await team.ask("research", "Check release risks")
```

LIPAS does not require every application to use every boundary. A local helper
may need only `Agent`; a customer-support workflow may add tools, a session,
and budgets; a high-risk operation may add a journal and human supervision.

## Do I need an Agent or a Team?

**Use one Agent by default.** It is the right shape when one coherent goal can
share one conversation, one set of tools, one budget, and one final answer.
Multiple steps, multiple tools, or even multiple model calls do not by
themselves justify a Team.

**Use a Team only at a real responsibility boundary.** Add a named member when
you need at least one of these:

- a durable handoff between independently restartable pieces of work;
- different authority, budget, or approval policy for the next piece of work;
- a separately auditable result that another member must consume;
- an explicit owner for an external operation or human escalation.

The important distinction is not “one model versus many models.” It is whether
the work needs a boundary that should survive failure, review, or delegation.

## How Agents and Teams correspond

They are two levels, not three nested abstractions:

```text
Agent                 can run alone
Team                  contains one or more named members
Team member           is usually one Agent, or sometimes a plain function
```

The recommended relation is one named Team member per Agent, because it gives
that Agent a clear audit trail and responsibility boundary. An Agent can also
run outside every Team. A plain function may be a Team member when no model is
needed—for example, a deterministic paper-index query.

## What the tape is—and is not

The claim tape is an audit and recovery record, not a magical memory system.
It records why an action was allowed, rejected, replayed, or escalated. Your
application remains responsible for domain databases, user profiles, search,
and long-term knowledge.

## Relation to OTP

The inspiration is small and practical: named members, messages, supervision,
and visible recovery state. LIPAS does not claim BEAM process isolation or try
to reproduce Erlang/OTP; it applies those reliability questions to Python
agents and external effects.
