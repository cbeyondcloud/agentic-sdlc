# agentic-control-plane (`agentic_cp`)

The org-mode server (SP15): one deployment serves several fully isolated tenants. It holds
tenancy, token authentication, RBAC (pycasbin behind `agentic_cp.rbac.authz`) and the
per-tenant audit chain (slice 1a); each tenant's git policy repo, validated and served as a
digest-checked bundle, its repos and their CODEOWNERS-derived maintainers, and its run
event chains, verified on ingest (slice 1b). The CLI side lives in
`agentic_cli.control_plane` (`agentic login --for control-plane`, org-mode `init`, the run
event sink and `agentic sync`).

## Guarantees the database enforces

- Every table except `tenants` is tenant-owned: `tenant_id` first in every key, `ENABLE`
  and `FORCE ROW LEVEL SECURITY`, one `tenant_isolation` policy. The tenant reaches
  Postgres per transaction (`set_config('app.tenant_id', ..., true)`) from
  `tenancy.context.tenant_tx`; with no tenant set, reads return nothing and writes fail.
- The runtime role (the user in `AGENTIC_CP_DATABASE_URL`) owns nothing, has no
  `BYPASSRLS`, belongs to no role and may only `SELECT, INSERT` the evidence tables
  (`audit_log`, `policy_versions`, `run_events`); on `tenants` it sees its own row and may
  change only its RBAC revision and its policy source.
- Tokens (control-plane and policy-repo webhook) resolve to their tenant only through
  registered `SECURITY DEFINER` functions.
- `/readyz` answers 503 and `serve` refuses to start when any of this does not hold
  (`AGENTIC_CP_REQUIRE_DB_ENFORCEMENT=false` relaxes it for local development only).

## Operating it

    python -m agentic_cp migrate               # owner role; re-grants and seeds roles
    python -m agentic_cp db grant-runtime      # create or re-align the runtime role
    python -m agentic_cp admin tenant create --slug acme --name "Acme" --admin-email a@acme.example
    python -m agentic_cp admin tenant list
    python -m agentic_cp admin tenant set-policy-repo --tenant acme --repo https://git.acme.example/org/policy.git
    python -m agentic_cp admin issue-token --tenant acme --email a@acme.example
    python -m agentic_cp admin audit verify --tenant acme
    python -m agentic_cp serve
    python -m agentic_cp worker                # policy sync, CODEOWNERS, auto-trip, mail; one leader
    python -m agentic_cp openapi --check       # the committed openapi.json is current

`serve` and `worker` both refuse (exit 2) a database that does not hold the guarantees
above.

Settings are `AGENTIC_CP_*` variables (`agentic_cp/settings.py`); an unknown name is an
error. `AGENTIC_CP_OWNER_DATABASE_URL` is needed by `migrate`, `db` and `admin` only.

Git runs as a separate process with line-ending conversion off. A tenant's git credentials
are never stored in a URL: mount `<AGENTIC_CP_GIT_CONFIG_DIR>/<tenant-slug>.gitconfig`
(naming a credential helper) and git uses it for that tenant only. Git never reads the
system or the service account's own git config (`GIT_CONFIG_NOSYSTEM`, and
`GIT_CONFIG_GLOBAL` is that file or nothing), so a tenant **without** a `<slug>.gitconfig`
fetches anonymously: a credential helper or ssh key set up for the whole deployment does
not apply, and a private policy repo then fails with git's authentication error - mount
the tenant's file to fix it. Tenant-set repository
URLs must be `https://`, `ssh://` or `user@host:path`; `AGENTIC_CP_ALLOW_LOCAL_REPOS=true`
also accepts `file://` and paths, for development only (in a shared deployment a tenant
could read the server's disk). Request bodies over `AGENTIC_CP_MAX_BODY_BYTES` get 413; a
limit at the ingress in front of the service is still recommended.

### Rate limits

Each process gives every token holder a bucket of `AGENTIC_CP_RATE_LIMIT_PER_MINUTE`
requests (default 1200: a minute's worth as burst, refilled continuously) and, when
`AGENTIC_CP_TENANT_RATE_LIMIT_PER_MINUTE` is set, one bucket for all of a tenant's callers;
over it the API answers 429 with `Retry-After`. `0` turns a limit off. The buckets are per
process and are checked after the token is resolved, so a limit across replicas, and any
limit on unauthenticated floods (invalid tokens, webhooks), belongs at the ingress.

### Health, metrics and alerts

- `GET /healthz`: liveness (never touches the database). `GET /readyz`: readiness - the
  database answers, is migrated to this release and enforces the guarantees (503 with a
  problem code otherwise).
- `GET /metrics`: Prometheus text format, only with `AGENTIC_CP_METRICS_TOKEN` as the bearer
  token (404 when unset or blank, 401 for a wrong one; a token shorter than 16 characters
  refuses startup). Requests by route template and status class,
  request duration, ingest lag/results/rejections, kill-switch state reads, and per tenant
  the mail outbox (pending, failed, oldest pending) and active switches by mode and source
  (`human`, `auto-trip`). The per-tenant gauges are read from the database at most every
  `AGENTIC_CP_METRICS_REFRESH_SECONDS` (30); tenants beyond `AGENTIC_CP_METRICS_MAX_TENANTS`
  (100, by slug order) are summed as `tenant="_other"`. No label carries a run, user, team
  or token id.
- `deploy/prometheus/alerts.yml`: alert rules (control plane down, 5xx rate, ingest lag,
  chain rejections, outbox backlog, stuck and failed mail, auto-trip active), with promtool
  unit tests in `alerts.test.yml`; the Linux check stage runs `promtool check rules` and
  the unit tests. Scrape job name: `agentic-control-plane`. Every replica reads the
  per-tenant gauges from the same database, so they repeat across replicas: aggregate them
  with `max`, never `sum`, in your own rules and dashboards.

### Auto-trip

The worker pauses a team whose spend rate or error rate passes the threshold in the active
policy (`org.yaml`):

    auto_trip:
      spend_usd_per_hour: 50      # unset: no spend trip
      error_rate: 0.3             # share of steps that ran and ended in error; unset: off
      window_minutes: 60          # the measuring window (by each event's own time)
      min_steps: 20               # fewer steps: the error rate is unmeasured
      reset_ratio: 0.5            # re-armed only below threshold x reset_ratio
      hold_minutes: 30            # auto_clear waits at least this long after a trip
      auto_clear: false           # true: the control plane clears its own switch

A team may only tighten the thresholds (`teams/<team>.yaml: auto_trip: {spend_usd_per_hour:
20}`). Spend is what the runs' `step_finished` events reported (`cost.usd`); a team with no
reported cost is unmeasured and never trips on spend. A trip sets a team pause switch
(`created_by: system:auto-trip`), mails the team's `killswitch.team` holders and is
audited (`killswitch.set`, `autotrip.tripped`). If a person clears the switch while the
rate is still high, auto-trip stays disarmed until the rate is below the reset level.

### Audit export

`GET /v1/audit/export?format=jsonl|csv&team=&from=&to=` (CLI: `agentic audit export`)
streams a range of the tenant's audit chain with its chain proof; it needs `audit.export`
(the builtin `auditor` role and org admins). Records of other teams in a team export are
`link` rows (sequence and hashes only); `team` must be a team slug. Both formats carry the
chain proof: a header row (the anchor before the range) and a proof row (counts, end
anchor, chain head), so a file with rows missing at either end does not verify - in CSV
they are rows of `kind` `header` and `proof` with their values as JSON in the `after`
column. An auditor verifies an export without this
database or our packages: copy `core/src/agentic/auditproof.py` (standard library only)
and run `python auditproof.py export.jsonl` (exit 0 intact, 3 broken). Keep each export's
end anchor: the next export's start must continue it. CSV cells that a spreadsheet would
run as a formula carry one leading `'`, which the verifier strips.

## Tests and Postgres

Database tests need Postgres 15 or later. They never use a Postgres installed on the host:

- natively (Windows, macOS, Linux) the fixtures start the pinned `postgres:17` image
  through `CONTAINER_ENGINE` (`docker` or `podman`; the first one on `PATH` otherwise);
- or set `AGENTIC_CP_TEST_DSN` to a superuser DSN of a throw-away server;
- without either, database tests skip with a reason; with `AGENTIC_CP_REQUIRE_PG=1` they
  fail instead. `scripts/check.py --linux` runs them against a Postgres sidecar on a
  private container network and sets that variable.

Each test gets its own database copied from a template migrated once per session.
