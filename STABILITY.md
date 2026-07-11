# API stability path

LIPAS is intentionally a normal Python library. A user should be able to
construct an `Agent`, decorate ordinary Python functions as tools, and call an
agent or a Team member without adopting a graph DSL, generated configuration,
or a hosted control plane.

## 1.0 candidate core

The APIs being stabilized first are the smallest reliable kernel:

- `Agent`, `DeclarativeAgent`, `LLM`, `Tool`, `ToolRegistry`, and explicit
  `SideEffectClass` declarations;
- append-only ClaimStore/RowSet semantics and effect intent/result/spend audit;
- `replay()` / `ToolReplayer` safety semantics;
- normalized adapter-layer `Request`, `Reply`, `Usage`, and stream events;
- `Team` at-least-once handoff semantics.
- `Supervisor` policy integration with the default `Agent` ReAct lifecycle and
  tag-indexed `project_supervisor(...)` queries.

For 1.0, semantic changes to these APIs require a migration note and a
deprecation period, except where a change closes a side-effect or data-safety
hole.

## Experimental until further notice

- provider-specific request extras and pricing tables;
- `OperationJournal` provider reconciliation/compensation adapters;
- cross-agent delegated capability and budget policy;
- SQLite multi-process ownership and claim-schema migrations;
- the legacy compatibility types exported from `lipas.types`.

New integrations should use the provider-neutral shapes from `lipas.adapter`.
The legacy root type surface remains available during beta while it is
converged deliberately, rather than silently changing a public import.
