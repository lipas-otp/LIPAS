# 0.63 / 1.0 local-first release checklist

This checklist records the 0.63 capability-complete refinement on the path to 1.0.
It separates repository contracts from deployment evidence. Passing the
automated suite is necessary, but it does not claim provider SLAs, KMS/HSM
custody, or external design-partner acceptance.

## Before installation

- Install the wheel in a clean virtual environment and run `lipas install`.
- Run `lipas release check` and resolve every failed check.
- Keep the workspace on a durable local filesystem with a tested restore path.

## Before enabling writes

- Run `lipas tour --offline` and the fault matrix in a disposable workspace.
- Run a bounded local soak and retain its JSON report, for example
  `lipas soak --home "$LIPAS_HOME" --iterations 10000 --json`.
- Run one explicitly approved real-provider workflow with
  `run_provider_workflow(..., live=True)` and archive only redacted terminal
  evidence, usage, provider request identity, and reconciliation records.
- Exercise approval, cancellation, uncertain-operation reconciliation, and
  process-restart recovery with provider-free fixtures.
- Configure an allowlisted secret reference. Never place raw credentials in
  prompts, tool arguments, URLs, reports, or logs.
- For non-loopback Operator/Worker binds, configure TLS and authentication;
  document certificate expiry, rotation, and rollback owners. Load a new
  `TLSConfig` and call `server.reload_tls(...)` (and
  `RemoteWorkerHTTPClient.reload_tls(...)` for rotated client trust); existing
  connections finish on the old certificate and new connections use the new
  context.
- Inject a managed KMS/HSM resolver through `ManagedSecretResolver`; the
  resolver must return opaque references only and provide a redactor for
  provider responses. The built-in file resolver is not key-management proof.

## Backup and restore drill

```bash
lipas backup --home "$LIPAS_HOME" --destination /safe/lipas-bundle \
  --include-evidence
lipas verify-bundle --source /safe/lipas-bundle
lipas restore --home "$LIPAS_RESTORE" --source /safe/lipas-bundle --yes
lipas release check --home "$LIPAS_RESTORE"
```

The evidence bundle is the archival unit for a single workspace: it includes
`workspace.db`, every regular file below `runs/`, installation metadata, and a
manifest with SHA-256 and SQLite integrity checks. Keep at least one rollback
bundle until the restored workspace has been audited.

## External acceptance

Record real operator runs separately from local fixtures. A release claim is
complete only after the planned soak period, incident review, and rollback
exercise have named owners and reproducible evidence.
