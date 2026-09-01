# Production-minded installation and onboarding

LIPAS 0.63 is local-first. The supported first-run path is deliberately
provider-free so an operator can verify storage, sandbox, approval, recovery,
and delivery before configuring a billable model or connector.

## Five-minute first use

From a source checkout, use an isolated virtual environment and a disposable
home directory:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[all]'
mkdir -p ~/lipas-demo
cd ~/lipas-demo
lipas install --home ~/.lipas-demo --sandbox auto
lipas doctor --home ~/.lipas-demo
lipas tour --offline
```

The offline tour needs no model, provider account, or network access. It is the
recommended first success criterion. If `doctor` reports that Bubblewrap is not
operational, keep the tour provider-free; use `--sandbox local` only for
trusted code in a disposable workspace and only when an explicit isolation
fallback is acceptable.

To try a local model afterwards, start Ollama, pull a model, and run one
bounded prompt:

```bash
ollama pull gemma4:12b
lipas model list
lipas chat --model gemma4:12b \
  --session ~/.lipas-demo/runs/chat.db \
  --once "Explain the local-first control plane in three sentences."
```

For a quick smoke test, the model may be written directly after `chat`:

```bash
lipas chat phi4-mini --once "Introduce yourself in one sentence"
```

Local Ollama connections ignore ambient `HTTP(S)_PROXY`/`ALL_PROXY` settings
by default. If the Ollama host is remote and needs a proxy, opt in with
`--trust-env`.

Built-in chat turns are remembered automatically in `~/.lipas/runs/chat.db`; use
`--no-memory` for an ephemeral turn. The persisted value is complete current-session
message history (including tool results), not hidden personal memory; there is no
automatic summary or semantic/vector retrieval yet, and long conversations remain
bounded by the selected model's context window. To give the assistant safe project
context, opt in to read-only file tools with `--workspace .`.

## Standard installation and readiness

```bash
python -m pip install 'lipas[all]'
lipas install --home ~/.lipas --sandbox auto
lipas release check --home ~/.lipas
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

`lipas upgrade --home ~/.lipas` is the idempotent production wrapper around
the explicit migration flow. It writes `.installation.json` and never removes
the retained legacy or pre-restore backup. `lipas backup` and `lipas restore`
use SQLite online-backup and integrity checks; restore is fenced against live
Runtime processes and requires an explicit confirmation flag.

For a complete portable snapshot, include per-Run evidence explicitly:

```bash
lipas backup --home ~/.lipas --destination /safe/lipas-bundle --include-evidence
lipas restore --home ~/.lipas-restored --source /safe/lipas-bundle --yes
```

The bundle contains `workspace.db`, `runs/**` (including each Run's
`claims.db`), and installation metadata. A manifest records sizes, SHA-256
hashes, and SQLite integrity; restore rewrites installation paths for the new
home and keeps a rollback bundle by default.

Production operators should keep the workspace directory on a durable local
filesystem, back up `workspace.db` and `runs/` together, and never copy a live
database while a worker is writing. Configure provider credentials through an
allowlisted environment reference; raw keys must not appear in prompts,
arguments, URLs, reports, or operation requests.

For local secret custody, `FileSecretResolver` stores only operator-owned
`secret://file/NAME` references in application data and atomically rotates a
0600 JSON file. For any non-loopback Operator or remote Worker bind, configure
`TLSConfig` with a TLS 1.2+ certificate/key pair and keep authentication
enabled; loopback-only HTTP remains available for development fixtures.

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

The repeatable fixture harness can be run before inviting a partner:

```python
from lipas import DesignPartnerCase, run_design_partner_validation

report = run_design_partner_validation(
    "local-fixture",
    [
        DesignPartnerCase("repo", "Repository maintenance", "inspect, patch, verify"),
        DesignPartnerCase("mail", "Controlled email", "draft, approve, send, reconcile"),
    ],
    lambda case: {
        "run_id": "fixture-" + case.case_id,
        "success": True,
        "unsafe_delivery": False,
        "operator_accepted": True,
    },
)
assert report.evidence_scope == "local_fixture"
```

This validates report shape and operator workflow only. It is not partner
evidence; external partners must run the same cases with their own accounts
and sign off the redacted evidence package.

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
