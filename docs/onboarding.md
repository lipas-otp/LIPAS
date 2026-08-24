# Production-minded installation and onboarding

LIPAS 0.40 is local-first. The supported first-run path is deliberately
provider-free so an operator can verify storage, sandbox, approval, recovery,
and delivery before configuring a billable model or connector.

```bash
python -m pip install 'lipas[all]'
mkdir -p ~/.lipas
lipas doctor --home ~/.lipas --json
lipas tour --offline
```

`doctor` distinguishes an uninitialized workspace, an explicit migration
requirement, SQLite health, and default sandbox readiness. On Linux, the safe
default is Bubblewrap; use `--sandbox local` only for trusted code. The offline
tour exercises separated user input, write approval, durable resume, audit,
and report generation without a model or external write.

For an existing pre-0.40 workspace:

```bash
lipas migrate plan --home ~/.lipas
lipas migrate apply --home ~/.lipas --yes
lipas migrate verify --home ~/.lipas
lipas audit --home ~/.lipas --repair
```

Production operators should keep the workspace directory on a durable local
filesystem, back up `workspace.db` and `runs/` together, and never copy a live
database while a worker is writing. Configure provider credentials through an
allowlisted environment reference; raw keys must not appear in prompts,
arguments, URLs, reports, or operation requests.

Before enabling an external connector, verify all of the following:

1. the host is in the egress allowlist;
2. writes have an explicit stable idempotency key;
3. the provider exposes a lookup/reconciliation route;
4. approval displays scope, preview/diff, budget, and recipient/resource;
5. a provider-free failure drill has been recorded.

The local operator exposes `/api/approvals`, `/api/operations`, and
`/api/operations/<key>/reconcile` in addition to task/run pages. Mutating
routes require the bearer token. Reconciliation is an explicit operator
decision: every operation closeout records an observation (and a provider
reference when found), while Run reopening records an evidence object. It is
never inferred from a timeout or a boolean acknowledgement alone.

## Design-partner validation

The first external cohort should validate one recurring workspace workflow per
partner: repository/release work, document/data processing, or controlled email
and ticket preparation. The pilot must not enable unrestricted autonomous
publishing. Each partner owns the provider account, workspace, retention
decision, and approval policy.

Every partner runs the same bounded fixtures:

1. inspect-only task;
2. staged local write with verification and diff review;
3. process kill after an Effect commit and before its checkpoint;
4. approval and missing-input suspend/resume;
5. provider timeout yielding `uncertain`, followed by reconciliation;
6. redelivery with the same request identity;
7. rejected path escape or egress request.

Record verified completion, duplicate-write incidence (target: zero), recovery
time, uncertain-operation count and reconciliation time, approval latency,
manual-takeover reason, estimated versus billed usage, and whether the operator
can explain what changed, what was verified, and what remains uncertain. Do not
use test count as a proxy for trust. Expand the pilot only after two consecutive
weeks without an unexplained duplicate write, an unreviewed external write, or
an irrecoverable workspace state.

The evidence package contains only a redacted task report, event cursor,
verification result, operation reconciliation record, and interview summary.
Never export raw secrets, full provider payloads, or personal content without
explicit retention consent.
