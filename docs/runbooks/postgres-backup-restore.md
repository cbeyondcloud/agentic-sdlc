# Postgres backups, point-in-time recovery and the restore drill (M3-02)

The control plane keeps everything in one Postgres schema, `agentic`: tenants, people and
roles, runs and their events, the audit chain, kill switches, notifications. TokenLean
keeps its own schema (`tokenlean`) and is backed up the same way. This runbook covers how
to back the schema up, how to restore it, and the drill that proves a restore works.

The org brings its own Postgres (the chart never runs one). Pick one of the options below
per environment. Prod needs point-in-time recovery (PITR) **and** a daily logical backup.

Related: [upgrade.md](upgrade.md) (upgrades and rollback), [release.md](release.md).

## What must be true after any restore

The restore drill and `python -m agentic_cp db verify-restore` check all three:

1. **Migration revision.** The schema is at the revision of the release you will run
   (`current`). An older one needs `python -m agentic_cp migrate` first.
2. **Database guarantees.** Every tenant table has forced row-level security, the audit
   tables are insert-only, and the runtime role has exactly its grants. The API refuses
   to serve without them.
3. **Every tenant's audit chain is intact.** A restore that lost rows in the middle of a
   chain, or changed one, shows up here.

A restore with no tenant at all fails as well: it is an empty or wrong dump. Pass
`--allow-empty` only for a deployment that has no tenant yet.

A chain cannot show that its newest records are missing: a backup taken before them
still verifies. So write down the tenant count and each tenant's record count when you
take a backup, and compare them with the check's output (`tenant <slug>: audit chain
intact, <n> records`). The retention job (M3-06) prunes a chain's oldest records after the
audit period: a record count can then be lower than at an earlier backup, and the chain
still verifies from its first kept record through the checkpoint in its `retention.pruned`
event.

## Option 1: a managed service (Cloud SQL, RDS, Azure Database, ...)

Use the provider's automated backups with PITR turned on.

- Retention: 35 days of backups and PITR (`retention.yaml` `days.backups`, OR-01; at
  least 14 in prod, 7 in staging if you shorten it). Older backups must go: a row the
  retention job deleted lives on in every backup that still holds it ([retention.md](retention.md)).
- High availability: a standby in another zone (the master plan's sizing: 4 vCPU /
  16 GB with an HA standby).
- Restore = the provider creates a **new instance** at a point in time. Never restore
  over the live one.

After the new instance is up:

```bash
# run the check against the restored instance (a one-off pod with the release's image)
kubectl -n agentic run cp-verify --rm -it --restart=Never \
  --image=<registry>/agentic-control-plane:<version> \
  --overrides='{"spec":{"containers":[{"name":"cp-verify","image":"<registry>/agentic-control-plane:<version>","args":["db","verify-restore"],"envFrom":[{"secretRef":{"name":"agentic-platform-restored"}}]}]}}'
```

The Secret `agentic-platform-restored` holds `AGENTIC_CP_DATABASE_URL` and
`AGENTIC_CP_OWNER_DATABASE_URL` pointing at the **restored** instance. When the check
says `restore verified`, point the chart's Secret at the new instance and restart:
`kubectl -n agentic rollout restart deploy`.

## Option 2: CloudNativePG (an operator in the cluster)

[CloudNativePG](https://cloudnative-pg.io) (Apache-2.0) runs Postgres with streaming
replicas and continuous WAL archiving to object storage (S3-compatible, GCS, Azure Blob).

- A `Cluster` with `instances: 3` and `backup.barmanObjectStore` (or the Barman Cloud
  plugin) for base backups and WAL.
- A `ScheduledBackup` every night.
- PITR: create a new `Cluster` with `bootstrap.recovery` and a `recoveryTarget`
  (`targetTime`). Never recover over the running cluster.

Then run `db verify-restore` against the new cluster as in option 1.

## Option 3: logical backups (every environment, in addition to 1 or 2)

A nightly `pg_dump` of the `agentic` schema in custom format. It is small, portable
between Postgres versions and providers, and it is what the restore drill uses.

```bash
# as the owner role; the pinned image of the local stack and the tests
docker run --rm -e PGPASSWORD postgres:17.10-alpine \
  pg_dump -Fc --schema=agentic -h <host> -U <owner role> -d agentic > agentic-$(date +%F).dump
```

In the cluster, run the same command as a CronJob with the same image, the password from
the chart's Secret, and the file written to object storage with a lifecycle rule that
deletes it after `days.backups` (35 days by default, OR-01; [retention.md](retention.md)). Keep backups as sensitive as the database: they hold the audit
trail and people's emails. Encrypt them at rest and limit who can read them.

## The restore drill

Run it **daily in staging** (automated) and **monthly against a prod backup** (by hand).
It restores a logical backup into a fresh, throw-away Postgres and runs the three checks.
It never touches the database the backup came from.

```bash
uv run python scripts/restore_drill.py --dump agentic-2026-10-29.dump
```

What it does:

1. Starts a fresh pinned Postgres container (docker or podman; set `CONTAINER_ENGINE` to
   choose). Nothing else is needed on Windows, macOS or Linux.
2. Re-creates the schema's owner role, restores the dump without privileges, and
   re-creates the dashboard views with the release's own definitions.
3. Grants a throw-away runtime role, then checks the revision, the guarantees and every
   tenant's audit chain.
4. Prints one line per finding and removes the container.

Exit codes: `0` the restore is usable, `1` it is not (the output says why), `2` the drill
itself could not run (no engine, not a custom-format dump).

To restore into a scratch server instead of a container (pg_restore must be on PATH):

```bash
export SCRATCH_PG='postgresql://postgres:...@scratch-db:5432/postgres'   # a superuser
uv run python scripts/restore_drill.py --dump agentic.dump --target-dsn-env SCRATCH_PG
```

The drill creates one database and one role there and drops both at the end.

Record each drill: date, backup file, the drill's output, who ran it. A failed drill is an
incident: the backups are not good enough until a drill passes.

## Restore for real

1. Stop writes: scale the release's API and worker to zero, and nothing else in the
   namespace (here the release is `agentic` in namespace `agentic`). CLIs queue events
   offline.

   ```bash
   kubectl -n agentic scale deploy -l app.kubernetes.io/instance=agentic,app.kubernetes.io/part-of=agentic-platform --replicas=0
   ```
2. Restore into a **new** database or instance (PITR, or `pg_restore` of the latest
   logical backup as in the drill, into an empty database).
3. Run `python -m agentic_cp migrate` if the restore is at an older revision, then
   `python -m agentic_cp db verify-restore`. Do not continue until it passes.
4. Re-run the steps a logical restore does not carry (a PITR restore keeps them):
   - `python -m agentic_cp db grant-runtime` (the runtime role's grants);
   - `python -m agentic_cp db link-usage ...` (the dashboards' link to TokenLean's tables);
   - `python -m agentic_cp db dashboard-role --tenant <slug> --role <reader>` for each
     tenant's Grafana reader role.
5. Point the chart's Secret at the restored database and scale back up. `helm upgrade`
   with the same values restores the worker. It does not restore the API: the chart
   leaves the API's replica count to its autoscaler, and an autoscaler never scales a
   Deployment up from zero. Set the API back to `api.autoscaling.minReplicas` yourself
   (2 by default; `api.replicas` when autoscaling is off); the autoscaler takes over from
   there.

   ```bash
   helm upgrade agentic <chart> --namespace agentic -f <your values> --wait --timeout 15m
   kubectl -n agentic scale deploy -l app.kubernetes.io/instance=agentic,app.kubernetes.io/component=api --replicas=2
   ```
6. Tell users what period was lost (between the restore point and the outage). The
   CLIs' offline queues resend what they still hold.

## Notes

- **Grafana reader roles see the gateway link's host.** Every Grafana user can read the
  options of the foreign server `agentic_gateway` (host, port, database name, never the
  password) in `pg_foreign_server` (DF-630). Use a DNS alias that reveals nothing for the
  TokenLean database if that matters in your org.
- **Worker leadership after a node failure.** The worker's leader holds a database
  session lock. If its node dies, Postgres frees the lock when it notices the dead
  connection (TCP keepalive). Set `tcp_keepalives_idle` on the server (for example 60
  seconds) so a standby takes over quickly.
