# Upgrading the org deployment (M3-02)

How the platform team upgrades the server side installed from the umbrella chart
`deploy/helm/agentic-platform`, rolls it back, and raises the minimum CLI version.
Cutting and verifying a release is in [release.md](release.md); backups and restores are in
[postgres-backup-restore.md](postgres-backup-restore.md).

## How an upgrade works

`helm upgrade` runs in this order:

1. **Migration Job** (a `pre-upgrade` hook). It runs `python -m agentic_cp migrate` as the
   owner role, then `db grant-runtime` to re-align the runtime role. Helm waits for it.
   If it fails, Helm stops: nothing else changes and the old pods keep serving.
2. **New pods.** The API rolls one pod at a time (`maxUnavailable: 0`). A new pod takes
   traffic only when `/readyz` says the database is reachable, migrated and enforcing
   tenant isolation and insert-only audit.
3. **Old pods** keep serving until they are replaced. They stay ready on the migrated
   database: a release treats a migration revision it does not know as a newer release's
   and serves it (DF-413). Two migration runs at once take turns on a database lock.

The worker restarts too. One worker leads at a time (a database lock); the new one takes
over when the old one stops.

**The rule this depends on: migrations expand, then contract.** A migration may add
tables, columns, indexes and views. It must not drop or rename anything the previous
release still uses. Drop it one release later. Reviewers check this on every migration.

## Before you upgrade

1. Read the release notes (`CHANGELOG.md` at the tag) for migration notes.
2. Upgrade **staging first** (the same chart and values, staging's Secret), run
   `helm test`, the smoke run and the acceptance suite. Then prod.
3. Make sure last night's backup exists and the restore drill passed
   ([postgres-backup-restore.md](postgres-backup-restore.md)). For a migration that
   rewrites data, take an extra logical backup now.
4. **Large-table index builds.** A migration that adds an index with a plain
   `CREATE INDEX` blocks writes to that table while the index builds. `0011_audit_ops`
   adds one on `run_events`, the largest table: on an existing deployment with many run
   events, ingest pauses for the length of the build (DF-614). Schedule that upgrade in a
   quiet hour, or build the index first without blocking, as the owner role:
   `CREATE INDEX CONCURRENTLY IF NOT EXISTS run_events_tenant_id_received_at_idx ON agentic.run_events (tenant_id, received_at);`
   The migration then finds it and skips the build. Later migrations that index a large
   table say so in their docstring and in the release notes.

## Upgrade

```bash
helm upgrade agentic deploy/helm/agentic-platform \
  --namespace agentic --values values-prod.yaml --wait --timeout 15m
helm test agentic --namespace agentic
```

With a packaged chart, use the `.tgz` of the release instead of the folder. The chart's
version is the release version (`0.4.0`, or `0.4.0-rc.1` for a release candidate) and the
image tag defaults to it, so `--version` and the image move together.

Watch it:

```bash
kubectl -n agentic get jobs,pods
kubectl -n agentic logs job/agentic-agentic-platform-migrate
kubectl -n agentic rollout status deploy/agentic-agentic-platform-api
```

If the migration Job fails, read its log, fix the cause and run `helm upgrade` again. The
hook deletes the old Job before it starts a new one.

## Roll back

```bash
helm history agentic --namespace agentic
helm rollback agentic <revision> --namespace agentic --wait
```

`helm rollback` deploys the previous chart and image. The pre-upgrade hook does not run
backwards: **database migrations are never undone.** The previous release serves the
newer schema because migrations only expand (see the rule above). If a release broke that
rule, roll forward with a fix instead.

If the data itself is damaged, restore it ([postgres-backup-restore.md](postgres-backup-restore.md)).

## Raise the minimum CLI version

Raise it only **after a grace period**: the new release is published, announced and used
by most developers (for example two weeks, or when the index's download counts show the
old version fading). Never in the same change as the upgrade.

1. Set the deployment floor in your values file and upgrade:
   `minCliVersion: "0.4.0"` (it becomes `AGENTIC_CP_MIN_CLI_VERSION`).
2. Or raise it per org in the policy repo: `min_cli_version: "0.4.0"` in `policy/org.yaml`
   (a reviewed pull request). The higher of the two applies.
3. From then on an older CLI cannot register runs; it tells the developer how to upgrade.

To undo, lower the value the same way.

## Troubleshooting

| What you see | Meaning and fix |
|---|---|
| `/readyz` 503 `db.migrations_pending` | The database is older than the pods. The migration Job did not run or failed: check `helm history` and the Job's log. |
| `/readyz` 503 `tenancy.unenforced` or `audit.unenforced` | The runtime role or a table lost a guarantee. Run the migration Job again (`helm upgrade` with the same values); the server log names each gap. |
| Pods fail with "unknown setting AGENTIC_CP_..." | A variable with the `AGENTIC_CP_` prefix that is not a setting. The chart turns off Kubernetes service links (DF-415); check `config:` in your values. |
| Migration Job: `relation already exists` | An older release without the migration lock ran at the same time. Run the upgrade again. |
| Pre-install Job: service account not found | The Job uses `migrations.serviceAccountName` (default `default`); the namespace must have it. |
