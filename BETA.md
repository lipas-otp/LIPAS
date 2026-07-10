# Beta release gate

LIPAS is ready for a public beta when every item below is true:

- `pytest -q` is green, with no skipped safety or adapter contract tests.
- The supported Python versions are tested in CI, including a clean optional
  dependency install for every advertised adapter extra.
- OpenAI, Anthropic, and Ollama have recorded contract fixtures for success,
  tool use, rate limit, malformed response, and stream interruption.
- SQLite recovery is exercised across process restart for sessions, operation
  journals, and mailboxes; provider reconciliation is documented per provider.
- Public API changes have a migration note and a deprecation period unless they
  close a safety hole such as implicit side-effect classification.
- A release has versioned changelog entries, package build/install smoke tests,
  API reference, and a security/contact policy.

## What still separates beta from 1.0

Beta may retain API evolution and limited provider coverage.  1.0 additionally
requires a declared compatibility policy, stable serialization migrations,
concurrency/process ownership semantics for SQLite, performance/load limits,
and production observability (structured logs, metrics, tracing and redaction).

Exactly-once remains conditional even at 1.0: LIPAS can provide durable intent,
idempotency keys and reconciliation, but cannot prove a remote provider's
outcome after a crash unless that provider exposes a matching idempotency and
lookup contract.
