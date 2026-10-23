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
    python -m agentic_cp worker                # policy sync + CODEOWNERS refresh, one leader
    python -m agentic_cp openapi --check       # the committed openapi.json is current

Settings are `AGENTIC_CP_*` variables (`agentic_cp/settings.py`); an unknown name is an
error. `AGENTIC_CP_OWNER_DATABASE_URL` is needed by `migrate`, `db` and `admin` only.

Git runs as a separate process with line-ending conversion off. A tenant's git credentials
are never stored in a URL: mount `<AGENTIC_CP_GIT_CONFIG_DIR>/<tenant-slug>.gitconfig`
(naming a credential helper) and git uses it for that tenant only. Tenant-set repository
URLs must be `https://`, `ssh://` or `user@host:path`; `AGENTIC_CP_ALLOW_LOCAL_REPOS=true`
also accepts `file://` and paths, for development only (in a shared deployment a tenant
could read the server's disk). Request bodies over `AGENTIC_CP_MAX_BODY_BYTES` get 413; a
limit at the ingress in front of the service is still recommended.

## Dashboards (M2-37, M2-38)

Grafana reads the `dash_*` views (`agentic_cp/dashboards/views.py`): runs, steps, gates,
limits, audit and repos, joined with the gateway's usage rows through their tags (run, step,
agent, user, repo, pipeline, tier, policy version) and its authenticated team. Cost per run,
step, agent and team shows the gateway's cost next to the cost the engine reported; calls
without a run tag are the `(unattributed)` line; what was not measured is NULL, never 0.

    python -m agentic_cp db link-usage --host H --dbname D --user U --password-env VAR
    python -m agentic_cp db dashboard-role --tenant acme --role grafana_acme --password-env VAR
    python -m agentic_cp dashboards --check     # the committed dashboard JSON is current

- **The gateway link.** The views read two contract views, `usage_source` and
  `gateway_audit_source`, empty until `db link-usage` points them at the gateway's
  `usage_events` and `audit_events` through `postgres_fdw` (the gateway's database may be on
  the same server or another; give the link a read-only gateway role). Run it again after a
  gateway upgrade; a gateway without ST-1 (`team`) or ST-2 (`tags`) is refused.
- **Reader roles.** One Grafana login role per tenant. It may select the `dash_*` views and
  nothing else, its role default `app.tenant_id` is its tenant, and the views show it that
  tenant's rows only, whatever it sets the setting to. Each tenant's Grafana organisation
  provisions the datasource uid `agentic-postgres` with its own reader, so every tenant uses
  the same dashboard JSON (`agentic_cp/dashboards/grafana`, rendered from `catalog.py`).
- **One-time DBA steps** (a superuser, once per database): `CREATE EXTENSION postgres_fdw`,
  `GRANT USAGE ON FOREIGN DATA WRAPPER postgres_fdw TO <owner>` and
  `GRANT SET ON PARAMETER app.tenant_id TO <owner>`. The owner role must be neither a
  superuser nor `BYPASSRLS` (the views run with its rights; `db dashboard-role` checks).
- `deploy/org-local` runs all of it on one machine (control plane, Postgres, Grafana and
  the gateway's LITE stack).

## Tests and Postgres

Database tests need Postgres 15 or later. They never use a Postgres installed on the host:

- natively (Windows, macOS, Linux) the fixtures start the pinned `postgres:17` image
  through `CONTAINER_ENGINE` (`docker` or `podman`; the first one on `PATH` otherwise);
- or set `AGENTIC_CP_TEST_DSN` to a superuser DSN of a throw-away server;
- without either, database tests skip with a reason; with `AGENTIC_CP_REQUIRE_PG=1` they
  fail instead. `scripts/check.py --linux` runs them against a Postgres sidecar on a
  private container network and sets that variable.

Each test gets its own database copied from a template migrated once per session.
