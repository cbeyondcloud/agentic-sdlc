# Local org stack (M2-39)

The framework's org mode on one machine: the control plane, its Postgres and Grafana with
the seven framework dashboards, next to the gateway's (TokenLean) LITE stack in mock mode,
so nothing calls a model provider and nothing is spent. Windows, macOS and Linux; Docker or
Podman.

| Service | What | Address |
|---|---|---|
| `control-plane` | `python -m agentic_cp serve` | http://127.0.0.1:8280 |
| `grafana` | official Grafana image, provisioned with the framework dashboards (folder *Agentic*) and the gateway's LITE set (folder *Gateway*) | http://127.0.0.1:3300 |
| `cp-postgres` | the control plane's database (Postgres 17, `postgres_fdw`) | stack network only |
| `cp-init` | one-shot: migrate, runtime role, tenant `local`, gateway usage link, Grafana reader role | - |
| LITE stack | the gateway proxy, its Postgres and Redis, and a mock provider, started by TokenLean's own `deploy/lite/local/up` | http://127.0.0.1:4100 |

## Use

You need a container engine with compose (Docker Desktop, Rancher Desktop or Podman with a
compose provider), curl (bash scripts) and a TokenLean checkout that has `deploy/lite/local`
(its LITE profile). That checkout is only read; TokenLean's own `up` writes its git-ignored
`.env.lite` and `.state/` there.

Published ports bind to `127.0.0.1` and are set in `.env.org`: `ORG_CP_PORT` (8280),
`ORG_GRAFANA_PORT` (3300); the gateway's port is `LITE_PROXY_PORT` in its `.env.lite`. Change
them there if something else on the machine already listens on one.

bash or zsh (macOS, Linux, Git Bash):

    export TOKENLEAN_DIR=~/src/token-optimisation
    ./up.sh          # first run builds the images: several minutes
    ./smoke.sh
    ./down.sh        # --purge also deletes the data; --keep-lite leaves the gateway running

PowerShell (Windows PowerShell 5.1 or PowerShell 7):

    $env:TOKENLEAN_DIR = 'D:\token-optimisation'
    .\up.ps1
    .\smoke.ps1
    .\down.ps1       # -Purge, -KeepLite

Podman: set `CONTAINER_ENGINE=podman` (`$env:CONTAINER_ENGINE = 'podman'`). Every compose
call names the project (`-p agentic-org`), so another stack's `COMPOSE_*` settings cannot
redirect it. Bind mounts carry the SELinux `z` label.

Grafana's login is `admin` with `GRAFANA_ADMIN_PASSWORD` from `.env.org`. The first run
writes `.env.org` (git-ignored) with generated passwords and keeps them; the gateway's
tenant and database password are copied from its `.env.lite` on every `up`.

A token for the tenant's first admin (printed once, not kept in any log), from this folder:

    docker compose -p agentic-org --env-file .env.org run --rm --entrypoint python cp-init -m agentic_cp admin issue-token --tenant local --email admin@org.local

## What smoke proves

The control plane is live and ready (migrated, tenant isolation enforced); Grafana is
healthy, both datasources answer and every dashboard is provisioned; the gateway answers;
and one call through the gateway, tagged with a fresh run id (`X-Agentic-Run-Id`), appears
in the tenant's `dash_calls` view when queried through Grafana's datasource, which logs in
as the tenant's dashboard reader. That is the whole path: gateway usage row, the
`postgres_fdw` link, the tenant-scoped views, the reader role and Grafana.

## Notes

- **Licences.** Grafana is AGPL-3.0: the stack runs the official image unmodified and ships
  only provisioning files and dashboard JSON; no Grafana code is copied, linked or changed.
  Postgres (PostgreSQL licence) and the uv/Python base image are permissive.
- **Images** are exact releases pinned by digest, each at least two weeks old; the
  control-plane image is built from `control-plane/Dockerfile`.
- **Gateway dashboards.** The LITE set's Postgres panels read the gateway's database
  (`app-postgres`). Its Prometheus panels show no data here: the local stack runs no
  Prometheus (see the track's deferred items).
- **Tenancy.** Grafana organisation 1 holds the one local tenant. In a multi-tenant
  deployment each tenant gets its own Grafana organisation whose `agentic-postgres`
  datasource logs in as that tenant's reader (`python -m agentic_cp db dashboard-role`);
  the gateway's own dashboards stay with the platform team.
