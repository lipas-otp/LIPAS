# LIPAS

> Language: [English](README.md) | [中文](README.zh-CN.md)

LIPAS is a local trustworthy task agent for individuals and small teams. It
works inside a selected workspace, asks before risky actions, survives
interruptions, verifies the result, and delivers evidence instead of merely
ending a chat. Its Python runtime is the internal reliability foundation and
an optional advanced embedding surface.

```text
Agent  = one assistant that thinks and uses tools
@tool  = an explicit capability with a declared side effect
Team   = a durable handoff between named assistants or functions
```

> **0.32.0 compatible model endpoints alpha.** The unified Runtime now connects
> to OpenAI-compatible Chat Completions providers through an explicit URL,
> model, and API key, including the compatible surfaces offered by Volcengine
> Ark, Alibaba Bailian, Tencent Hunyuan, and DeepSeek. No provider/model
> fallback is implicit; the 0.31 storage and per-Run evidence boundaries remain.

## One system, two layers

```text
LIPAS local task workbench (0.32.0 compatible-endpoint alpha)
  Task / Workspace / Approval / Artifact / Task CLI / future Local Web
                              │
                              ▼
LIPAS Python runtime (available today)
  Agent / Tool / Effect / Guard / Budget / Replay / Execution / Operation / Team
```

The workbench is the first-party way to use LIPAS for workspace tasks such as
inspecting files, making controlled changes, running checks, and delivering a
report. The Python API remains independently useful for applications that need
their own domain model or interface. Both layers share the same Effects and
audit record; the workbench does not create a second execution model.

Applications can open those layers through one lifecycle owner:

```python
from lipas import LIPASRuntime

with LIPASRuntime.open(".lipas") as runtime:
    runtime.execution
    runtime.claims
    runtime.operations
    runtime.handoffs
    runtime.sessions
    runtime.artifacts
```

The global control and product tables live in `.lipas/workspace.db`. Each Run
keeps its Claim/Effect tape under `.lipas/runs/<run-id>/claims.db`, preserving
budget and replay isolation without creating another Task/Run state machine.
Legacy workspaces are never changed on open: inspect and migrate them with
`lipas migrate plan` and `lipas migrate apply --yes`. Migration and rollback
are copy-on-write, preserve verified backups, account for SQLite WAL state,
and refuse an active Runtime or SQLite writer. A dead process's stale
migration lock can be recovered; a live lock is never removed.

Agent calls, conversational Sessions, and durable Runs also share
`RunContext`, `AgentEvent`, cancellation, deadlines, and event cursors. See
[Unified runtime contracts](docs/runtime-contracts.md) for the authority and
compatibility boundaries.

## The one idea underneath

LIPAS does not ask you to write a graph or a special workflow language. You
write ordinary Python; an `Agent` calls a model and ordinary `@tool` functions.
The runtime admits the reliability-relevant parts of that work as immutable
stored snapshots called **Claims**. A caller may still edit a Claim object it
has not submitted; mutation cannot rewrite a Claim after the store accepts it.

A **fold** accepts each stable claim once, validates it, and updates small
derived views of the same record: history answers what happened, capability
enforces spend limits, and effects record `intent → result | rejection`.

```text
ordinary Python Agent / Tool / Execution / Operation / Team
                 │
                 ▼
           append-only Claims
                 ├── history:    decisions and handoffs
                 ├── capability: budgets and spend
                 └── effect:     intent, result, lineage
```

That one evidence tape is why the pieces fit together rather than becoming
unrelated features: guards and budgets decide before a call; replay substitutes a
recorded result; supervision records its recommendation; a Team handoff has a
stable causal id; an external write can be reconciled against its recorded
intent; execution control stores mirror their transitions through recoverable
outboxes. Your code remains natural Python because LIPAS records the boundary
around it instead of replacing its control flow.

For the precise guarantees and limits, read the short [Execution
model](docs/execution-model.md).

## Start here

```bash
pip install 'lipas[ollama]'
ollama pull gemma4:12b
```

```python
from lipas import Agent, tool


@tool(side_effect="read_only")
def lookup_customer(customer_id: str) -> str:
    """Look up a customer without changing external state."""
    return f"customer={customer_id}"


with Agent.ollama(
    tools=[lookup_customer],
    instructions="Use tools when useful; answer concisely.",
    session="runs/support.db",  # omit for in-memory use
) as agent:
    result = agent.ask("Find customer C-42")
    print(result.text)
```

`agent.ask(...)` is the normal-script API. In an async application, use
`await agent.run(...)`. The first runnable example is
[`examples/01_first_agent.py`](examples/01_first_agent.py).

For an OpenAI-compatible `/chat/completions` endpoint, keep the credential out
of source control and provide the route explicitly:

```bash
pip install 'lipas[compatible]'
```

```python
import os

from lipas import Agent

agent = Agent.openai_compatible(
    model="deepseek-chat",
    base_url="https://api.deepseek.com",
    api_key=os.environ["DEEPSEEK_API_KEY"],
)
```

The same factory covers compatible routes from Volcengine Ark, Alibaba
Bailian, Tencent Hunyuan, OpenAI, private gateways, and other providers.
Non-streaming is the compatibility-first default; SSE is explicit, and
provider/model-specific capabilities remain unknown until registered. See
[OpenAI-compatible model endpoints](docs/model-providers.md) for URLs, secure
CLI use, streaming, tools, error semantics, and exact compatibility limits.
Before running an Agent, `lipas model check --base-url ... --model ...` validates
the configuration without network access; add explicit `--live` only for an
intended, potentially billable provider probe.

New to LIPAS? Read [LIPAS, step by step](docs/tutorial.md) as a small,
linear introduction: first Agent, tools, side effects, results, sessions,
budgets, replay, durable recovery, writes, Skills, Teams, and then complete
runnable projects. The numbered
[example course](examples/README.md) remains the reference collection for
focused scenarios.

## When to add more

Keep one Agent when one coherent goal shares one conversation, tool set,
budget, and answer. Multiple steps or multiple tools do not require a Team.

Add a `Team` only when work needs a separate owner or recovery boundary: an
independently restartable task, a different authority/budget, a separately
audited result, or a human/external-operation handoff. A Team member is usually
an Agent, but can be a plain async function. In a normal script:

```python
from lipas import Team


async def researcher(prompt):
    return {"finding": f"researched: {prompt}"}


with Team.open("runs/team.db") as team:
    team.add("research", researcher)
    finding = team.ask_sync("research", "check release risks")
```

## Reliability, only when you ask for it

| Add | LIPAS provides |
|---|---|
| `@tool(side_effect="read_only")` | explicit replay and retry safety class |
| `session="runs/app.db"` | durable trace of intent, result, spend, and decisions |
| `budgets={...}` | pre-flight rejection before a known limit is exceeded |
| `tool_guards=[...]` | recorded policy denial before a live call |
| `OperationJournal` | idempotency-key persistence and reconciliation state for an external write |
| `Team` | durable, at-least-once handoff with leases and acknowledgement |
| `ExecutionStore` + `Agent.run_durable()` | leased ReAct checkpoints, approval interruption, cancellation, and crash recovery |

The record is not a magic memory system and LIPAS is not a graph/workflow DSL.
Your application still owns its domain data, business rules, and user-facing
workflow.

`Agent.run(...)` returns a final result. `Agent.stream(...)`, `Session`, and
durable event cursors expose normalized run/model/tool events; adapters that
produce real deltas surface them through the same `AgentEvent` protocol.

## Local workspace tasks (product alpha)

The 0.31.0 release makes the local-task vertical use the unified runtime by
default. It
reuses the same `ExecutionStore` and Effect tape from a separate product layer;
Workspace, Artifact, and Report concepts do not leak back into the Agent
runtime. State defaults to `~/.lipas` and can be changed with `LIPAS_HOME` or
`--home`.

```bash
lipas task start . "fix the documentation error and run relevant tests"
lipas task submit . "update two local reports and verify them"
lipas task worker --max-concurrency 2
lipas task list
lipas task approvals
lipas task show <task-id>
lipas task approve <approval-id>
lipas task diff <task-id>
lipas task apply <task-id>
# or: lipas task discard <task-id>
lipas task events <task-id>
lipas task report <task-id>
lipas doctor
lipas audit
lipas tour --offline
```

`doctor` reports storage health and full runtime readiness separately and
executes a bounded launch probe of the default sandbox. `audit` is read-only by
default: it checks storage invariants and explicitly reports Claim lint as
`not_run`. `audit --repair` opens the Runtime to repair recoverable audit
outboxes and lint both global evidence and every registered per-Run Claim tape.

CLI Tasks modify a per-Run staging workspace rather than the selected
workspace. Staged file writes do not interrupt one-by-one; commands still wait
for durable approval and resume the same checkpointed Run. The first tool set is limited to contained workspace files,
read-only Git status/diff, and an allowlisted command runner without shell
expansion. Reports show recorded changes, verification commands, exit states,
and unresolved risks. A Python factory may accept `tools`, `session_path`, and
`workspace`; without one, the task CLI uses local Ollama unless an explicit
OpenAI-compatible `--base-url`, model, and key environment variable are given.

Command execution defaults to `--sandbox auto`, which uses Bubblewrap with a
minimal filesystem and no network and fails closed when that isolation cannot
be established. `--sandbox local` is an explicit unsafe fallback for trusted
code. `task events` prints the durable product history as stream-friendly
JSONL, including approvals, artifacts, verifications, run states, and reports.
Within one model turn, independent `pure`/`read_only` tools may run in parallel;
writes and policy/accounting-sensitive calls remain serial. Heartbeats keep the
run lease alive, and stable Effects restore completed calls after interruption.

After a Run completes, `task diff` shows its complete staged file ChangeSet.
`task apply` is the explicit delivery approval; it verifies that every original
path still matches the snapshot baseline before applying anything. External
workspace drift fails closed. Applying is per-file atomic and retryable if the
process stops between files. `task discard` removes an unapplied stage without
changing the workspace. Reports expose `delivery: ready|applied|discarded`.

`task submit` persists work without tying it to the submitting process.
`task worker` is the local persistent dispatcher: it runs several Tasks with a
bounded concurrency, reclaims expired leases after restart, and releases a slot
when a Run waits for approval. `task approvals` is the durable operator inbox.
Use `task approve <id> --defer-resume` to queue
the allowed Run for a worker. Each Run has its own Claim/Effect session while
the global `ExecutionStore` remains the authoritative queue, preventing
parallel Tasks from sharing budget or single-writer journal state.

For Git workspaces, staging snapshots tracked and non-ignored untracked files;
for other workspaces it snapshots ordinary files. Secret-like paths and text
content, symlinks, generated cache directories, and files above the per-file
limit are excluded; aggregate file/size limits fail closed.
This first snapshot backend is intentionally bounded; dependency directories
excluded by Git may need installation or a later read-only mount design for
verification.

The 0.31 boundary is explicit: legacy `Team` remains a compatibility
orchestration layer whose mailbox ownership has not yet moved completely onto
`ExecutionStore`, and broad automatic recovery after every model/tool phase
timeout is still roadmap work. Durable cancellation, approval resume, orphan
detection, and completed-Effect restoration are available today.

## Experimental interoperability

LIPAS develops its own task product first. LangGraph, MCP-server, and
OpenCrew/OpenClaw adapters are experimental compatibility samples: they are
not core product surfaces and carry no compatibility commitment. See the
[experimental integration guide](docs/integrations.md) only when an existing
system genuinely needs such an entry point.

## Reusable Skills

A Skill is a portable `SKILL.md` instruction file: it captures how an Agent
should approach recurring work without granting it any new authority. Tools
remain the only executable capability. Start by copying one of the ready-made
[example skills](examples/skills), then point an Agent at its directory:

```python
from lipas import Agent
from my_app.tools import search_papers

agent = Agent.ollama(
    tools=[search_papers],
    skills="skills/research-brief",
)
```

The research, support-triage, daily-brief, and safe-external-actions Skills are
deliberately small templates: edit them for your own standards rather than
treating prompt text as a permission system.

## Try and inspect

The optional CLI is for trying an ordinary Python Agent and inspecting its
session; it is not a second configuration language.

```bash
pip install 'lipas[ollama,cli]'
lipas init support-demo --model gemma4:12b
cd support-demo
lipas chat --factory agent:build_agent
lipas trace runs/chat.db
lipas effects runs/chat.db
```

From a source checkout before installation, use `python -m lipas.cli` instead.
The session file is created automatically. Ollama is local but accessed through
its local HTTP service; a timeout means the local daemon/model did not answer
in time, not that LIPAS contacted the internet.

## Read only what you need

- [LIPAS, step by step](docs/tutorial.md) — the recommended linear tutorial,
  from one Agent through complete projects.
- [Execution model](docs/execution-model.md) — the exact semantics and limits
  of claims, effects, durable runs, replay, external operations, and Teams.
- [Roadmap](docs/roadmap.md) — how the runtime and local task workbench advance
  as one LIPAS project.
- [OpenAI-compatible model endpoints](docs/model-providers.md) — connect an
  explicit Chat Completions URL, model, and API key without hidden fallback.
- [Experimental integrations](docs/integrations.md) — optional LangGraph,
  MCP-server, OpenCrew/OpenClaw, and Action Gateway compatibility samples.
- [Examples](examples/README.md) — focused, runnable scenarios from the high
  level API down to the lower-level harnesses.
- [Changelog](CHANGELOG.md) — release history.

## License

[Apache License 2.0](LICENSE)
