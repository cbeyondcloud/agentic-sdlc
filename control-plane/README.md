# agentic-control-plane (`agentic_cp`)

The org-mode server (SP15): one deployment serves several fully isolated tenants. Slice 1a
holds tenancy, token authentication, RBAC (pycasbin behind `agentic_cp.rbac.authz`) and
the per-tenant audit chain; policy sync, repos, ingest and the CLI side follow in slice 1b.

## Guarantees the database enforces

- Every table except `tenants` is tenant-owned: `tenant_id` first in every key, `ENABLE`
  and `FORCE ROW LEVEL SECURITY`, one `tenant_isolation` policy. The tenant reaches
  Postgres per transaction (`set_config('app.tenant_id', ..., true)`) from
  `tenancy.context.tenant_tx`; with no tenant set, reads return nothing and writes fail.
- The runtime role (the user in `AGENTIC_CP_DATABASE_URL`) owns nothing, has no
  `BYPASSRLS`, belongs to no role and may only `SELECT, INSERT` the audit log.
- Tokens resolve to their tenant only through registered `SECURITY DEFINER` functions.
- `/readyz` answers 503 and `serve` refuses to start when any of this does not hold
  (`AGENTIC_CP_REQUIRE_DB_ENFORCEMENT=false` relaxes it for local development only).

## Operating it

    python -m agentic_cp migrate               # owner role; re-grants and seeds roles
    python -m agentic_cp db grant-runtime      # create or re-align the runtime role
    python -m agentic_cp admin tenant create --slug acme --name "Acme" --admin-email a@acme.example
    python -m agentic_cp admin tenant list
    python -m agentic_cp admin issue-token --tenant acme --email a@acme.example
    python -m agentic_cp admin audit verify --tenant acme
    python -m agentic_cp serve
    python -m agentic_cp openapi --check       # the committed openapi.json is current

Settings are `AGENTIC_CP_*` variables (`agentic_cp/settings.py`); an unknown name is an
error. `AGENTIC_CP_OWNER_DATABASE_URL` is needed by `migrate`, `db` and `admin` only.

## Tests and Postgres

Database tests need Postgres 15 or later. They never use a Postgres installed on the host:

- natively (Windows, macOS, Linux) the fixtures start the pinned `postgres:17` image
  through `CONTAINER_ENGINE` (`docker` or `podman`; the first one on `PATH` otherwise);
- or set `AGENTIC_CP_TEST_DSN` to a superuser DSN of a throw-away server;
- without either, database tests skip with a reason; with `AGENTIC_CP_REQUIRE_PG=1` they
  fail instead. `scripts/check.py --linux` runs them against a Postgres sidecar on a
  private container network and sets that variable.

Each test gets its own database copied from a template migrated once per session.
