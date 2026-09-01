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

## Install and maintain one local workspace

The 0.63/1.0 local-first path uses an explicit manifest and copy-on-write
storage operations:

```bash
lipas install --home ~/.lipas --sandbox auto
lipas release check --home ~/.lipas
lipas backup --home ~/.lipas --destination /safe/path/lipas.db
lipas backup --home ~/.lipas --destination /safe/path/lipas-bundle --include-evidence
lipas verify-bundle --source /safe/path/lipas-bundle
lipas soak --home ~/.lipas --iterations 10000 --json
lipas restore --home ~/.lipas --source /safe/path/lipas.db --yes
lipas restore --home ~/.lipas --source /safe/path/lipas-bundle --yes
lipas upgrade --home ~/.lipas
```

`install` is idempotent and creates `.installation.json`, `workspace.db`, and
the `runs/` evidence directory with restrictive permissions. `upgrade` applies
legacy migration only when required and never deletes the previous state.
`release check` is read-only; a failed check must be resolved before exposing
the operator beyond loopback. Restore requires `--yes` and preserves a
pre-restore backup by default.
The default backup is a single SQLite file for compatibility. Use
`--include-evidence` to create a directory bundle containing `workspace.db`,
all `runs/**` evidence (including per-Run `claims.db` tapes), and a manifest
with file sizes, SHA-256 hashes, and SQLite integrity checks. Restoring a
directory source automatically uses the bundle restore path and preserves a
rollback bundle by default.
`verify-bundle` performs the same manifest, path, hash, and SQLite integrity
checks without modifying the destination workspace, so it can be used before
copying a bundle to removable or offline storage.
`soak` repeatedly exercises local durable Task/Run transitions and checks that
each Run reaches a terminal state. Its report is local durability evidence,
not a model/provider SLA; use `--duration` with a large iteration cap for a
time-bounded rehearsal.

For a one-off, tool-less local conversation:

```bash
lipas chat --model gemma4:12b --session runs/try.db
```

See which local models are installed with `lipas model list` (or the shortcut
`lipas chat list`).

The model can also be supplied positionally for a faster first run:

```bash
lipas chat phi4-mini --once "Say hello in one sentence"
```

Local Ollama connections ignore ambient `HTTP(S)_PROXY`/`ALL_PROXY` settings
by default, so a shell proxy cannot prevent the local client from starting.
When connecting to a remote Ollama host through a proxy, opt in with
`--trust-env`.

The built-in chat remembers the conversation in `~/.lipas/runs/chat.db` by default. Use
`--session PATH --session-id NAME` for a separate memory, or `--no-memory` for
a one-off in-memory turn. This is complete message-history memory (user turns,
assistant replies, and tool results), not hidden personal memory: automatic
summarisation and vector/semantic retrieval are not enabled yet, so a very long
conversation remains subject to the selected model's context window. `:about`,
`:memory`, `:runtime`, `:reset`, `:tools`, and `:help` are available inside the REPL.

Chat also exposes a read-only `get_runtime_info` tool. It reports the actual
working directory, selected workspace, session identity, memory mode, and
capability boundary. The model must use those facts for location/capability
questions rather than inventing a generic "no filesystem" limitation. Chat
does not expose write or shell authority; use `lipas task start <goal>
<workspace>` for staged workspace changes and review/apply the resulting
ChangeSet explicitly.

To let the model inspect a project (including bounded PDF text extraction), opt in to bounded read-only file tools:

```bash
lipas chat phi4-mini --workspace .
```

For example, ask it to inspect the project with
`--once "List the Python files and summarize the entry point"`.

Additional explicitly classified tools can be loaded with
`--tool-factory module:callable`; shell and write capabilities remain outside
the built-in chat surface and belong in the Task Workbench.

Workspace Tasks include bounded document tools. `read_pdf` extracts text from
an unencrypted PDF (with page/character limits), while
`convert_workspace_file` can create reviewable TXT, Markdown, HTML, JSON, or
CSV, DOCX, or XLSX outputs from supported text/PDF/DOCX/XLSX/PPTX input pairs
(for example PDF→TXT/Markdown/HTML/JSON and XLSX→CSV/JSON). DOCX/XLSX/PPTX/PDF parsers
are optional dependencies; install them with `pip install 'lipas[documents]'`.
Every conversion writes inside the selected/staged workspace and records the
source, target, digest, and size as evidence. Unsupported, encrypted, secret,
oversized, or path-escaping inputs fail closed.

The same Workbench exposes bounded `calculate` and `analyze_csv` helpers, plus
`inspect_archive`/`extract_archive` for ZIP/TAR files. Archive extraction
rejects traversal, links, and device members before writing and limits both
member count and expanded bytes. `python_exec` is a temporary, approval-gated
worker; it never receives implicit project files and reports its sandbox flags.

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

## Select business knowledge and Scenarios

Packaged Skills are instruction-only and can be inspected without a model:

```bash
lipas skill list
lipas skill show coding-task
```

Scenarios compose the minimal Skill bundle, lifecycle, and Tool requirements:

```bash
lipas scenario list
lipas scenario list --category office
lipas scenario show coding-change
lipas scenario check email-delivery --factory connectors:email_tools --json
```

`scenario check` validates exact Tool names, required input fields, and honest side-effect declarations.
For connector Scenarios it also reports host-policy obligations that Tool shape
alone cannot prove.

Use repeatable `--skill` to select only the knowledge needed by a built-in
chat or task Agent, and repeatable `--skill-path` to add portable local
`SKILL.md` files:

```bash
lipas chat \
  --scenario email-draft \
  --once "Draft a concise customer update"

lipas task start . "repair the parser regression" \
  --scenario coding-change
```

Repeat `--scenario` to compose recipes; repeat `--skill` or `--skill-path` for
additional knowledge. No selection adds Tools or permissions. A tool-less chat
rejects a workspace/connector Scenario whose declared capabilities would be
missing. Custom chat and task factories may accept the composed
`SkillRegistry` through `skills=` or `**kwargs`; task factories may also accept
the selected `ScenarioRegistry` through `scenarios=`. Worker and resume
commands accept the same selection flags.

See [business Skills, Scenarios, and capabilities](business-skills.md) for the
catalog and the boundary between drafting an email and sending one.

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
lipas audit --home ~/.lipas --repair
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
concurrent Runs. Since 0.40, core connections share WAL and bounded contention
policy, and durable convenience calls no longer hold a Runtime-wide lock.
SQLite remains a single physical writer; deployment and scale guidance is in
[SQLite storage and concurrency](sqlite-storage.md).

## Local Web operator (0.63.0)

The Python API exposes a dependency-free operator projection without adding a
second queue or status store:

```python
with LIPASRuntime.open(".lipas") as runtime:
    operator = runtime.operator(operator_token="use-a-local-secret")
    operator.serve_forever(host="127.0.0.1", port=8787)
```

The production CLI wrapper keeps the operator authenticated and prints a
one-time token for loopback development when `LIPAS_OPERATOR_TOKEN` is absent:

```bash
export LIPAS_OPERATOR_TOKEN='a-long-local-secret'
lipas operator serve --home ~/.lipas --host 127.0.0.1 --port 8787
```

Non-loopback binds require `--certfile`/`--keyfile` (and the token environment
variable); `TLSConfig` enforces TLS 1.2+ and private-key permissions. Use
`--cafile --require-client-certificate` when mutual TLS is part of the host
policy.

`serve_forever` owns the serving thread. If an application runs it in a
dedicated thread, call `operator.shutdown()` from another thread and let the
loop close the server; the Runtime/Store itself must still be opened in the
serving thread when the default thread-bound SQLite connection is used.

`GET /`, `/ui`, `/health`, `/ready`, `/api/snapshot`, `/api/tasks`, `/api/tasks/<id>`,
`/api/runs`, `/api/runs/<id>`, and `/api/runs/<id>/events` return bounded,
redacted projections. The root route is a small dependency-free browser page;
the JSON routes are the reconnectable contract. When the
operator is created by `LIPASRuntime`, task detail also includes Workbench
events, artifacts, ChangeSet paths/diff, and the current report. `POST
/api/tasks/<id>/cancel`, `POST /api/runs/<id>/cancel`, and `POST
/api/interrupts/<id>/{resolve,approve,deny}` delegate to the existing durable
transitions and require `Authorization: Bearer ...`. Stale mutations return
HTTP 409 so a UI can refresh and retry without guessing state. The operator
remains a projection rather than a second scheduler; clients can
reconnect using the same per-Run and aggregate event cursors.

### Conversation kernel (0.41)

When the operator is created from `LIPASRuntime`, release 0.41 exposes the
conversation kernel over the same workspace database:

```text
GET  /api/conversations
POST /api/conversations
GET  /api/conversations/<conversation-id>
GET  /api/conversations/<conversation-id>/messages
POST /api/conversations/<conversation-id>/messages
GET  /api/conversations/<conversation-id>/events?after=0&limit=100
POST /api/conversations/<conversation-id>/events
POST /api/conversations/<conversation-id>/messages/<message-id>/promote
```

All POST routes require the operator bearer token. A client should send a
stable `message_id` and retain `next_cursor`; duplicate message or promotion
requests return the original fact rather than creating another Task/Run. The
event route is intentionally cursor-pollable instead of hiding a second
streaming state: a chat host can append `model_delta`, `tool_activity`,
`approval_card`, `diff`, or `report` events while durable AgentEvents and
Interrupts are projected automatically for linked Runs. This keeps browser,
CLI, and Python views reconnectable without a frontend dependency or an SSE
server thread competing with SQLite's thread-bound connection.

### Local Web conversation projection (0.42)

Release 0.42 adds the browser timeline, authenticated SSE catch-up, approval
and input cards, tool activity, diffs, reports, and content-addressed
attachments on top of the 0.41 routes. It remains a bounded projection of the
same authority and does not create a second scheduler.

`GET /api/approvals` adds risk, scope, preview/diff, and budget fields;
`GET /api/operations` lists pending/uncertain external operations. A provider or
operator can explicitly reconcile one with `POST
/api/operations/<key>/reconcile` (`found=true|false`); the request must include
an observation, and `found=true` must include the provider reference. A durable
phase timeout requires `POST /api/runs/<id>/reopen` with
`{"acknowledge_uncertain":true,"reconciled":true,"evidence":{"observation":"..."}}`
after its Effect/provider outcome has been reconciled. Neither route retries a
live operation implicitly or treats a checkbox alone as provider evidence.

`GET /api/metrics`, `/api/incidents`, and `/api/cost` expose bounded derived
projections for a local dashboard. They do not replace the append-only event
or Effect evidence, and an empty execution window is never reported healthy.
`/ready` (or `/api/ready`) runs the local installation/readiness checks,
including schema, permission, and disposable backup/restore verification.

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
