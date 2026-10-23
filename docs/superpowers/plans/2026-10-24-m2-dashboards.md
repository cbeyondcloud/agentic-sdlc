# M2 Dashboards Implementation Plan (M2-37, M2-38, M2-39)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Org admins, team leads and auditors see cost, runs, gates, limits, audit and adoption in Grafana, each tenant only its own. SQL views join the control plane's tables (runs, run events, gates, audit, repos, kill switches) with the gateway's (TokenLean) `usage_events` through its `tags` (ST-2) and `team` (ST-1). Cost per run, step, agent and team reconciles exactly with the gateway rows; calls without run tags are an `unattributed` line; unmeasured values are NULL, never 0. Seven dashboards ship as JSON with provisioning files. A docker-compose local org stack (control plane, Postgres, Grafana, TokenLean LITE in keyless mock mode) starts, stops and smoke-tests from bash and PowerShell, on Docker or Podman.

**Architecture:**
- **The gateway link (ruling L1).** The views never read TokenLean's tables directly. `usage_source` and `gateway_audit_source` are contract views in the control plane's schema with fixed, typed columns. A migration creates them as empty placeholders (`... WHERE false`), so every dashboard view compiles before any gateway exists. `python -m agentic_cp db link-usage` (owner role) uses `postgres_fdw` to import TokenLean's `usage_events` and `audit_events` into `<schema>_gateway` and re-points both contract views at them with `CREATE OR REPLACE VIEW` (same columns, so dependent views stay). One mechanism serves every topology: TokenLean's database on the same server, in the same database, or elsewhere. The remote session never carries our `app.tenant_id`, which TokenLean's own RLS reads with a different meaning.
- **Tenancy for Grafana (ruling T1).** Each tenant gets one Grafana reader role, registered in the new column `tenants.dashboard_role` by `python -m agentic_cp db dashboard-role --tenant S --role R`. The command creates or aligns the login role (no inherit, no grants but `SELECT` on the `dash_*` views and `USAGE` on the schema), sets its role default `app.tenant_id` to the tenant, and appends `dashboard.reader_bound` to the tenant's audit chain. The views run with their owner's rights (not `security_invoker`), so the reader never touches a table. Forced RLS still filters every framework table by `app.tenant_id`. `dash_scope` adds the binding: a session sees its `app.tenant_id` tenant only if it is that tenant's registered reader, or a role that may already `SELECT` the tenant tables directly (runtime, owner). A reader that runs `SET app.tenant_id = <other tenant>` therefore gets no rows. Gateway rows are filtered by the tenant's `gateway_tenant`; a tenant without one sees no gateway rows.
- **Views (`agentic_cp.dashboards.views`, created by migration `m2_37_usage_views`):** `usage_source`, `gateway_audit_source` (contracts); `dash_scope`; `dash_calls` (one row per gateway call with tag columns, `run_key`/`step_key`/`agent_key` that read `unattributed` when the tag is missing); `dash_step_runs` (one row per finished step execution: agent, outcome, reported cost, duration); `dash_runs`; `dash_run_cost`, `dash_step_cost`, `dash_agent_cost`, `dash_team_cost` (gateway cost next to the cost the engine reported, and their difference); `dash_gates`; `dash_agent_quality`; `dash_limit_events`; `dash_gateway_safety`; `dash_audit`; `dash_repos`. Every `dash_*` view has `tenant_id` first and joins `dash_scope`.
- **Dashboards (`agentic_cp.dashboards.catalog`):** the seven dashboards as Python data, rendered to Grafana JSON in `agentic_cp/dashboards/grafana/*.json` (package data). `python -m agentic_cp dashboards --write|--check` keeps the committed JSON equal to the catalog, like `openapi --check`. Every panel reads the datasource uid `agentic-postgres`: in a multi-tenant deployment each tenant's Grafana organisation provisions that uid with its own reader role, so the JSON is the same for everyone.
- **Local org stack (`deploy/org-local/`):** `docker-compose.yml` (project `agentic-org`): `cp-postgres` (the pinned Postgres image; first-start init creates the owner role, the database, `postgres_fdw` and the parameter grant), `cp-init` (one-shot: migrate, grant runtime, create tenant `local`, link usage, bind the reader), `control-plane` (`serve`), `grafana` (official image, provisioning only; its datasource files read host, reader and password from the container environment). Smoke ends with one tagged mock call through the gateway that must appear in `dash_calls` queried through Grafana's datasource. TokenLean LITE is started by TokenLean's own `deploy/lite/local/up.sh --mock` / `up.ps1 -Mock` from `TOKENLEAN_DIR` (read-only use; ruling S1), and our services join its network `tokenlean-lite_default`. `up`, `down`, `smoke` in bash and PowerShell; `CONTAINER_ENGINE=docker|podman`.

**Tech Stack:** Python 3.12, SQLAlchemy Core + Alembic + asyncpg (existing), Postgres 17 `postgres_fdw` (PostgreSQL licence, in the official image), Grafana `grafana/grafana:13.2.3` official image (AGPL-3.0, run unmodified as a separate service; ruling G1), compose v2. No new Python dependency.

**Spec:** §3 (responsibility split), §14.1-14.5 (observability, identity headers, dashboards, audit), §25 (deployment, LITE dashboards, network flows), §26 (reconciliation rule, unattributed line). Master plan rows M2-37, M2-38, M2-39; sp15 plan "Usage views (M2-37 consumers)".

## OSS reuse decisions

| Need | Reused | Not reused, why |
|---|---|---|
| Cross-database read of gateway usage | `postgres_fdw` (PostgreSQL licence, contrib, shipped in the official image) | `dblink`: untyped rows, no import; logical replication: a second copy to operate |
| Dashboards | Grafana official image, run unmodified; dashboards are our JSON | grafanalib 0.7.1 (Apache-2.0): a dependency for ~35 panels, and it trails Grafana's schema; grafana-foundation-sdk: generated, large, Go-first |
| Metric doctrines | Zyroflo `sdlc/dashboard.py` + `_content/sdlc_templates/grafana`: "unmeasured is NULL, never 0", "what cannot be attributed is a row", panels for human wait at gates, loop-backs, decisions by approver | Zyroflo's files themselves: a SQLite datasource over one repo's `audit.db`, and the control plane may not import Zyroflo |
| TokenLean LITE stack | TokenLean's own `deploy/lite/local` scripts and compose, started as documented | Copying or including its compose: duplicates its secret and key-file preparation, which only its `up` does |
| Gateway dashboards | TokenLean's `dashboard/dashboards/*.json` (LITE set: live, requests, billing, latency-breakup, sla, trust-safety, trends, quarterly), copied by `up` into the git-ignored `.state/` (a `:z` mount would relabel files in TokenLean's checkout) | Their Prometheus panels: no Prometheus in the local stack (deferred) |
| Smoke | curl / `Invoke-WebRequest` | — |

## Global Constraints

- **Control plane rules.** Build on the merged control plane as it is. The new column is on `tenants` (not tenant-owned), not writable by the runtime role (`TENANT_WRITABLE` unchanged), unique across tenants. No new tenant-owned table. Views are `relkind 'v'`, so `enforcement_status`'s table checks are unaffected; the completeness guard for views is new (Task 3). No new `/v1` route.
- **Every change on the audit chain.** `db dashboard-role` appends `dashboard.reader_bound` to the tenant's chain; `db link-usage` appends `gateway.usage_linked` to every active tenant's chain.
- **NULL, never 0.** A gateway measure with no matching call is NULL (calls, tokens, cost). A step without a reported cost is NULL. `runs.spent_usd` (default 0) is not used for reported cost; the views sum the `step_finished` costs. TokenLean columns that are `NOT NULL DEFAULT 0` are passed as they are; `status_code` 0 and `total_duration_ms` 0 become NULL.
- **Unattributed.** A call without a `run_id` tag has `run_key = 'unattributed'`; likewise `step_key`, `agent_key`, and `team` (empty authenticated team). Each cost view has that line; nothing is filtered out.
- **Team.** Cost per team uses the authenticated `usage_events.team` (ST-1), never the caller-asserted `tags.team`.
- **No vendor names** in panel titles or dashboard titles (test). "gateway" is the word for TokenLean in titles.
- **Images pinned** by exact tag plus digest, each at least two weeks old (released on or before 2026-10-07): Postgres = `scripts/check.py` `POSTGRES_IMAGE`; Grafana `13.2.3` (2026-09-29); the control-plane image builds on the repo's uv image `0.12.1-python3.12-trixie-slim`.
- **Scripts.** bash 3.2+ (macOS default) and PowerShell 5.1+; no jq; LF endings for `.sh` (`.gitattributes`); engine from `CONTAINER_ENGINE`; every compose call names the project with `-p agentic-org`; secrets reach containers through variables in a git-ignored env file, never on a command line.
- **Never modify the TokenLean repo.** Its `up` writes only its git-ignored `.env.lite` and `.state/`.
- **Formatting, CI and records:** no `# fmt: skip`/`# fmt: off`; line length 100; check pytest exit codes directly; `uv run python scripts/check.py --all` green on Windows and Linux; TRACKER.md, DEFERRED.md and DECISIONS are updated by the coordinator from this track's output.

## Review Focus

1. **A tenant's Grafana user never sees another tenant's rows.** Reader of a sees only a's rows in every `dash_*` view; after `SET app.tenant_id = <b>` it sees none; it cannot select any table or the gateway link schema; a role with no binding and no table rights sees none; tenant b's gateway rows (another `gateway_tenant`) never appear in a's sums. Test: Task 3 `test_reader_sees_only_its_tenant` (parametrised over every view), `test_reader_switching_tenant_sees_nothing`, `test_reader_cannot_read_tables_or_link`, `test_unbound_role_sees_nothing`, `test_every_dash_view_is_registered`.
2. **Cost reconciles exactly.** On the seeded database the sum of `gateway_usd` over `dash_run_cost`, `dash_step_cost`, `dash_agent_cost` and `dash_team_cost` each equals the tenant's gateway total (Decimal equality), per-run and per-step values equal the seeded sums, and the unattributed line equals the untagged calls. Test: Task 4 `test_cost_reconciles_per_dimension`, `test_unattributed_line_holds_untagged_calls`.
3. **Unmeasured is NULL.** A registered run with no calls has NULL calls and NULL gateway cost; a step with no reported cost has NULL reported cost; an unlinked deployment returns no gateway rows rather than zeros. Test: Task 4 `test_unmeasured_values_are_null`.
4. **The link survives and is replaceable.** `link-usage` on a migrated database with dependent views succeeds, re-running it is a no-op, a TokenLean without `team`/`tags` is refused with a message naming ST-1/ST-2. Test: Task 2.
5. **Every panel's query runs on the seeded database and returns rows**, through the tenant reader, with Grafana's macros expanded; every panel's datasource is provisioned. Test: Task 6 `test_every_panel_query_returns_rows`.
6. **The stack is engine-neutral and pinned.** Compose images carry a tag and a digest; scripts use `CONTAINER_ENGINE`, `-p agentic-org`, never print secrets; `.sh` files are LF. Test: Task 7 static tests; live smoke run reported in the track output.

---

### Task 1: Contract views, scope and data views (M2-37)

**Files:**
- Create: `control-plane/src/agentic_cp/dashboards/__init__.py`, `control-plane/src/agentic_cp/dashboards/views.py`, `control-plane/src/agentic_cp/migrations/versions/m2_37_usage_views.py`
- Modify: `control-plane/src/agentic_cp/tables.py` (`tenants.dashboard_role`)
- Test: `control-plane/tests/dashboards/test_dash_views.py`, seed helpers `control-plane/tests/dashboards/dash_seed.py`

**Interfaces:**
- `USAGE_COLUMNS: tuple[tuple[str, str, str], ...]` (name, SQL type, expression over TokenLean's `usage_events u`); `GATEWAY_AUDIT_COLUMNS` likewise.
- `placeholder_sql(columns) -> str`; `source_sql(columns, relation) -> str`.
- `DASH_VIEWS: tuple[str, ...]` (creation order); `view_statements(schema) -> list[str]`; `drop_statements() -> list[str]`.
- Migration `m2_37_usage_views` (down `0010`): add `tenants.dashboard_role text unique`, create every view.

- [ ] **Step 1: Failing tests:** `test_views_exist_after_migrate`, `test_placeholder_sources_are_empty`, `test_runtime_and_owner_see_their_tenant_in_scope`, `test_step_runs_read_cost_and_agent` (seeded run events), `test_bad_cost_value_is_null_not_an_error`.
- [ ] **Step 2: Run** `uv run pytest control-plane/tests/dashboards/test_dash_views.py -q`. Expected: FAIL (no module `agentic_cp.dashboards`).
- [ ] **Step 3: Implement.** **Step 4: Run.** Expected: PASS, and `test_cp_migrations.py` (metadata match, downgrade to base) still PASS.
- [ ] **Step 5: Commit.** `feat(cp): usage contract views and tenant-scoped dashboard views (M2-37)`

### Task 2: `db link-usage` (M2-37, ruling L1)

**Files:**
- Create: `control-plane/src/agentic_cp/dashboards/link.py`
- Modify: `control-plane/src/agentic_cp/__main__.py`
- Test: `control-plane/tests/dashboards/test_dash_link.py`

**Interfaces:**
- `@dataclass(frozen=True) GatewayDb(host, port, dbname, user, password: SecretStr, remote_schema="public")`.
- `class LinkError(Exception)`.
- `async link_usage(owner_conn, schema, gateway: GatewayDb) -> LinkResult(linked: tuple[str, ...])`: `CREATE EXTENSION IF NOT EXISTS postgres_fdw` (a DBA step when the owner may not), server `agentic_gateway` and the owner's user mapping (created or altered), `IMPORT FOREIGN SCHEMA ... LIMIT TO (usage_events, audit_events)` into a freshly emptied `<schema>_gateway`, checks the imported columns, `CREATE OR REPLACE VIEW` of both contracts.
- CLI: `python -m agentic_cp db link-usage --host H [--port 5432] --dbname D --user U --password-env VAR [--remote-schema public]` (the password only from the named variable); audits `gateway.usage_linked` per active tenant.

- [ ] **Step 1: Failing tests:** `test_link_points_contracts_at_gateway_rows`, `test_relink_is_idempotent`, `test_gateway_without_team_or_tags_is_refused`, `test_missing_audit_table_keeps_its_placeholder`, `test_command_reads_password_from_variable_only`, `test_link_is_audited_per_tenant`.
- [ ] **Step 2-4:** run (FAIL: no module), implement, run (PASS).
- [ ] **Step 5: Commit.** `feat(cp): db link-usage - gateway usage through postgres_fdw (M2-37)`

### Task 3: `db dashboard-role` and view isolation (M2-37, ruling T1, RF1)

**Files:**
- Create: `control-plane/src/agentic_cp/dashboards/readers.py`
- Modify: `control-plane/src/agentic_cp/__main__.py`
- Test: `control-plane/tests/dashboards/test_dash_isolation.py`

**Interfaces:**
- `async bind_reader(owner_conn, schema, tenant, role, password: str | None, *, clock) -> None`: refuses the runtime or owner role name, a name bound to another tenant, and a views owner that is a superuser or has BYPASSRLS; creates or alters the role (`LOGIN NOINHERIT`), revokes its memberships and table rights, grants `USAGE` on the schema and `SELECT` on every `dash_*` view, sets `app.tenant_id` and `search_path` as role defaults, stores `tenants.dashboard_role`, audits `dashboard.reader_bound`. Needs `GRANT SET ON PARAMETER app.tenant_id TO <owner>` once (a DBA step; the error says so).
- CLI: `python -m agentic_cp db dashboard-role --tenant S --role R [--password-env VAR]`.

- [ ] **Step 1: Failing tests** (RF1): `test_reader_sees_only_its_tenant[<view>]` for every `DASH_VIEWS` entry, `test_reader_switching_tenant_sees_nothing`, `test_reader_cannot_read_tables_or_link`, `test_unbound_role_sees_nothing`, `test_every_dash_view_is_registered` (the schema's `dash_*` views equal `DASH_VIEWS`, and each has `tenant_id` first), `test_role_bound_to_another_tenant_is_refused`, `test_runtime_role_cannot_be_a_reader`, `test_bind_is_audited`.
- [ ] **Step 2-4:** run, implement, run.
- [ ] **Step 5: Commit.** `feat(cp): tenant-bound dashboard reader roles (M2-37)`

### Task 4: Reconciliation on a seeded database (M2-37, RF2, RF3)

**Files:**
- Modify: `control-plane/tests/dashboards/dash_seed.py` (two tenants, runs, step and gate events, audit records, repos, kill switches; a TokenLean database with `usage_events` and `audit_events` on the same server, linked with `link_usage`; tenant b has its own `gateway_tenant`)
- Test: `control-plane/tests/dashboards/test_dash_reconcile.py`

- [ ] **Step 1: Failing tests:** `test_cost_reconciles_per_dimension` (run, step, agent, team; Decimal equality with the gateway total and with each seeded per-run/per-step sum), `test_unattributed_line_holds_untagged_calls`, `test_unmeasured_values_are_null`, `test_reported_and_gateway_cost_side_by_side` (DF-438), `test_team_is_the_authenticated_team_not_the_tag`, `test_other_gateway_tenant_never_counted`.
- [ ] **Step 2-4:** run, fix views, run.
- [ ] **Step 5: Commit.** `test(cp): dashboard views reconcile with gateway usage (M2-37)`

### Task 5: Seven dashboards as JSON (M2-38)

**Files:**
- Create: `control-plane/src/agentic_cp/dashboards/catalog.py`, `control-plane/src/agentic_cp/dashboards/grafana/{org,team,run-explorer,agent-quality,limits-safety,audit-explorer,adoption}.json`
- Modify: `control-plane/src/agentic_cp/__main__.py` (`dashboards --write|--check`)
- Test: `control-plane/tests/dashboards/test_dash_catalog.py`

**Interfaces:**
- `Panel(title, kind: "stat"|"timeseries"|"table"|"barchart", sql, unit=None, w=12, h=8)`, `Variable(name, label, sql, multi=True)`, `Dashboard(uid, title, description, panels, variables=(), time_from="now-30d")`.
- `DASHBOARDS: tuple[Dashboard, ...]` (seven); `render(dashboard) -> dict`; `rendered_files() -> dict[str, str]` (file name -> JSON text, sorted keys, LF).
- `DATASOURCE = {"type": "grafana-postgresql-datasource", "uid": "agentic-postgres"}`.
- Macros allowed in SQL: `$__timeFilter(col)`, `$__timeGroupAlias(col, interval)`, `$__timeFrom()`, `$__timeTo()`, and the dashboard's own variables.

- [ ] **Step 1: Failing tests:** `test_seven_dashboards_with_unique_uids`, `test_no_vendor_names_in_titles`, `test_committed_json_is_current`, `test_panels_use_only_known_macros_and_variables`, `test_panels_read_dash_views_only`.
- [ ] **Step 2-4:** run, implement and `--write`, run.
- [ ] **Step 5: Commit.** `feat(cp): seven Grafana dashboards rendered from a catalog (M2-38)`

### Task 6: Provisioning test (M2-38, RF5)

**Files:**
- Create: `deploy/org-local/grafana/provisioning/dashboards/agentic.yaml` (providers `agentic` and `gateway`), `deploy/org-local/grafana/provisioning/datasources/{agentic,gateway}.yaml` (`agentic-postgres` as the tenant reader; TokenLean's `app-postgres` uid for its LITE set; values from the container environment)
- Test: `control-plane/tests/dashboards/test_dash_provisioning.py`, helper `control-plane/tests/dashboards/grafana_macros.py`

- [ ] **Step 1: Failing tests:** `test_provisioning_loads_every_dashboard` (provider paths resolve to the committed JSON; every panel's datasource uid is provisioned by `cp-init`'s datasource template or the gateway file), `test_every_panel_query_returns_rows` (seeded database, tenant reader, macros and variables expanded), `test_macro_expansion_refuses_unknown_macros`.
- [ ] **Step 2-4:** run, implement, run.
- [ ] **Step 5: Commit.** `test(cp): Grafana provisioning loads all dashboards; every panel returns rows (M2-38)`

### Task 7: Local org stack (M2-39, ruling S1)

**Files:**
- Create: `control-plane/Dockerfile`, `.dockerignore`, `deploy/org-local/{README.md,docker-compose.yml,.env.org.template,_lib.sh,_lib.ps1,up.sh,up.ps1,down.sh,down.ps1,smoke.sh,smoke.ps1}`, `deploy/org-local/cp-postgres/init.sh`, `deploy/org-local/cp-init/init.sh`, `.gitattributes` entries
- Test: `scripts/tests/test_org_stack.py` (static)

- [ ] **Step 1: Failing tests:** `test_images_pinned_by_tag_and_digest`, `test_scripts_exist_in_bash_and_powershell`, `test_scripts_name_the_project_and_engine`, `test_shell_scripts_are_lf`, `test_no_secret_on_a_command_line`, `test_bind_mounts_carry_selinux_label` (DF-162 M11 for our mounts), `test_tokenlean_repo_only_read`.
- [ ] **Step 2-4:** run, implement, run.
- [ ] **Step 5:** live run on this machine: `up.ps1`, `smoke.ps1`, `down.ps1` (and bash via Git Bash); record results in the track output.
- [ ] **Step 6: Commit.** `feat(deploy): local org stack - control plane, Postgres, Grafana, TokenLean LITE (M2-39)`

### Task 8: Deferred items pulled in

- DF-176: rename the step span attribute to `agentic.aggregated_cost_usd` (test first in `core/tests/agents/test_call_spans.py`).
- DF-438, DF-358 (M2-37 view note), DF-223/DF-345 (view part): reported and gateway cost side by side per run/step (Task 4); spend the engine did not record still appears on the gateway side.
- DF-162 M11: SELinux `:z` on our bind mounts (Task 7). M9 and TokenLean's own mounts: TokenLean repo.
- Retargeted or kept open with reasons in the track output: DF-111, DF-132, DF-168, DF-170, DF-215, DF-427, DF-450, and the iteration half of DF-223/DF-345/DF-358.

### Task 9: Docs and full check

- [ ] Control-plane README: dashboards section (link, reader roles, DBA one-time grants, per-tenant Grafana organisations).
- [ ] `uv run python scripts/check.py --all` green on Windows and Linux; fix and re-run.

## Rulings (for the owner's review)

| Problem | Alternatives | Decision | Why |
|---|---|---|---|
| L1: how views reach TokenLean's tables | same-database schema read; `dblink`; `postgres_fdw` | `postgres_fdw` behind fixed contract views, set up by `db link-usage` | Works for one server, one database or two; TokenLean's RLS reads `app.tenant_id` with another meaning, so a same-session read would be filtered wrongly; one place absorbs TokenLean schema drift |
| T1: tenant isolation for Grafana | one reader per tenant with forced-RLS GUC only; tenant-filtered views; a binding table | owner-rights views + forced RLS + `dash_scope` binding via `tenants.dashboard_role` | A reader can `SET app.tenant_id`; the binding makes that return nothing, and the reader has no table rights at all |
| G1: Grafana licence (AGPL-3.0) | vendor Grafana; build on its code; run the official image | run the official image unmodified; ship only dashboard JSON and provisioning files | No Grafana code is copied, linked or modified |
| S1: TokenLean LITE in the stack | compose `include`; published image; TokenLean's own `up` | TokenLean's own `up --mock`, our services join its network | Its `up` prepares secrets, key file and overlays; no published image exists |
| D1: one datasource uid for every tenant | per-tenant dashboard copies | uid `agentic-postgres`, provisioned per Grafana organisation with that tenant's reader | Same JSON for every tenant; Grafana org = tenant boundary in the UI, the database is the real one |
