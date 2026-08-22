# CLI: prototype and inspect

The `lipas` command is a thin companion to the Python library. It does not
introduce a workflow DSL or a second agent configuration language: it either
creates ordinary Python, invokes an ordinary Python `Agent` factory, or reads
the same SQLite claim session used by the library.

## Start a prototype

```bash
pip install 'lipas[ollama]'
lipas init support-demo --model gemma4:12b
cd support-demo
ollama pull gemma4:12b
lipas chat --factory agent:build_agent
```

`init` creates an editable `agent.py`, not a private configuration file. Add
tools, policies, or application code there as the prototype grows.

The `lipas` executable is installed with the package. While working directly
from a source checkout before installation, use `python -m lipas.cli` in its
place, for example `python -m lipas.cli chat --model gemma4:12b`.

For a one-off, tool-less local conversation:

```bash
lipas chat --model gemma4:12b --session runs/try.db
```

For an OpenAI-compatible Chat Completions provider, pass the route and the
name of a credential environment variable. The CLI intentionally has no
plaintext API-key flag:

```bash
export DEEPSEEK_API_KEY='...'
lipas chat \
  --base-url https://api.deepseek.com \
  --api-key-env DEEPSEEK_API_KEY \
  --model deepseek-chat \
  --session runs/deepseek.db
```

`--base-url` and the Ollama-specific `--host` are mutually exclusive.
Non-streaming is the compatibility default; `--model-streaming` explicitly
selects SSE. Compatible-only flags fail explicitly when `--base-url` is absent
or a custom `--factory` owns construction. See
[OpenAI-compatible model endpoints](model-providers.md) for
Volcengine Ark, Alibaba Bailian, Tencent Hunyuan, DeepSeek, OpenAI, custom
gateways, token-limit fields, and exact capability boundaries.

Validate configuration without contacting the provider:

```bash
lipas model check \
  --base-url https://api.deepseek.com \
  --api-key-env DEEPSEEK_API_KEY \
  --model deepseek-chat \
  --json
```

Add `--live` only when one explicit external, potentially billable model probe
is intended. The live check reports normalized usage or a classified,
credential-redacted failure. It is a direct transport diagnostic and does not
write a persistent Agent Claim/Effect session.

For a trusted local gateway that deliberately has no authentication, replace
`--api-key-env ...` with explicit `--no-api-key`. The two credential modes are
mutually exclusive. `--prompt` also requires `--live`, so a dry check never
silently ignores probe input.

Inside chat, `:trace` renders the current claim tape, `:effects` shows orphan
and rejected effects, and `:quit` exits. `--once "…"` is useful for scripts.
The session file is created automatically; do not `touch` it first.

## Check and migrate runtime state

First-party task commands use `LIPASRuntime` and the schema-v2
`workspace.db`. Diagnostics are read-only unless repair or migration is
explicitly requested:

```bash
lipas doctor --home ~/.lipas
lipas doctor --home ~/.lipas --json
lipas audit --home ~/.lipas
lipas audit --home ~/.lipas --repair
lipas tour --offline
```

`tour --offline` uses a deterministic built-in adapter and a disposable
workspace. It walks through missing Input, a separate write Approval, durable
resume, event catch-up, Artifact/Report creation, and audit without requiring
a provider or touching the user's project.

`doctor` separately reports storage health and runtime readiness. Its default
sandbox check executes a bounded probe; finding `bwrap` on `PATH` is not
reported as operational unless the requested isolation can actually start.
The default `audit` checks storage invariants without writing. Its JSON uses
`"claim_audit": "not_run"` and `"claim_issues": null`; pass `--repair` to
open the Runtime, lint persistent Claims, and repair recoverable audit mirrors.

An existing v1 workspace is never changed merely because a newer LIPAS opens
it. Stop active workers, inspect the exact source files and row counts, then
apply the copy-on-write migration:

```bash
lipas migrate plan --home ~/.lipas
lipas migrate apply --home ~/.lipas --yes
lipas migrate verify --home ~/.lipas
```

The migration retains the original databases and an additional
migration-time SQLite backup. `lipas migrate rollback --yes` preserves the v2
database in a new backup before reactivating the retained v1 layout; any
v2-only writes therefore remain recoverable but are not projected backwards.
Migration and rollback recover dead-PID migration locks but refuse a live
lock. Rollback also refuses active first-party Runtime/worker leases or a busy
SQLite writer, checkpoints WAL, and verifies the preserved backup before
deactivating `workspace.db`.

## Run local workspace tasks

```bash
lipas task submit . "inspect the release and report risks"
lipas task worker --max-concurrency 2
lipas task approvals
lipas task report <task-id>
```

Commands that execute a Task accept the same `--base-url`, `--api-key-env`,
`--model`, `--model-streaming`, and `--max-tokens-field` options. A remote
model still receives only the bounded Workbench tools; it does not bypass
staging, command/write approval, Effect recording, or recovery.

CLI, Python API, Sessions, Handoffs, Operations, and product events share the
same global database. Per-Run Claim/Effect tapes remain separate under
`runs/<run-id>/claims.db` so budgets and replay evidence cannot leak between
concurrent Runs.

## Inspect a run

```bash
lipas trace runs/try.db
lipas trace runs/try.db --jsonl
lipas effects runs/try.db
```

`trace` renders the append-only claim tape. `effects` summarizes the effect
lifecycle, including causal `message_id` links when an Agent ran as a Team
member. An orphan effect means an intent lacks a terminal result or rejection;
investigate or reconcile it rather than assuming the external world is known.

The CLI is intentionally an experiment and operations surface. Production
agents remain normal Python code using `Agent`, `Tool`, `Team`, and
`OperationJournal` directly.
