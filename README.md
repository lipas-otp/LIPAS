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
AgentCoordinator = ExecutionStore-backed ownership across named members
Team   = the legacy mailbox compatibility facade
```

> **0.40.0 local operator and recovery beta.** `AgentCoordinator` now maps every
> handoff to one deterministic Task/Run under the existing `ExecutionStore`.
> Sequential, RoundRobin, bounded parallel, map/reduce, durable Selector, and
> bounded Swarm policies therefore share one lease, cancellation, event, and
> terminal-replay authority instead of creating a graph or mailbox database.
> Aggregate event handles, shared budget/capability policy, durable Agent
> members, dependency-free LangGraph/AutoGen handoff adapters, and the
> scaffold/conformance SDK are included in this boundary.

> **0.38.0 SQLite concurrency and evidence store.** Durable Runs can now execute
> concurrently without a Runtime-wide lock. Every core store shares one WAL,
> timeout, transaction, and failure policy; Claim tapes support concurrent
> idempotent append, bounded pages, and rebuildable projection snapshots. The
> 18 Scenarios and 17 instruction-only Skills from 0.35 remain available.

> **0.40.0 is now shipped.** `LocalWebOperator` exposes the same Task/Run/
> Interrupt/event projections over a loopback HTTP boundary; `/` serves a small
> dependency-free browser projection, mutation routes
> require a bearer token. `FaultCampaign` and `benchmark_execution_store()`
> provide deterministic recovery drills and local transition measurements.
> The hardening pass adds bounded task detail (diffs, artifacts, reports),
> safe task cancellation/approval aliases, reusable fault plans, and a
> multi-connection SQLite contention probe.

## One system, two layers

```text
LIPAS business and product layer (0.40.0 SQLite-first runtime)
  Scenario / Skill / Task / Workspace / Approval / Artifact / Local Web operator
                              │
                              ▼
LIPAS Python runtime (available today)
  Agent / Tool / Effect / Guard / Budget / Replay / Execution / Operation
  AgentCoordinator / legacy Team
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
    coordinator = runtime.coordinator()
    operator = runtime.operator(operator_token="change-me")
```

The global control and product tables live in `.lipas/workspace.db`. Each Run
keeps its Claim/Effect tape under `.lipas/runs/<run-id>/claims.db`, preserving
budget and replay isolation without creating another Task/Run state machine.
SQLite remains the deliberate local engine: WAL keeps readers moving, write
transactions stay short, `synchronous=FULL` protects the durable default, and
per-Run evidence files remove unnecessary global hotspots. It is a bounded
single-writer design, not a disguised distributed database; see
[SQLite storage and concurrency](docs/sqlite-storage.md).
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
ordinary Python Agent / Tool / Execution / Operation / AgentCoordinator
                 │
                 ▼
           append-only Claims
                 ├── history:    decisions and handoffs
                 ├── capability: budgets and spend
                 └── effect:     intent, result, lineage
```

That one evidence tape is why the pieces fit together rather than becoming
unrelated features: guards and budgets decide before a call; replay substitutes a
recorded result; supervision records its recommendation; a coordinator handoff has a
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
budgets, replay, durable recovery, writes, Skills, coordination, and then complete
runnable projects. The numbered
[example course](examples/README.md) remains the reference collection for
focused scenarios.

## When to add more

Keep one Agent when one coherent goal shares one conversation, tool set,
budget, and answer. Multiple steps or multiple tools do not require multiple
Agents.

Add an `AgentCoordinator` only when work needs a separate owner or recovery boundary: an
independently restartable task, a different authority/budget, a separately
audited result, or a human/external-operation handoff. A member is usually
an Agent, but can be a plain async function. In a normal script:

```python
from lipas import AgentCoordinator


async def researcher(prompt):
    return {"finding": f"researched: {prompt}"}


async def main():
    with AgentCoordinator.open("runs/coordination.db") as coordinator:
        coordinator.add("research", researcher)
        finding = await coordinator.handoff(
            "research", "check release risks",
            coordination_id="release-risk-v1",
        )
        print(finding.value)
```

Use legacy `Team` only when maintaining the mailbox API. New orchestration and
its exact recovery limits are documented in
[Multi-Agent coordination](docs/multi-agent.md).

## Reliability, only when you ask for it

| Add | LIPAS provides |
|---|---|
| `@tool(side_effect="read_only")` | explicit replay and retry safety class |
| `session="runs/app.db"` | durable trace of intent, result, spend, and decisions |
| `budgets={...}` | pre-flight rejection before a known limit is exceeded |
| `tool_guards=[...]` | recorded policy denial before a live call |
| `OperationJournal` | idempotency-key persistence and reconciliation state for an external write |
| `AgentCoordinator` | deterministic handoff Runs, bounded policies, cancellation, and terminal replay |
| legacy `Team` | mailbox-compatible at-least-once handoff for existing applications |
| `ExecutionStore` + `Agent.run_durable()` | leased ReAct checkpoints, approval interruption, cancellation, and crash recovery |
| `CoordinationEventHandle` | reconnectable aggregate events without a second global sequence |
| `LocalWebOperator` | local Task/Run/Interrupt projection with token-protected mutations |
| `FaultCampaign` / `run_fault_matrix()` | isolated named recovery fixtures without hidden retries |
| `benchmark_execution_store()` | bounded SQLite transition and contention measurements |
| `ExtensionManifest` / `run_conformance()` | offline provenance, connector-safety, and version checks |

The record is not a magic memory system and LIPAS is not a graph/workflow DSL.
Your application still owns its domain data, business rules, and user-facing
workflow.

`Agent.run(...)` returns a final result. `Agent.stream(...)`, `Session`, and
durable event cursors expose normalized run/model/tool events; adapters that
produce real deltas surface them through the same `AgentEvent` protocol.

## Local workspace tasks (0.40 product beta)

The historical 0.31.0 slice made the local-task vertical use the unified runtime by
default. The current 0.40 product beta
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
parallel Tasks from sharing a budget projection or one hot evidence sequence.

For Git workspaces, staging snapshots tracked and non-ignored untracked files;
for other workspaces it snapshots ordinary files. Secret-like paths and text
content, symlinks, generated cache directories, and files above the per-file
limit are excluded; aggregate file/size limits fail closed.
This first snapshot backend is intentionally bounded; dependency directories
excluded by Git may need installation or a later read-only mount design for
verification.

The current boundary is explicit: `AgentCoordinator` is the new
ExecutionStore-backed orchestration standard library; legacy `Team` keeps its
mailbox only for compatibility and is not a second Task/Run API for new code.
An ordinary Agent member shares context and causality, but does not implicitly
become durable. A SQLite-backed Agent member is different: its already-claimed
handoff Run directly carries the Agent checkpoint, Approval/Input Interrupt,
and Effect tape, so resume and replay do not double-claim. An ambiguous
model/tool phase timeout is marked recovery-required; the operator must
reconcile the Effect/provider, record an observation/evidence object, and
explicitly reopen the Run before resume. `LLMHarness.reconcile_orphan()` and
`ToolHarness.reconcile_orphan()` close intent-only Effects without issuing an
unverified second request.

## Experimental interoperability

LIPAS develops its own task product first. LangGraph, MCP-server, and
OpenCrew/OpenClaw adapters are experimental compatibility samples: they are
not core product surfaces and carry no compatibility commitment. The HTTP and
MCP clients are first-party capability boundaries. See the
[integration guide](docs/integrations.md) for both.

## Business Skills and Scenarios

A Skill is a portable instruction file; a `BusinessScenario` composes the
smallest relevant Skill bundle, lifecycle, and required Tool contracts. Neither
grants authority. Tools remain the only executable capability, while durable
Runs own approval, recovery, and evidence. LIPAS packages 17 Skills and 18
Scenarios across files, engineering, office, personal writing, and scoped
connector workflows:

```python
from lipas import Agent, ScenarioRegistry

scenarios = ScenarioRegistry.from_names([
    "coding-change",
    "release-readiness",
])
skills = scenarios.skill_registry(
    paths=["skills/repository-conventions"],
)

agent = Agent.ollama(
    skills=skills,
)
```

Nothing is auto-selected, so catalog growth does not inflate unrelated
prompts. Inspect recipes and capability boundaries without running a model:

```bash
lipas skill list
lipas scenario list
lipas scenario show email-delivery
lipas scenario check email-delivery --factory connectors:email_tools
lipas chat --scenario office-report --once "Draft a project update"
lipas task start . "repair the parser" --scenario coding-change
```

Connector Scenarios are contracts, not built-in account access. Email delivery
still requires an application-supplied `send_email` Tool, explicit scope,
preview approval, idempotency, provider evidence, and uncertain-result
reconciliation. See [business Skills, Scenarios, and capabilities](docs/business-skills.md).

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
  of claims, effects, durable runs, replay, and external operations.
- [Multi-Agent coordination](docs/multi-agent.md) — deterministic handoffs,
  coordination policies, cancellation, replay, and remaining limits.
- [SQLite storage and concurrency](docs/sqlite-storage.md) — WAL policy,
  concurrent Run boundaries, evidence paging/snapshots, and honest scale limits.
- [Roadmap](docs/roadmap.md) — how the runtime and local task workbench advance
  as one LIPAS project.
- [Strategy](docs/strategy.md) — LIPAS's position alongside LangGraph and
  AutoGen, current gaps, architectural guardrails, and the path to 0.40.
- [OpenAI-compatible model endpoints](docs/model-providers.md) — connect an
  explicit Chat Completions URL, model, and API key without hidden fallback.
- [Experimental integrations](docs/integrations.md) — optional LangGraph,
  MCP-server, OpenCrew/OpenClaw, and Action Gateway compatibility samples.
- [Installation, onboarding, and design-partner validation](docs/onboarding.md)
  — doctor, offline tour, migration, backup, external capability readiness,
  and the repeatable recovery/reconciliation pilot protocol.
- [0.39/0.40 coordination and operator contracts](docs/multi-agent.md) — aggregate
  event handles, shared policy, framework boundaries, local operator, and drills.
- [Examples](examples/README.md) — focused, runnable scenarios from the high
  level API down to the lower-level harnesses.
- [Changelog](CHANGELOG.md) — release history.

## License

[Apache License 2.0](LICENSE)
