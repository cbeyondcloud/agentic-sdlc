# Data retention

How long each kind of data is kept, what deletes it, how a legal hold stops deletion and how
deletion is proven (M3-06; open requirement OR-01, owner decision 115).

## The periods

Each org sets them in its policy repo, `policy/retention.yaml`, under `days:`. A kind left
out keeps its default; `null` keeps it forever. Org admins change the file through a pull
request like any other policy file, and the control plane checks it before accepting it.

| Kind (`days.<key>`) | Default | Floor | What deletes it |
|---|---|---|---|
| `audit`: the audit chain | 2555 (7 years) | 2555, and never shorter than any other kind | the retention job |
| `runs`: control-plane runs and their events | 400 | 1 | the retention job |
| `feedback`: learning-loop feedback records and their journals | 365 | 1 | the retention job |
| `logs`: logs, support bundles, the control plane's sent-mail log | 30 | 1 | the retention job (mail log); your log stack and the person who made the bundle (the rest) |
| `gateway_usage`: the gateway's usage records | 400 | 400 (the gateway's billing floor) | the gateway's own purge, configured from this value |
| `traces`: prompts and answers in traces | 30 | 1 | your trace store's own retention setting |
| `run_folders`: a laptop's finished run folders and worktrees | 30, counted from the run's end | 1 | `agentic gc` |
| `backups`: database backups | 35 | 1 | your backup tooling (below) |

A policy that breaks a floor, keeps any kind longer than the audit chain, or names a team
that does not exist is refused with the reason, and the previous version stays active.

**Upgrading from the placeholder.** Before OR-01 the template's `retention.yaml` said
`days: { audit: 365, traces: 30 }`. That `audit` is below the floor, so a policy repo that
still has it is refused at its next change (the active version stays). Replace the file
with the current template (`templates/policy-repo/policy/retention.yaml`).

**Trace capture is off.** Prompts and answers reach a trace only when the team opts in
(`trace_capture: true` in `policy/teams/<team>.yaml`) **and** the repository's
classification allows it (`trace_capture: true` under that class in
`policy/classifications.yaml`). Today only the native backend honours it; agent-CLI
backends never send prompt text to the framework's traces. Set the trace store's own
retention (for example Langfuse's data retention, or the OpenTelemetry collector's
backend) to `days.traces`.

## The retention job

`python -m agentic_cp retention run` deletes, for every tenant:

1. feedback records captured before the feedback cut-off, then the feedback journals of
   runs with no feedback left;
2. runs whose last activity (opening, last event, last heartbeat) is older than the runs
   cut-off and that have no feedback left, with all their events;
3. sent and failed mails older than the logs cut-off (a mail not sent yet is never
   deleted);
4. the audit chain's oldest records older than the audit cut-off.

It runs as the database **owner role** (`AGENTIC_CP_OWNER_DATABASE_URL`): the evidence
tables stay insert-only for the role the API and worker use. The Helm chart runs it daily
as a CronJob (`retention.enabled`, `retention.schedule`, default `17 3 * * *`;
`retention.dryRun: true` only counts). Two runs never overlap: the CronJob forbids it, and
the command holds a Postgres advisory lock (a second run prints that another is in progress,
exits 4 and deletes nothing). With Docker Compose, run the command from cron or a scheduled
task on one host.

- `--dry-run` counts what one pass would delete and changes nothing. Runs that become free
  only once their feedback goes are counted the time after.
- `--tenant <slug>` handles one tenant.
- Each pass deletes at most 5000 rows of each kind (runs: 5000 runs, with all their
  events), in its own transaction with its own `retention.pruned` event; a run makes up to
  20 passes per tenant and says when rows are still left for the next run.
- A run with feedback is kept until its feedback goes: with `feedback: null`, such runs are
  kept forever too.
- A tenant without an accepted policy, or whose policy cannot be read, is skipped: nothing
  is deleted.

**How deletion is proven.** Every run that is not a dry run writes one `retention.pruned`
event on the tenant's audit chain, in the same transaction as the deletes: the counts per
kind, the periods and legal holds in force, the policy version, and whether rows were
left for the next run. Export it with the audit export (`agentic audit export`) like any
other event.

**The audit chain stays verifiable.** When the oldest audit records go, the
`retention.pruned` event also records the checkpoint: the sequence number and hash of the
last record removed. The records are removed after the event is written, so the checkpoint
sits inside the hash chain. `admin audit verify`, `db verify-restore` and `GET
/v1/audit/verify` accept a chain that starts after seq 1 only when such a checkpoint names
the record just before its first one and that record's hash; a gap without one is reported
as removed records. Audit exports already start from an anchor (the first record's `prev`),
so an export taken after pruning verifies with `auditproof.py` as before. Keep your last
export's `end` hash: it is the external anchor that a rewritten history cannot continue.

## Legal holds

List a hold in `retention.yaml` and nothing in its scope is deleted while it is listed:

```yaml
legal_holds:
  - id: LH-2026-01
    reason: "Litigation hold, case 2026-117 (legal@example.com)"
    data: [audit, runs, feedback]   # empty or missing: every kind
    teams: [payments]               # missing: the whole org
```

- An audit hold for a team stops the chain's pruning at that team's first record (the
  chain must stay one unbroken run of records); a hold without teams stops it entirely.
- Mails have no team: a logs hold for any team keeps all mails.
- The holds in force are named in each `retention.pruned` event.
- A tenant on hold cannot be deleted.
- The control plane cannot apply a hold to what it does not delete itself. For a hold on
  `gateway_usage`, regenerate the gateway config (below: it then keeps usage forever); for
  `traces`, `backups`, `logs` outside the control plane and laptops' `run_folders`, pause
  the trace store's, backup tool's and log stack's expiry and tell the people concerned not
  to run `agentic gc`.

## The gateway (TokenLean)

The control plane never writes the gateway's tables, and the gateway has no admin API for
retention, so its share is configuration. Print the block for a tenant and put it in the
gateway's `config.yaml` (its Helm values or the compose file's config):

```bash
python -m agentic_cp retention gateway-config --tenant acme
# retention:
#   enabled: true
#   interval_hours: 24
#   audit_days: 2555
#   usage_days: 400
#   cache_l2_expired_cleanup: true
```

The gateway's purge applies to its whole deployment: when several tenants share one
gateway, use the longest period any of them asks for. A hold on gateway usage gives
`usage_days: 0` (kept forever). Regenerate the block after every change to `retention.yaml`.

## Backups

Keep database backups for `days.backups` (35 by default) and no longer: a deleted row lives
on in every backup that still holds it. With a managed service, set the automated backup
and PITR retention to 35 days; with CloudNativePG, set the `Backup`/`ScheduledBackup`
retention policy to `35d`; for logical dumps, set the object storage lifecycle rule to 35
days. While a legal hold is listed, keep the backups its scope needs. See
[postgres-backup-restore.md](postgres-backup-restore.md).

## Laptops

`agentic gc` prunes finished run folders and their worktrees older than `days.run_folders`
(30 by default, counted from the run's end) unless `limits.yaml` sets
`local.gc_after_days`, the user's own config sets `gc.older_than_days`, or `--older-than`
is given. Nothing runs it automatically ([cli-operations.md](cli-operations.md)).

## Deleting a tenant

```bash
python -m agentic_cp admin tenant suspend --tenant acme --reason "contract ended"
python -m agentic_cp admin tenant delete --tenant acme --confirm acme --reason "contract ended"
```

Only a suspended tenant is deleted, never one on legal hold or with gateway keys the gateway
has not revoked (`agentic rbac revoke-keys`). Every row of the tenant goes except its audit
chain, which is kept for the audit period in force when it was deleted; `tenant.deleted`
closes the chain with the counts. The tenant shows as `deleted` under the slug `deleted-<id>`
(the old slug is free for a new tenant; the chain's `tenant.deleted` event keeps it), no
operator command but `admin audit verify` accepts it, and the retention job removes its chain and its row once the newest record is older than that
period (that last step is in the job's log, as no chain is left to hold it). Then:

- erase the tenant at the gateway with the gateway's own tenant erasure;
- drop its Grafana reader role (`db dashboard-role` made it) and delete its policy work
  folder (`AGENTIC_CP_POLICY_WORKDIR/<tenant id>`).

Migration `m3_06_retention` only widens the tenant status check. Its downgrade fails while
a deleted tenant exists: downgrade only before deleting any tenant.
