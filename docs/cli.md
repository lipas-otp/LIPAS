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

Inside chat, `:trace` renders the current claim tape, `:effects` shows orphan
and rejected effects, and `:quit` exits. `--once "…"` is useful for scripts.
The session file is created automatically; do not `touch` it first.

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
