# SP15 Control Plane, Slice 1a Implementation Plan (track sp15-1a-control-plane)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build slice 1a of the SP15 control plane exactly as specified by Tasks 1-6 of the committed plan `docs/superpowers/plans/2026-10-21-sp15-control-plane.md` (the "parent plan"): a FastAPI + Postgres service, `agentic_cp`, that serves several isolated tenants from one deployment, with forced row-level security, a runtime role without `BYPASSRLS`, the tenant taken only from credentials, a hash-chained audit table per tenant that the runtime role can only append to, tenant-bound opaque tokens, tenant bootstrap through operator commands, and RBAC through pycasbin behind `authorize`/`require`. Stop at the parent plan's slice-1a checkpoint (Task 6 Step 6). Slice 1b (Tasks 7-11: policy sync, repos and CODEOWNERS, ingest, CLI side) is a later track.

This file does not restate the parent plan. It records what this track builds, the concrete names it uses where the parent plan leaves room, and every ruling it made.

**Rows:** M2-01 (skeleton, database, migrations, tenancy, token auth, OpenAPI), M2-03 core (RBAC catalogue, default roles, assignments, `authorize`, check API; the CODEOWNERS-derived maintainers are Task 8, slice 1b), M2-05 (audit chain per tenant, insert-only evidence).

**Architecture:** as in the parent plan's Architecture section, slice-1a parts only:
- `control-plane/src/agentic_cp/`: `settings.py`, `app.py`, `errors.py`, `db.py`, `tables.py`, `db_roles.py`, `migrations/` (Alembic, package data), `__main__.py` (`serve`, `migrate`, `db grant-runtime`, `admin ...`, `openapi`);
- `tenancy/` (`context.py`, `rls.py`, `tenants.py`, `ratelimit.py`), `audit/` (`chain.py`, `api.py`), `auth/` (`tokens.py`, `deps.py`, `api.py`, `bootstrap.py`), `rbac/` (`defaults.py`, `model.conf`, `authz.py` - the only casbin importer, `store.py`, `api.py`);
- core `agentic.orgapi` (`models.py`, `rbac.py`): the wire contract and the permission catalogue, no server libraries;
- commons `sdlc_commons.ledger.entry_hash`: Zyroflo's `approve.entry_hash`, so the audit chain uses the run ledger's formula.

**Tech Stack:** Python 3.12; FastAPI 0.142.4, Starlette 1.7.0 (already locked), uvicorn 0.54.0 (already locked), SQLAlchemy 2.0.54 Core + asyncio, asyncpg 0.31.0, greenlet 3.5.6, Alembic 1.20.0, pycasbin 2.8.0 (+ simpleeval 1.0.8, wcmatch 11.0.1, bracex 3.0.1); pytest with the anyio plugin; httpx's ASGI transport for API tests; `postgres:17.10-alpine` pinned by digest (released 2026-07-08) in a container through `CONTAINER_ENGINE`. `codeowners` is not added in 1a: Task 8 (slice 1b) is its only user, so it joins with that task.

## OSS reuse decisions

The parent plan's OSS table (checked 2026-10-21 and 2026-10-22) stands; this track re-checked only what it adds:

| Need | Reused | Not reused, why |
|---|---|---|
| Everything in parent plan Tasks 1-6 | As in the parent plan's table | As in the parent plan's table |
| Test Postgres image | `postgres:17.10-alpine@sha256:742f40ea...` (PostgreSQL licence; tag pushed 2026-07-08, older than two weeks) | `postgres:17.11` (pushed 2026-10-21, too young); the full Debian image (four times larger, same server) |
| SCRAM verifier for the runtime role's password | Own 15 lines over `hashlib.pbkdf2_hmac` + `hmac` (RFC 5802/7677 formula) | asyncpg and SQLAlchemy have no public helper; psycopg's `encrypt_password` is LGPL |
| Async test client | httpx `ASGITransport` (already locked) in the test's own event loop | Starlette `TestClient`: runs the app in a second event loop, which asyncpg connections cannot cross |

## Rulings made by this track (for the coordinator's decisions log)

- **R1 Credential lookups under FORCE RLS.** The parent plan has the definer functions owned by the owner role, but `FORCE ROW LEVEL SECURITY` applies to the table owner too, so `resolve_token` would find nothing. Each registered credential lookup sets `app.credential_lookup = 'on'` (transaction-local) inside its plpgsql body right before its one query and clears it before returning (a function-level `SET` of a placeholder setting needs a superuser; an error aborts the transaction, which undoes the setting too), and `tokens` and `users` get one extra registered policy, `credential_lookup`, `FOR SELECT TO <owner role>` with `USING (current_setting('app.credential_lookup', true) = 'on')`. The runtime role gains nothing (the policy is not granted to it), the owner role's admin commands see nothing extra unless they set the switch on purpose, and `enforcement_status` refuses any other extra policy, or this one if it is not `SELECT`-only or names the runtime role or `PUBLIC`.
- **R2 Unqualified table names.** `tables.metadata` has no schema; every engine sets `search_path` to the configured schema (`db_schema`, default `agentic`, validated as an identifier), and Alembic keeps its version table in that schema. Definer functions pin `search_path = pg_catalog, <schema>`.
- **R3 Test module names.** Generic test names get a `cp_` prefix (`test_cp_settings.py`, `test_cp_app.py`, `test_cp_auth.py`, `test_cp_matrix.py`, ...) so that no parallel track's `test_settings.py` stops collection (`scripts/tests/test_test_layout.py`).
- **R4 Enforcement status takes a role.** `enforcement_status(conn, role=None)` checks `role` or `current_user`; tests check a throw-away role made with `grant_runtime`, so a test that grants `BYPASSRLS` never changes the shared `cp_app` role.
- **R5 Team existence guard waits for Task 7.** Slice 1a has no policy tables, so "an assignment's team must exist in the active policy" cannot apply yet; any team slug is accepted (the parent plan's "before any policy" case). `test_assignment_to_unknown_team_refused_when_policy_active` moves to Task 7 (deferred).
- **R6 Default roles.** Permission kinds and role contents are transcribed from spec §12.3 and §23 into `agentic.orgapi.rbac.PERMISSIONS` and `rbac/defaults.py`; `control-plane/tests/rbac/matrix.py` restates the §23 matrix as expected scopes per role, independently of the code. `developer` holds `agent.edit.repo` (spec §24: "the developer (or team lead) runs `agentic init --repo`", which only opens a PR).
- **R8 No default table privileges for the runtime role.** `grant_runtime` grants table by table and `migrate` re-runs it after every upgrade, so a new table is closed to the runtime role until granted (a missed re-grant fails loudly instead of leaving a new evidence table writable). Default privileges only revoke `EXECUTE` on new functions from `PUBLIC`.
- **R9 `current_token` does not set the tenant context variable.** Routes open every transaction with `tenant_tx(engine, ctx.tenant_id)`; setting the variable inside a dependency could outlive the request when the ASGI app runs in the caller's task (tests, embedded use). A transaction opened without `tenant_tx` has no tenant, so it fails closed.
- **R10 `audit.unenforced` wins over `tenancy.unenforced` in `/readyz`** when both apply (the parent plan's owner-DSN test expects it); every reason goes to the server log, none to the caller.
- **R11 `skills.personal.edit` is a repo-kind permission**, so a derived repo maintainer holds it for that repo, as §23 ("own" for every role but auditor, exec viewer, service account) requires.
- **R7 Migrations are sync at the edge.** `migrate(owner_dsn, schema)` runs Alembic in its own event loop (for the CLI); `migrate_async` is the same over an existing loop (tests, `admin` commands).

## Global Constraints

The parent plan's Global Constraints apply in full to the slice-1a parts (wire contract, headers, tenancy, tokens, RBAC, audit records, evidence tables, import contracts, provider-agnostic, Windows and macOS, pins, formatting). In particular:
- Postgres runs only in a container (docker or podman through `CONTAINER_ENGINE`), never on the Windows host. Without an engine and without `AGENTIC_CP_TEST_DSN`, database tests skip with a reason naming both; with `AGENTIC_CP_REQUIRE_PG=1` (set by `check.py --linux`) they fail instead.
- `check.py --linux` creates a private network, starts the pinned Postgres on it as `pg`, runs the check container on that network with `AGENTIC_CP_TEST_DSN` and `AGENTIC_CP_REQUIRE_PG=1`, and removes both in a `finally`.
- Never `# fmt: skip` / `# fmt: off`; line length 100; check pytest's exit code directly; `uv run python scripts/check.py --all` green on Windows and Linux.
- TRACKER.md, DEFERRED.md and DECISIONS-*.md are not edited here; the structured output carries the updates.

## Review Focus

The parent plan's Review Focus items 2, 4, 5, 6 and 7 (slice-1a parts), plus:
1. **R1 lookup policy.** Only the registered definer functions can see `tokens`/`users` rows across tenants, only by exact hash, only `SELECT`, never as the runtime role. Tests: `test_resolve_token_returns_at_most_one_row_and_nothing_else`, `test_enforcement_status_reports_rls_gaps` (an unregistered extra policy), `test_owner_sees_no_tokens_without_the_lookup_switch`.
2. **The Postgres sidecar in `check.py --linux`.** Cleanup runs even when the checks fail; the DSN reaches the container by variable name, never on the command line. Test: `test_linux_stage_starts_postgres_sidecar_on_a_private_network`.

---

### Task 1: Package skeleton, settings, app factory, problem+json, OpenAPI snapshot (M2-01)

As parent Task 1, except `codeowners` (Task 8) is not pinned yet.
- Files: `control-plane/pyproject.toml`, root `pyproject.toml` (contracts "The CLI never imports the control plane" and "Server libraries only in the control plane"), `uv.lock`; `control-plane/src/agentic_cp/{settings.py, app.py, errors.py, __main__.py}`; `core/src/agentic/orgapi/{__init__.py, models.py}`; `control-plane/openapi.json`.
- Tests: `control-plane/tests/test_cp_settings.py`, `control-plane/tests/test_cp_app.py`, `control-plane/tests/conftest.py`.
- [ ] Step 1: write the parent plan's Task 1 tests. - [ ] Step 2: run, see them fail (no module). - [ ] Step 3: implement, `uv lock`, check upload times, lint-imports, licence check. - [ ] Step 4: pass. - [ ] Step 5: commit.

### Task 2: Database, migrations, tenants table, test Postgres (M2-01)

As parent Task 2.
- Files: `control-plane/src/agentic_cp/{db.py, tables.py, alembic.ini}`, `migrations/{env.py, script.py.mako, versions/0001_schema_tenants.py}`, `control-plane/tests/pg.py`, `scripts/check.py`. The image constant `POSTGRES_IMAGE` lives in `scripts/check.py`; `pg.py` reads it from there, so the two never differ.
- Tests: `control-plane/tests/test_cp_migrations.py`, `control-plane/tests/test_pg_fixture.py`, `scripts/tests/test_check_postgres.py`.
- [ ] Steps 1-5 as the parent plan.

### Task 3: Tenancy foundation (M2-01, owner row 76)

As parent Task 3, with R2 and R4.
- Files: `tenancy/{__init__.py, context.py, rls.py, tenants.py, ratelimit.py}`, `db_roles.py`, `migrations/versions/0002_tenancy.py`; `db.py`, `app.py` (`/readyz`), `__main__.py` (`db grant-runtime`).
- Tests: `control-plane/tests/isolation.py`, `test_tenancy.py`, `test_tenant_isolation.py`, `test_db_roles.py`, `test_no_raw_sql.py`.
- [ ] Steps 1-5 as the parent plan.

### Task 4: Hash-chained audit table per tenant, insert-only evidence (M2-05)

As parent Task 4.
- Files: `audit/{__init__.py, chain.py}`, `migrations/versions/0003_audit_log.py`, `commons/src/sdlc_commons/ledger.py` (`entry_hash`), `db_roles.py`, `tables.py`, `tests/isolation.py`; `__main__.py` (`admin audit verify`).
- Tests: `test_audit_chain.py`, `test_audit_enforcement.py`, `commons/tests/test_ledger_entry_hash.py`.
- [ ] Steps 1-5 as the parent plan.

### Task 5: Token authentication, users, tenant bootstrap, `/v1/me` (M2-01)

As parent Task 5, with R1.
- Files: `auth/{__init__.py, tokens.py, deps.py, api.py, bootstrap.py}`, `rbac/defaults.py`, `migrations/versions/0004_users_tokens_rbac.py`; `agentic.orgapi.models` (`Me`, `TenantRef`, `TokenIssued`, `TokenInfo`, `TokenRequest`), `__main__.py` (`admin tenant create|list`, `admin issue-token`), `app.py`, `tables.py`, `tests/isolation.py`.
- Tests: `test_cp_auth.py`, `test_bootstrap.py`, `test_api_isolation.py`.
- [ ] Steps 1-5 as the parent plan.

### Task 6: RBAC through pycasbin (M2-03)

As parent Task 6, with R5 and R6.
- Files: `control-plane/pyproject.toml` (pycasbin and its three pinned dependencies), root `pyproject.toml` ("casbin only behind authorize"; `casbin` in the server-libraries list), `uv.lock`; `core/src/agentic/orgapi/rbac.py`; `rbac/{__init__.py, model.conf, authz.py, store.py, api.py}`, `audit/api.py`.
- Tests: `core/tests/orgapi/test_orgapi_rbac.py`, `control-plane/tests/rbac/{matrix.py, test_cp_matrix.py, test_authz.py}`, `test_rbac_api.py`, `test_audit_api.py`.
- [ ] Steps 1-5 as the parent plan. - [ ] Step 6: slice-1a checkpoint (`openapi --check`, lint-imports, licence check, `check.py --all`, no `fmt:` markers) and the review.
