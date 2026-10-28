# SP15 Control Plane Implementation Plan (slice 1a: tenancy, auth, RBAC, audit; slice 1b: policy sync, repos, ingest, CLI; slice 2: M2-06..M2-18 interfaces)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Revision 2026-10-22 (owner decisions, DECISIONS-overnight-2026-10-21.md rows 74-77):** org admins may approve any gate when they hold `gate.approve` (row 74); opaque hashed tokens, 90 days for users and at most 365 for service accounts (row 75); **multi-tenant from the start**, isolated in Postgres with row-level security (row 76); **RBAC through pycasbin**, RBAC-with-domains where the domain is the tenant (row 77). Rows 76 and 77 make slice 1 too large for one review cycle, so it is split into **slice 1a** (Tasks 1-6: skeleton, database, tenancy/RLS, audit, auth, RBAC) and **slice 1b** (Tasks 7-11: policy sync, repos and CODEOWNERS, ingest, CLI side, entry points). Each half ends with a full check and a review.

**Goal:** Org mode gets its server. A small FastAPI + Postgres service (`agentic-control-plane`, package `agentic_cp`) that serves several fully isolated organisations (tenants) from one deployment and:
- keeps every tenant's rows apart in Postgres itself: row-level security on every tenant-owned table, the tenant set per transaction, and no rows at all when no tenant is set (row 76);
- authenticates every caller with a revocable control-plane token bound to exactly one tenant; the tenant comes only from the token (M2-01);
- keeps a hash-chained audit table per tenant that the running service can only append to; each tenant's chain verifies on its own (M2-05);
- holds roles, permissions and team-scoped assignments per tenant, derives repo maintainers from CODEOWNERS, and answers permission checks through pycasbin behind one `authorize` function (M2-03);
- pulls each tenant's git policy repo, validates it with the same core code the CLI uses, versions it as `git:<commit>`, keeps the previous version when a new one is invalid, and serves the validated bundle (M2-02);
- ingests each run's Zyroflo event chain, verifying it entry by entry, idempotently, into the caller's tenant (M2-04).

The CLI side of each piece ships with it: `agentic login --for control-plane` verifies the token, org-mode `init` fetches the policy bundle from the control plane, roles come from the control plane instead of the user config, and a `ControlPlaneSink` delivers ledger entries (wired into runs by M2-20). The CLI never names a tenant; the server derives it from the token.

**Architecture:**
- **Package `agentic_cp`** (`control-plane/src/agentic_cp/`, layout from spec §4.1):
  - `settings.py`, `app.py` (`create_app`), `errors.py` (RFC 9457 problem+json), `db.py` + `tables.py` (SQLAlchemy Core), `db_roles.py` (runtime role grants, enforcement probe), `migrations/` (Alembic, shipped as package data), `__main__.py` (`serve`, `worker`, `migrate`, `admin`, `db`, `openapi`);
  - `tenancy/` (tenant context, RLS migration helper, tenant rows, rate-limit hook), `auth/` (tokens, token context dependency, tenant bootstrap), `audit/` (chain), `rbac/` (catalogue store, `authz.py`: the only module that imports casbin, `model.conf`, CODEOWNERS maintainers), `policy_sync/` (git fetch, validate, versions, bundle, webhook), `ingest/` (chain verification, run store).
- **Shared wire contract in core: `agentic.orgapi`** (pydantic models, the permission catalogue and a read-only view of server-computed permissions; no HTTP, no server libraries, no casbin). The control plane and the CLI both import it, so the contract has one definition. Core never imports `agentic_cp`; the CLI never imports `agentic_cp`.
- **Chain code is not rewritten.** `sdlc_commons.ledger` gains `entry_hash` (Zyroflo's `approve.entry_hash`) and `verify_entries` (Zyroflo's chain rules over entries in memory). Ingest verifies with it; the audit table hashes with the same formula, so one verifier (M2-26) checks both chains.
- **Tenancy in the database, not only in code.** A `tenants` table; `tenant_id uuid NOT NULL` leads every key of every tenant-owned table; each such table has `ENABLE` and `FORCE ROW LEVEL SECURITY` and one `tenant_isolation` policy (`USING` and `WITH CHECK`) on `app.tenant_id`. The API connects as a runtime role that owns nothing and has `NOBYPASSRLS`. The tenant is set per transaction (`set_config('app.tenant_id', $1, true)`, the bindable form of `SET LOCAL`) by a SQLAlchemy `begin` event that reads a context variable; without it the predicate is NULL, so reads return no rows and writes are refused. The only cross-tenant reads are three registered `SECURITY DEFINER` functions that map a credential hash (token, invite, webhook) to its tenant, plus `active_tenant_ids()` for the worker.
- **RBAC through pycasbin, confined.** `agentic_cp.rbac.authz` builds one in-memory, read-only casbin enforcer per tenant from our own typed tables (`roles`, `assignments`, `repo_maintainers`) and rebuilds it when the tenant's `rbac_revision` moves. Callers only see `authorize(ctx, permission, resource)` / `require(...)`; an import-linter contract keeps casbin out of every other module.
- **Processes:** `serve` (stateless API, 2+ replicas behind a Service, spec §25) and `worker` (policy sync and CODEOWNERS refresh, looping over tenants; one active replica through a Postgres advisory lock). Tables live in the Postgres schema `agentic`, so the service can share TokenLean's database server (spec §25 topology).
- **Two database roles** (TokenLean `src/proxy/audit/enforcement.py` pattern, re-implemented): the owner role runs migrations and grants; the runtime role the API uses owns no table, cannot bypass RLS, and may only `SELECT, INSERT` on the evidence tables `audit_log`, `run_events` and `policy_versions`.

**Tech Stack:** Python 3.12; FastAPI 0.142.4, Starlette 1.7.0, uvicorn 0.54.0 (no `[standard]` extra); SQLAlchemy 2.0.54 (Core, asyncio) with asyncpg 0.31.0 and greenlet 3.5.6; Alembic 1.20.0; pycasbin 2.8.0 (with simpleeval 1.0.8, wcmatch 11.0.1, bracex 3.0.1); codeowners 0.9.0; git CLI; httpx2 (CLI client, D36); pytest with the anyio plugin; Postgres 17 in a container (docker or podman, D40). Details and sources below.

**Spec:** `docs/superpowers/specs/2026-10-16-agentic-sdlc-design.md` §3 (modes, responsibility split), §4.1 (layout), §12.1 (policy as code), §12.3 + §23 (RBAC, permission matrix, ownership D30), §13 (identity), §14.5 (audit record fields, two chains), §25 (deployment), decisions D5, D12, D14, D16, D17, D18, D19, D33, D40, D43. Master plan rows M2-01..M2-18 and the "Deployment landscape" and "Users and roles" sections.

**Spec deltas to record (coordinator):** row 77 supersedes the §1.2 non-goal "a general policy engine in v1" for RBAC (policy-as-code for org settings is unchanged; Cedar stays the later option for richer rules). Row 76 supersedes the single-tenant assumption ("Single tenant in an org deployment" in §25's TokenLean dashboard table, and the §25 topology where one control plane serves one org). The TokenLean LITE profile (one tenant) then needs one TokenLean tenant per control-plane tenant (ND-16).

**Prerequisite:** start after batch 4 (acceptance M1-68..71 and the M1-81 hardening tracks) has merged into `m1-foundation`. Task 10 edits `cli/.../commands/login.py`, `policy_source.py`, `workspace.py`, `commands/init.py`, `repo_guard.py` and `publishing.py`, which the CLI hardening track may also touch; Task 8 moves the repo matcher out of `repo_guard.py`. Branch: `track/sp15-control-plane` (slice 1a merges first; slice 1b continues on the same branch after the 1a review).

## OSS reuse decisions

Checked 2026-10-21 (first pass) and 2026-10-22 (pycasbin, adapters, tenancy libraries) against the PyPI JSON API (`https://pypi.org/pypi/<name>/json`, release upload dates) and each project's licence metadata. Every pin from the first pass was released on or before 2026-10-07; every pin added on 2026-10-22 was released on or before 2026-10-08 (two-week rule).

| Need | Reused (licence, version, released) | Rejected, why |
|---|---|---|
| HTTP API, OpenAPI | FastAPI (MIT) **0.142.4**, 2026-10-07; Starlette (BSD-3) **1.7.0**, 2026-09-23, pinned explicitly | FastAPI 0.143+ (after the cutoff). Starlette 1.9.0 is in `uv.lock` today through mcp (code-review-graph extra) but was released 2026-10-18; pinning 1.7.0 satisfies mcp (`>=0.27`), sse-starlette (`>=0.49.1`) and fastmcp (`>=1.0.1`). Fallback if the strict 14x24 h rule is applied at lock time: FastAPI 0.142.2 (2026-09-30) |
| ASGI server | uvicorn (BSD-3) **0.54.0**, 2026-09-25, plain (h11, pure Python) | `uvicorn[standard]`: uvloop has no Windows build and httptools is native; neither is needed at this scale |
| SQL toolkit | SQLAlchemy (MIT) **2.0.54**, 2026-09-15, Core only (tables and expressions, no ORM) | SQLAlchemy 2.1.x: 2.1.0 went GA 2026-09-24 and had four patch releases in two weeks; revisit at M3. ORM: append-only evidence tables, explicit grants and RLS read better as Core |
| Migrations | Alembic (MIT) **1.20.0**, 2026-09-11 (pulls Mako 1.4.3 MIT, MarkupSafe BSD-3) | — |
| Postgres driver | asyncpg (Apache-2.0) **0.31.0**, 2025-11-24; greenlet (MIT AND PSF-2.0, allowed by `licence_check.py`) **3.5.6**, 2026-09-14, via `sqlalchemy[asyncio]` | psycopg 3.3.6 (LGPL-3.0-only) and psycopg2 (LGPL): outside the allow-list. pg8000 1.31.5 (BSD-3, pure Python, 2025-09-14) is the fallback if native wheels are refused (ND-9). asyncpg 0.32.0 (2026-10-06) is acceptable once older |
| Tenant isolation | Postgres row-level security (built in) plus own `agentic_cp.tenancy` (about 60 lines: context variable, `begin` event, migration helper) | fastapi-tenancy 0.5.0 (MIT, 2026-07-28): a general framework (schema, database, RLS and hybrid strategies), pre-1.0 with five releases since 2026-02; we need one strategy and token-only tenant resolution. rls 0.3.2 (2026-04-24): no licence declared, so outside the allow-list. Schema-per-tenant (N copies of every migration; one wrong `search_path` leaks) and database-per-tenant (operational cost per org): weaker or costlier than RLS for one developer track. Application-only `WHERE tenant_id = ...`: one missed filter leaks; the owner asked for enforcement in Postgres |
| Control-plane tokens | stdlib `secrets` + `hashlib` (opaque bearer tokens, sha256 at rest; owner row 75) | PyJWT 2.15.1 (MIT): JWTs cannot be revoked instantly without a deny list; JWT/OIDC acceptance comes with SSO (phase 3) |
| RBAC engine | **pycasbin (Apache-2.0) 2.8.0, uploaded 2026-02-02** (the newest release on PyPI on 2026-10-22; distribution `pycasbin`, import name `casbin`, `requires_python >=3.8`; source github.com/casbin/pycasbin). RBAC with domains (owner row 77). Its two dependencies are pinned explicitly because their newest releases are too young: simpleeval (MIT) **1.0.8**, 2026-09-12; wcmatch (MIT) **11.0.1**, 2026-08-14 (11.1 is 2026-10-10, 11.2 is 2026-10-22); bracex (MIT, needed by wcmatch) **3.0.1**, 2026-07-20 (3.1 is 2026-10-11, 3.2 is 2026-10-22). None are in `uv.lock` today | Own typed checks (the earlier default; the owner chose an engine for future custom roles without code changes). The legacy `casbin` distribution 1.43.0 (2025-05-10): the project now publishes as `pycasbin`. oso 0.27.3 (2024-01): deprecated by its vendor. Cedar (cedarpy): the spec's later option for attribute rules, not needed for RBAC |
| RBAC policy storage | **Own typed tables** (`roles`, `assignments`, `repo_maintainers`, all tenant-owned with RLS) loaded into a read-only per-tenant enforcer by a small adapter inside `authz.py`; cross-replica freshness through `tenants.rbac_revision` (bumped in the same transaction as any RBAC change; returned by token resolution, so no extra query). Decision DR-1 below | casbin-async-sqlalchemy-adapter (Apache-2.0) **1.17.0**, 2025-12-10: passes the licence and age checks and can join our transaction (`use_session`) and take a custom `db_class`, but its rule shape is the generic `casbin_rule(ptype, v0..v5)`, which cannot carry the composite tenant foreign keys and the guards we need (last org admin, team-assignable roles, derived-only maintainers); it is ORM (`AsyncSession`) in a Core-only service; writing through the enforcer updates only the replica that made the change, so the other replicas still need a watcher; and the part it does (persisting rule rows) is what our typed tables already do. casbin-sqlalchemy-adapter 1.4.0 (Apache-2.0, 2024-07-08): synchronous. casbin-databases-adapter 1.6.0 (Apache-2.0, 2025-11-14): pins `asynccasbin==1.1.7` (an old fork) and adds `databases` |
| CODEOWNERS parsing | codeowners (MIT) **0.9.0**, 2026-06-28, no dependencies (sbdchd/codeowners, follows hmarr's Go library) | Own parser: last-match-wins and glob anchoring are easy to get subtly wrong |
| Audit chain + insert-only role | Own code; pattern from TokenLean `src/proxy/audit/enforcement.py` (Apache-2.0, same owner): restricted LOGIN role, `SELECT, INSERT` only on audit tables, an `enforcement_status` probe, here extended with the RLS checks. Hash formula: Zyroflo `approve.entry_hash` through `sdlc_commons` | Copying TokenLean's module: it is asyncpg-specific and TokenLean's audit rows are not chained; the pattern is re-implemented and attributed |
| Policy repo fetch | git CLI (already required by the CLI's policy clone; run as a separate process, one work folder per tenant) | dulwich 1.2.17 (Apache-2.0 OR GPL-2.0-or-later, 2026-10-03): a second git implementation whose proxy, CA bundle and credential-helper behaviour differs from git's on corporate networks (spec §15) |
| Background jobs | None: `worker` process with asyncio loops over tenants; leader by `pg_try_advisory_lock` | APScheduler 3.11.3 (MIT): one periodic job does not need a scheduler; retention and alert jobs become K8s CronJobs (spec §25) |
| Settings | Own pydantic model read from `AGENTIC_CP_*` variables (unknown `AGENTIC_CP_*` names are refused). Per-tenant settings (policy repo, branch) live in the `tenants` table, not in the environment | pydantic-settings: the locked 2.16.0 was released 2026-10-14 (breaks the age rule) and silently ignores typos by default |
| SMTP (slice 2, M2-07) | aiosmtplib (MIT) **5.1.3**, 2026-09-08; tests capture with aiosmtpd (Apache-2.0) **1.4.6**, 2024-05-18 | — |
| Metrics (slice 2, M2-12) | prometheus-client (Apache-2.0 AND BSD-2-Clause) **0.26.0**, 2026-07-24; `promtool` only inside the Linux check container | — |
| Test Postgres | Own session fixture that starts `postgres:17` through the docker/podman CLI (`scripts/check.py`'s `container_engine()`, D40), or uses `AGENTIC_CP_TEST_DSN` | testcontainers 4.15.0 (Apache-2.0): talks to the Docker Engine API (podman needs its socket service) and starts a Ryuk reaper container. pytest-postgresql 9.1.1: LGPL-3.0-or-later and needs local Postgres binaries (unsigned on Windows laptops). testing.postgresql 1.3.0: unmaintained since 2016 |
| Async tests | anyio's pytest plugin (anyio 4.15.1, MIT, already locked); Starlette's TestClient over httpx 0.28.1 (BSD-3, already locked) | pytest-asyncio 1.4.0: one more dependency for what anyio already does |
| Postgres server | `postgres:17` image (PostgreSQL licence), pinned by digest of a release at least two weeks old; supported range 15+ (ND-12; RLS with `FORCE` is available on all of them) | — |

## Needs decision (owner)

Each open item names the recommended default the plan builds unless the owner says otherwise. The coordinator records open items in DEFERRED.md as `Needs decision` against the named row.

| ID | Question | Status / default built by this plan | Row |
|---|---|---|---|
| ND-1 | RBAC engine | **Decided by the owner (decisions row 77): pycasbin**, RBAC with domains, domain = tenant; the §23 matrix becomes the seed policy with one test per cell. Storage per DR-1 below | M2-03 |
| ND-2 | How do CLIs get the org policy in org mode: from the control plane, or by cloning the policy repo themselves? | From the control plane: the tenant's validated `git:<commit>` bundle, digest-checked. Developers need no read access to the policy repo, and a commit the control plane refused never reaches a laptop. Cloning stays for orgs without a control plane and for standalone mode | M2-02 |
| ND-3 | Control-plane token form and lifetime | **Decided by the owner (decisions row 75):** opaque bearer tokens (`acp_` + 32 random bytes), stored as sha256, revocable at once; user tokens expire after 90 days (renewed with `agentic login`), service-account tokens at most 365 days (M2-14). Each token is bound to one tenant | M2-01 |
| ND-4 | How a tenant and its first org admin are created | A server-side operator command, `python -m agentic_cp admin tenant create --slug <s> --name <n> --admin-email <e> [--policy-repo <url>] [--policy-branch <b>]` (run via `kubectl exec` or `compose run`): creates the tenant, seeds its roles, creates the first org admin and prints that token once. There is no in-app cross-tenant role; the platform operator acts only through server commands, each audited in the affected tenant's chain as `system:operator`. Tenant self-service is out of scope | M2-01 |
| ND-5 | Fail closed when the database does not enforce insert-only audit or tenant isolation | Yes: `/readyz` answers 503 (`audit.unenforced` or `tenancy.unenforced`) and `serve` refuses to start; `AGENTIC_CP_REQUIRE_DB_ENFORCEMENT=false` only for local development | M2-05 / M2-01 |
| ND-6 | Where repo maintainers come from, and how a CODEOWNERS `@handle` maps to a person | The control plane fetches CODEOWNERS itself over git from the repo's default branch (provider-neutral, never trusts a client copy). Handles are host-qualified (`github.com:alice`), per tenant, and set only by the tenant's admins or the CSV import (M2-14), never self-declared. `@org/team` entries are reported as unresolved until host team sync (phase 3 IdP/group sync) | M2-03 |
| ND-7 | An ingested run whose `policy_version` the tenant never accepted (a locally edited policy, or a version known only to another tenant) | Accept the events (evidence is never dropped), mark the run `policy_known = false` and write an `ingest.unknown_policy_version` audit record; M2-21 refuses such runs before they start | M2-04 |
| ND-8 | Does an org admin satisfy any gate's `approver_role`? | **Decided by the owner (decisions row 74): yes, if they hold `gate.approve`.** Four-eyes still applies; every approval records the approver and the role actually held (`org-admin` or the named role) | M2-03 / M2-21 |
| ND-9 | Server-side native wheels (asyncpg, greenlet, SQLAlchemy's compiled extension) installed on framework developers' laptops through the workspace dev group | Accept: the control plane never ships to the 1000 user laptops (an import contract keeps server libraries out of the CLI), and core already uses native wheels (pydantic-core, regex). Fallback: pg8000 (pure Python, sync) | M2-01 |
| ND-10 | Tenancy | **Decided by the owner (decisions row 76): multi-tenant from the start**, isolated by Postgres RLS, tenant only from the token, per-tenant policy repo, audit chain, CODEOWNERS and rate-limit hook, isolation proven per table and per endpoint family | M2-01 |
| ND-11 | Run links in PR bodies in org mode (DF-291) | `<control_plane_url>/v1/runs/<run_id>` (the JSON run summary) until a web UI exists (phase 3); handoff links stay repo-relative, because handoff bodies are not uploaded. The URL carries no tenant; the reader's token decides | M2-04 |
| ND-12 | Supported Postgres versions | 15 and later (managed services all offer 15-17); CI tests on 17 | M2-01 |
| ND-13 | Should `agentic login --for control-plane --url X` write `control_plane_url` into `~/.agentic/config.yaml`? | No for now (writing user config is DF-274, still open): the URL comes from the user config or `AGENTIC_CONTROL_PLANE_URL`, which the invite (M2-18) and MDM set; `--url` only overrides for that command | M2-01 / M2-18 |
| ND-14 (new) | How a policy-repo webhook finds its tenant without a bearer token | The webhook URL carries an opaque per-tenant hook token (`/v1/policy/webhook/acw_<32 bytes>`), stored as sha256 and resolved like a bearer token; the payload is ignored (it only nudges that tenant's fetch). GitHub-style HMAC is dropped because it needs the raw secret stored server-side; a leaked URL can only cause extra fetches (rate-limited) and is rotated by the tenant's admin | M2-02 |
| ND-15 (new) | Git credentials for each tenant's policy repo and CODEOWNERS fetches | Never stored in the database or in a URL (a URL with credentials is refused). git runs with `GIT_CONFIG_GLOBAL=<AGENTIC_CP_GIT_CONFIG_DIR>/<tenant-slug>.gitconfig` when that file exists (a mounted K8s secret naming a credential helper or `http.extraHeader` file), else the deployment default | M2-02 / M2-03 |
| ND-16 (new) | Mapping control-plane tenants to TokenLean tenants (slice 2: routing, keys, usage, budgets) | One TokenLean tenant per control-plane tenant, recorded as `tenants.gateway_tenant` at `admin tenant create --gateway-tenant`; the LITE profile's single TokenLean tenant is then the one-tenant special case | M2-09 / M2-11 / M2-17 |
| ND-17 (new) | A person who works in two tenants of one deployment | Two separate user records (one per tenant, possibly the same email) with separate tokens; the CLI holds one control-plane token per URL, so switching tenant is `agentic login --for control-plane` again. Per-workspace tokens are a later item (new DF) | M2-01 |

## Decisions made in this revision (for the decisions log)

| ID | Decision | Why |
|---|---|---|
| DR-1 | RBAC policy lives in our typed, RLS-protected tables; casbin gets a read-only adapter inside `authz.py`, one enforcer per tenant, rebuilt (never mutated) when `tenants.rbac_revision` changes; the revision is bumped in the same transaction as the RBAC change and its audit record | A qualifying adapter exists (casbin-async-sqlalchemy-adapter 1.17.0) but its generic rule rows cannot carry tenant foreign keys or our guards, and it does not solve freshness across replicas; one write path keeps casbin's memory from drifting from the tables |
| DR-2 | Casbin domain string is `<tenant_id>|org`, `<tenant_id>|team:<slug>` or `<tenant_id>|repo:<host/owner/name>`; `authorize` asks only the scopes the permission's catalogue kind allows, and refuses any domain whose tenant differs from the token's | Keeps the tenant as the domain root (owner row 77) and keeps the spec's team and repo scopes without per-team role copies |
| DR-3 | Credential-to-tenant resolution goes only through registered `SECURITY DEFINER` functions with a pinned `search_path` and exact-hash lookup | The tenant is unknown until the token is resolved; a definer function is the narrowest hole through RLS and is checked by `enforcement_status` |
| DR-4 | `AGENTIC_CP_REQUIRE_DB_ENFORCEMENT` replaces `AGENTIC_CP_REQUIRE_AUDIT_ENFORCEMENT` and covers both insert-only evidence and RLS | One fail-closed switch for the two database guarantees |
| DR-5 | Another tenant's object answers exactly like a missing one (404, same problem body); 403 only for objects of the caller's own tenant | Existence must not leak across tenants (owner row 76) |

## Global Constraints

- **Wire contract.** Every request and response body is a model in `agentic.orgapi`; request models use `extra="forbid"`. Paths are under `/v1`. Errors are `application/problem+json` with `type`, `title`, `status`, `detail`, a stable machine `code` (`auth.invalid_token`, `ingest.chain_broken`, ...) and an optional `hint` the CLI shows as is. An error never echoes a submitted secret, token or DSN, and never names another tenant.
- **Headers.** The CLI sends `Authorization: Bearer <token>`, `User-Agent: agentic-cli/<version>` and `X-Agentic-CLI-Version: <version>` on every call (M2-08 reads the last one). It never sends a tenant.
- **Tenancy (owner row 76):**
  - `tenants(id uuid, slug, name, status, policy_repo_url, policy_branch, webhook_token_sha256, gateway_tenant, rbac_revision, created_at)` is the only table without `tenant_id`. Every other table in schema `agentic` is tenant-owned and listed in `tables.TENANT_TABLES`; a catalogue test fails on any table that is not.
  - A tenant-owned table has `tenant_id uuid NOT NULL REFERENCES tenants(id)` as the first column of its primary key and of every unique constraint; foreign keys between tenant-owned tables are composite and include `tenant_id`, so a row can never reference another tenant's row, not even when written by the owner role.
  - Each tenant-owned table is created through `tenancy.rls.enable_tenant_rls(op, table)`: `ENABLE` and `FORCE ROW LEVEL SECURITY` and one policy `tenant_isolation ... USING (p) WITH CHECK (p)` where `p = tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid`. A missing setting makes `p` NULL: no rows, no inserts.
  - The runtime role owns nothing, is `NOSUPERUSER NOBYPASSRLS NOCREATEROLE NOINHERIT`, belongs to no role, and on `tenants` may `SELECT` only its own row and `UPDATE` only `rbac_revision` (column grant plus RLS policy on `id`).
  - The tenant of a transaction comes from `tenancy.context.tenant_ctx` (a `ContextVar`) through a `begin` event on the engine that runs `select set_config('app.tenant_id', $1, true)`; it is transaction-local, so a pooled connection never carries it into the next transaction.
  - The tenant is resolved only from a credential (control-plane token, invite token, webhook token) through the registered definer functions (`DEFINER_FUNCTIONS`); never from a header, path, query or body.
  - Another tenant's object answers 404 with the same problem body as a missing one (DR-5); lists never include it.
  - No raw SQL outside `db.py`, `db_roles.py`, `tenancy/` and `migrations/` (a test scans the AST for `text(` and `exec_driver_sql`): with the tenant set by a GUC, injected SQL could call `set_config`, so string-built SQL is the boundary to keep closed.
  - Advisory-lock keys include the tenant id; logs carry the tenant slug; work folders are per tenant (`policy_workdir/<tenant_id>`).
  - Per-tenant, never shared: audit chain, policy source and versions, roles and assignments (casbin domain), users, handles and CODEOWNERS maintainers, runs, rate limits (through the `RateLimiter` hook).
  - There is no cross-tenant API. The platform operator acts through `python -m agentic_cp admin ...` only; each action runs inside the affected tenant's context and is audited there with actor `system:operator`.
- **Tokens.** Only `sha256(token)` is stored; a token value appears once, in the issuing response, and never in logs, audit records, problems or `repr`s. Each token belongs to one tenant and one user of that tenant. Unknown, malformed, revoked and suspended-tenant tokens get the same 401 detail (no oracle); an expired token says so and hints `agentic login --for control-plane`. User tokens last 90 days; service-account tokens at most 365 (owner row 75).
- **RBAC (owner rows 74, 77).** Permission decisions go through `agentic_cp.rbac.authz.authorize(ctx, permission, resource)` or `require(...)` only; casbin is imported nowhere else (import contract). The enforcer is per tenant, built from the tenant's tables, rebuilt when `rbac_revision` changes and never mutated. Every RBAC change bumps `rbac_revision` and writes its audit record in the same transaction. An org admin satisfies any gate role when they hold `gate.approve`; four-eyes still applies; the approval records the role actually held.
- **Audit records** carry the spec §14.5 fields: `seq`, `tenant` (the tenant id as text), `team`, `actor`, `actor_kind` (`human|service|system`), `event`, `target`, `before`, `after`, `reason`, `run_id`, `trace_id`, `policy_version`, `at`, `prev`, `hash`. `hash = sdlc_commons.ledger.entry_hash(record minus hash)`, the master plan's ledger formula. `at` is stored as the exact ISO-8601 UTC text that was hashed (milliseconds, `Z`). `before`/`after` hold JSON without floats (money as decimal strings), so a jsonb round trip re-hashes identically. One chain per tenant, verified on its own; appends serialise on `pg_advisory_xact_lock(hashtextextended('agentic.audit:' || tenant_id, 0))` and `PRIMARY KEY (tenant_id, seq)` backs it up.
- **Evidence tables are insert-only for the runtime role:** `audit_log`, `run_events`, `policy_versions` (the active pointer lives in the updatable `policy_active`). `migrate` re-applies the grants and RLS checks after every upgrade.
- **Policy versions** are `git:<40-hex commit>`, the pattern the run ledger accepts (`sdlc_commons.ledger.FIELDS["policy_version"]`). git runs with `-c core.autocrlf=false -c core.eol=lf -c core.fsmonitor=false`, `GIT_TERMINAL_PROMPT=0`, the tenant's `GIT_CONFIG_GLOBAL` (ND-15), and its error text goes through `agentic.hosting.base.scrub_url_credentials` before it is logged or returned.
- **Bundles** are `git archive --format=tar <commit>` gzipped with `mtime=0`; `digest = "sha256:" + sha256(bundle)`. `tree_digest` is `agentic.policy.tree_digest` over the extracted files: length-prefixed path and bytes, sorted by POSIX path (the DF-140 lesson). The CLI extracts with `tarfile` `filter="data"`.
- **Import contracts (D43), added to `pyproject.toml`:**
  - "The CLI never imports the control plane": `agentic_cli` must not import `agentic_cp`.
  - "Server libraries only in the control plane": `agentic`, `sdlc_commons` and `agentic_cli` must not import `fastapi`, `starlette`, `uvicorn`, `sqlalchemy`, `alembic`, `asyncpg`, `casbin` or `codeowners`.
  - "casbin only behind authorize": `agentic_cp` must not import `casbin`, with `ignore_imports = ["agentic_cp.rbac.authz -> casbin"]`.
  - The existing rules still apply: Zyroflo only through `sdlc_commons`; provider SDKs only in `agentic.transport`; core imports nothing above it.
- **Provider-agnostic.** No model or vendor names in `agentic_cp` or `agentic.orgapi`. Git hosting is reached through git only; host names appear only as data (handles, remotes).
- **Windows and macOS.** Everything except the Postgres container runs natively on both. Postgres runs in a container through `CONTAINER_ENGINE` (docker or podman). Without an engine and without `AGENTIC_CP_TEST_DSN`, database tests skip with a reason that names both; with `AGENTIC_CP_REQUIRE_PG=1` (set by `check.py --linux` and CI) a missing database fails instead.
- **Pins.** Exact `==` pins for the new direct dependencies, and for transitive ones whose newest release is too young (Starlette, wcmatch, bracex); after `uv lock`, check that every newly added package's `upload-time` in `uv.lock` is at least two weeks old on the day of the lock (2026-10-08 for the 2026-10-22 additions; DF-95 still has no automatic check).
- **Formatting, CI and records:**
  - never add `# fmt: skip` or `# fmt: off`; line length 100;
  - check pytest's exit code directly;
  - `uv run python scripts/check.py --all` must be green on Windows and Linux;
  - TRACKER.md, DEFERRED.md and the decisions log (DR-1..DR-5, ND-14..17) are updated by the coordinator from this track's output.

## Review Focus

1. **Ingest takes a chain exactly as written, or nothing.** An edited, inserted, removed or reordered entry, a fork (same `seq`, different `hash`), a gap, an entry claiming another run, or an identity that changes mid-run is refused, and no row of that request is stored. A replay is a no-op; an overlapping replay stores only the new tail. Tests: Task 9 `test_broken_chain_rejected_and_nothing_stored`, `test_fork_is_rejected`, `test_gap_is_409_with_head`, `test_duplicate_replay_is_a_noop`, `test_overlapping_replay_accepts_only_new`, `test_concurrent_batches_for_one_run_serialise`.
2. **The running service cannot rewrite evidence.** The runtime role gets an insufficient-privilege error on `UPDATE`, `DELETE` and `TRUNCATE` of `audit_log`, `run_events` and `policy_versions`, and the service fails closed when that is not true. Each tenant's audit chain stays one contiguous chain under concurrent writers and verifies independently of the others. Tests: Task 4 `test_runtime_role_cannot_update_delete_or_truncate_evidence`, `test_readyz_fails_closed_when_audit_unenforced`, `test_concurrent_appends_keep_one_chain`, `test_verify_reports_an_edited_record_by_seq`, `test_audit_chains_verify_independently_per_tenant`.
3. **No invalid policy ever becomes active, and the version is the same everywhere.** A broken policy commit is rejected with its problems, the tenant's previous version stays active, and the version id the CLI records equals the commit the control plane validated, on Windows and Linux alike. Tests: Task 7 `test_invalid_policy_rejected_and_previous_kept`, `test_bundle_digest_matches_and_is_deterministic`, `test_each_tenant_syncs_its_own_repo`; Task 10 `test_workspace_policy_uses_recorded_version`, `test_locally_edited_cached_policy_is_refused`.
4. **RBAC matches spec §23 cell by cell.** A team lead cannot reach another team or assign beyond Developer/Approver. The last org admin cannot be removed or stripped of `rbac.assign`. A service account cannot approve. An org admin approves a gate only through `gate.approve`, and the role held is recorded. Self-asserted roles in `~/.agentic/config.yaml` never authorise anything in org mode. Tests: Task 6 `test_permission_matrix_cell[...]`, `test_team_lead_assigns_only_developer_or_approver_in_own_team`, `test_last_org_admin_cannot_be_removed`, `test_role_held_names_the_role_actually_held`; Task 10 `test_org_init_takes_roles_from_control_plane`.
5. **Secrets stay secret.** Tokens (control-plane, webhook) are never stored or logged in clear. Git credentials never sit in a stored URL and never appear in a problem, a log line or an audit record. The DSN never appears in `repr(Settings)`. Tests: Task 5 `test_token_is_stored_hashed_and_never_logged`; Task 7 `test_policy_source_refuses_credentials_in_url`; Task 8 `test_git_errors_never_show_credentials`; Task 1 `test_settings_repr_hides_secrets`.
6. **Tenant isolation holds in the database, and nothing bypasses it.** Look for: a tenant-owned table without `FORCE` RLS or without the policy; a runtime role that owns a table, has `BYPASSRLS` or inherits a role that does; a `SECURITY DEFINER` function that is not registered, lacks a pinned `search_path`, is executable by `PUBLIC`, or returns more than the tenant id for an exact hash; a tenant set at session level (`set_config(..., false)`) or left on a pooled connection; a code path that runs with no tenant and expects rows; a tenant taken from a header, path, query or body; string-built SQL; a composite key or foreign key without `tenant_id`; advisory locks or work folders shared across tenants; a 403 (instead of 404) or a different problem body for another tenant's object; owner-role admin commands that touch a tenant without setting its context. Tests: Task 3 `test_tenant_isolation[<table>-<operation>]` (generated for every table in `TENANT_TABLES`), `test_missing_tenant_context_returns_no_rows`, `test_tenant_context_is_transaction_local`, `test_runtime_role_is_not_owner_and_cannot_bypass_rls`, `test_definer_functions_are_registered_and_pinned`, `test_no_raw_sql_outside_db_layer`; Task 5 `test_tenant_comes_only_from_the_token`; `test_api_isolation[<family>-<route>]` (Tasks 5-9) and `test_every_object_route_has_an_isolation_case`.
7. **Casbin's view never drifts from the tables, and casbin stays behind `authorize`.** Look for: an RBAC write path that does not bump `rbac_revision` in the same transaction; an enforcer mutated in place; a cache keyed without the revision; a replica that keeps an old decision after a change; a domain string built without the tenant; the seed policy diverging from the §23 matrix; `model.conf` edited without a test change; casbin imported outside `authz.py`. Tests: Task 6 `test_enforcer_built_from_tables_equals_seed`, `test_role_change_visible_on_next_request_across_replicas`, `test_failed_change_does_not_bump_revision`, `test_enforcer_never_matches_another_tenants_domain`, `test_model_conf_snapshot`; `uv run lint-imports`.

## Deferred items pulled in

| Item | Handling here |
|---|---|
| DF-182 (verify the gateway key at login, register with the control plane, check the minimum CLI version) | Control-plane half done in Task 10 (`login --for control-plane` verifies through `/v1/me` before storing). Minimum CLI version is M2-08 (slice 2). Gateway-key verification needs a TokenLean key introspection call: retarget to M2-17 |
| DF-184 (`agent.edit.repo` for `init --repo` in org mode) | Task 10: check 7 asks the control plane |
| DF-203 (roles from `~/.agentic/config.yaml` are self-asserted) | Task 10: org mode with a control plane takes roles from `/v1/me`; the config's `roles` is ignored there (warned once). Standalone keeps it, documented as "chooses skills only, never authorises" |
| DF-288 (git host settings in org policy) | Not in this slice: it is an `OrgPolicy` field plus CLI hosting wiring. Retarget to M2-51 (policy-repo template), which the bundle from M2-02 then distributes |
| DF-291 (control-plane run URL shape) | Task 9 adds `GET /v1/runs/{run_id}`; Task 10 changes the PR links per ND-11 |
| DF-02 (policy_version CRLF/LF hash) | Org mode no longer depends on it once the CLI records the control plane's `git:` version (Task 10); stays open at M1-81 for standalone |
| DF-95 (two-week rule for transitive packages) | Instances fixed: Starlette pinned to 1.7.0 (Task 1); wcmatch 11.0.1 and bracex 3.0.1 pinned (Task 6). The general check stays M1-81 |

New DF candidates this plan creates (coordinator assigns numbers): per-workspace control-plane tokens for people in two tenants (ND-17); tenant suspension and deletion commands (only `status` exists; no command sets it yet); parallel per-tenant policy sync when tenants number in the hundreds (the worker loops sequentially with a per-tenant git timeout); recording `role_held` in the gate ledger event (M2-21, see the hand-off table).

---

## Slice 1a: tenancy, auth, RBAC, audit (Tasks 1-6)

### Task 1: Package skeleton, settings, app factory, problem+json, OpenAPI snapshot (M2-01)

**Files:**
- Modify: `control-plane/pyproject.toml` (dependencies: `agentic-core`, `sdlc-commons`, `fastapi==0.142.4`, `starlette==1.7.0`, `uvicorn==0.54.0`, `sqlalchemy[asyncio]==2.0.54`, `asyncpg==0.31.0`, `alembic==1.20.0`, `codeowners==0.9.0`; `[tool.uv.build-backend]` includes `migrations/`, `alembic.ini` and `rbac/model.conf` as package data); root `pyproject.toml` (the import contracts above, except the casbin one, which Task 6 adds with the dependency); `uv lock`.
- Create: `control-plane/src/agentic_cp/{settings.py, app.py, errors.py, __main__.py}`, `core/src/agentic/orgapi/{__init__.py, models.py}`, `control-plane/openapi.json` (generated).
- Test: `control-plane/tests/test_settings.py`, `control-plane/tests/test_app.py`, `control-plane/tests/conftest.py`.

**Interfaces:**
- `agentic.orgapi`: `API_PREFIX = "/v1"`, `TOKEN_PREFIX = "acp_"`, `WEBHOOK_TOKEN_PREFIX = "acw_"`, `CLI_VERSION_HEADER = "X-Agentic-CLI-Version"`, `class Problem(BaseModel)` (`type`, `title`, `status`, `detail`, `code`, `hint: str | None`); a `RequestModel` base with `extra="forbid"`.
- `class SettingsError(Exception)`.
- `Settings` (frozen pydantic model, deployment-wide only; per-tenant values live in `tenants`): `database_url: SecretStr`, `owner_database_url: SecretStr | None`, `db_schema = "agentic"`, `policy_poll_seconds = 60` (at least 10), `policy_workdir: Path`, `git_config_dir: Path | None` (ND-15), `require_db_enforcement = True` (DR-4), `max_ingest_entries = 500` (1..5000), `max_body_bytes = 1_048_576`, `user_token_days = 90`, `service_token_max_days = 365`, `enforcer_cache_tenants = 256`.
- `Settings.from_env(env: Mapping[str, str]) -> Settings`: reads `AGENTIC_CP_<FIELD>`; an unknown `AGENTIC_CP_*` name raises `SettingsError` naming it.
- `create_app(settings, *, engine=None, clock=None, git=None, rate_limiter=None) -> FastAPI`: lifespan opens and closes the engine; installs the problem handlers (HTTPException, RequestValidationError, unhandled exceptions as a 500 with `code="internal"` and no traceback in the body); adds `GET /healthz` (no database access).
- `python -m agentic_cp openapi [--write | --check]` writes or compares `control-plane/openapi.json` (sorted keys, LF).

- [ ] **Step 1: Write the failing tests**:
  - `test_settings_from_env_reads_prefixed_variables`: `Settings.from_env({... "AGENTIC_CP_POLICY_POLL_SECONDS": "30"}).policy_poll_seconds == 30`.
  - `test_settings_refuse_unknown_prefixed_variable`: `AGENTIC_CP_POLICY_POL_SECONDS` raises `SettingsError` whose message contains that name; so does the retired `AGENTIC_CP_TENANT` (the hint says tenants are created with `admin tenant create`).
  - `test_settings_refuse_bad_values`: poll seconds `5` and `max_ingest_entries` `0` are each refused, naming the field.
  - `test_settings_repr_hides_secrets` (RF5): a DSN password is not in `repr(settings)` or `str(settings)`.
  - `test_healthz_is_ok_without_database`: an app whose engine would fail on connect still answers `GET /healthz` with 200 `{"status": "ok"}`.
  - `test_unknown_route_is_problem_json`: 404, content type `application/problem+json`, `code == "not_found"`.
  - `test_validation_error_is_problem_json_with_field_names`: 422, `detail` names the field, `code == "invalid_request"`; an extra body field is refused the same way.
  - `test_unhandled_error_is_500_without_traceback`: the body has `code == "internal"` and no `Traceback`.
  - `test_openapi_snapshot_is_current`: `app.openapi()` equals the committed `openapi.json`; the failure message says `python -m agentic_cp openapi --write`.
- [ ] **Step 2: Run** `uv run pytest control-plane/tests/test_settings.py control-plane/tests/test_app.py -q`. Expected: FAIL (no module).
- [ ] **Step 3: Implement.** Pin the dependencies, run `uv lock`, check the new `upload-time` values, run `uv run lint-imports` and `uv run python scripts/licence_check.py`.
- [ ] **Step 4: Run.** Expected: PASS; lint-imports and the licence check green.
- [ ] **Step 5: Commit.** `feat(control-plane): skeleton - settings, app factory, problem+json, OpenAPI snapshot (M2-01)`

### Task 2: Database, migrations, tenants table, test Postgres (M2-01)

**Files:**
- Create: `control-plane/src/agentic_cp/{db.py, tables.py}`, `control-plane/src/agentic_cp/migrations/{env.py, script.py.mako, versions/0001_schema_tenants.py}`, `control-plane/src/agentic_cp/alembic.ini`, `control-plane/tests/pg.py` (fixture helpers).
- Modify: `scripts/check.py` (the `--linux` stage starts a Postgres sidecar), `control-plane/tests/conftest.py`.
- Test: `control-plane/tests/test_migrations.py`, `control-plane/tests/test_pg_fixture.py`, `scripts/tests/test_check_postgres.py`.

**Interfaces:**
- `tables.metadata` (schema from settings); `0001` creates the schema plus `tenants(id uuid pk, slug unique (lower-case slug), name, status active|suspended, policy_repo_url text null, policy_branch text default 'main', webhook_token_sha256 bytea unique null, gateway_tenant text null, rbac_revision bigint default 0, created_at)`.
- `TENANT_TABLES: tuple[str, ...] = ()` in `tables.py` (filled by later tasks).
- `migrate(owner_dsn, schema) -> str` runs `alembic upgrade head` programmatically with an async env and returns the head revision. `python -m agentic_cp migrate` uses `AGENTIC_CP_OWNER_DATABASE_URL`.
- `make_engine(settings) -> AsyncEngine` (asyncpg; `search_path` set to the schema; pool pre-ping). Task 3 adds the tenant `begin` event to it.
- Test fixtures (session scope):
  - `pg_server`: `AGENTIC_CP_TEST_DSN`, else `<engine> run -d --rm -p 127.0.0.1::5432 -e POSTGRES_PASSWORD=... postgres:17@sha256:<digest>`, waiting for `pg_isready` inside the container;
  - `pg_template` migrates a template database once and creates the roles `cp_owner` and `cp_app`;
  - function scope: `db` is `CREATE DATABASE t_<uuid> TEMPLATE ...` (about 100 ms) with an owner engine and an app engine; `two_tenants` creates tenants `a` and `b` through the owner role and yields their ids.
- `check.py --linux`:
  - `<engine> network create agentic-check-<pid>`, then start the pinned `postgres:17` on it;
  - run the check container on the same network with `AGENTIC_CP_TEST_DSN=postgresql://...@pg:5432/postgres` and `AGENTIC_CP_REQUIRE_PG=1`;
  - remove both in a `finally`.

- [ ] **Step 1: Failing tests**:
  - `test_upgrade_head_on_empty_database`: `migrate()` returns the head; `tenants` exists in schema `agentic`.
  - `test_migrate_is_idempotent`: a second `migrate()` changes nothing.
  - `test_migrations_match_metadata`: Alembic `compare_metadata(...) == []`.
  - `test_downgrade_to_base_and_upgrade_again`: development safety; succeeds.
  - `test_missing_database_skips_with_reason`: with no engine and no DSN (monkeypatched `which`), the skip reason names `CONTAINER_ENGINE` and `AGENTIC_CP_TEST_DSN`.
  - `test_missing_database_fails_when_required`: with `AGENTIC_CP_REQUIRE_PG=1`, the same case fails instead.
  - `test_linux_stage_starts_postgres_sidecar_on_a_private_network`: a fake runner shows network create, postgres run with `--network`, the check container with both variables, then cleanup even when the checks fail.
- [ ] **Step 2: Run** `uv run pytest control-plane/tests/test_migrations.py control-plane/tests/test_pg_fixture.py scripts/tests/test_check_postgres.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement.** Pin the image by digest (a `postgres:17.x` release at least two weeks old) in one constant shared by `pg.py` and `check.py`.
- [ ] **Step 4: Run.** Expected: PASS, natively with Rancher Desktop or Podman, and in `check.py --linux`.
- [ ] **Step 5: Commit.** `feat(control-plane): Postgres schema, tenants table, Alembic migrations, test database fixture (M2-01)`

### Task 3: Tenancy foundation: RLS, tenant context, runtime role, isolation harness (M2-01, owner row 76)

Every later table depends on this task: it fixes how a table becomes tenant-owned, how the tenant reaches the database, and how isolation is proven, so Tasks 4-9 only register their tables and object routes.

**Files:**
- Create: `control-plane/src/agentic_cp/tenancy/{__init__.py, context.py, rls.py, tenants.py, ratelimit.py}`, `control-plane/src/agentic_cp/db_roles.py`, `migrations/versions/0002_tenancy.py`.
- Modify: `control-plane/src/agentic_cp/db.py` (installs the `begin` event), `app.py` (`GET /readyz`), `__main__.py` (`db grant-runtime`).
- Test: `control-plane/tests/isolation.py` (harness and registries), `control-plane/tests/test_tenancy.py`, `control-plane/tests/test_tenant_isolation.py`, `control-plane/tests/test_db_roles.py`, `control-plane/tests/test_no_raw_sql.py`.

**Interfaces:**
- `tenancy.context`:
  - `tenant_ctx: ContextVar[UUID | None]` (default None);
  - `install_tenant_guc(engine)`: `event.listen(engine.sync_engine, "begin", ...)`; when `tenant_ctx` is set, runs `select set_config('app.tenant_id', $1, true)`; otherwise sets nothing;
  - `@asynccontextmanager tenant_tx(engine, tenant_id) -> AsyncConnection`: sets `tenant_ctx`, begins, reads back `current_setting('app.tenant_id')` and raises `TenancyError` if it differs, resets the context variable on exit;
  - `class TenancyError(Exception)`.
- `tenancy.rls`:
  - `TENANT_PREDICATE = "tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid"`;
  - `enable_tenant_rls(op, table)`: `ENABLE` + `FORCE ROW LEVEL SECURITY`, `CREATE POLICY tenant_isolation ... USING (...) WITH CHECK (...)`;
  - `0002` puts an `own_tenant` policy on `tenants` (`id = NULLIF(...)::uuid`; `ENABLE` without `FORCE`, so the owner role can create tenants).
- `tenancy.tenants`: `TenantRow` (frozen dataclass of the `tenants` columns), `async create_tenant(owner_conn, slug, name, *, policy_repo=None, policy_branch="main", gateway_tenant=None) -> TenantRow` (refuses a used slug), `async current_tenant(conn) -> TenantRow`, `async bump_rbac_revision(conn) -> int`.
- Definer functions (`DEFINER_FUNCTIONS` registry in `db_roles.py`; each `SECURITY DEFINER`, owned by the owner role, `SET search_path = pg_catalog, agentic`, `REVOKE EXECUTE ... FROM PUBLIC`, granted to the runtime role):
  - `agentic.active_tenant_ids() RETURNS SETOF uuid` (status `active` only; used by the worker); Task 5 adds `resolve_token`, Task 7 `resolve_webhook`, M2-18 `resolve_invite`.
- `db_roles`:
  - `async grant_runtime(owner_conn, role, password, schema)`, idempotent: `LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEROLE NOINHERIT`, member of no role, owns nothing; full DML on the tenant tables; `SELECT` and `UPDATE (rbac_revision)` on `tenants`; `EXECUTE` on `DEFINER_FUNCTIONS` only; default privileges for later tables; the password sent as a SCRAM verifier, never in clear. Task 4 narrows the evidence tables to `SELECT, INSERT`.
  - `async enforcement_status(conn) -> EnforcementStatus(enforced: bool, reasons: list[str], codes: set[str])`, fail closed (a check that is missing or NULL counts against). Tenancy reasons (code `tenancy.unenforced`): the runtime role is superuser, has `BYPASSRLS`, owns any table in the schema, or is a member of a role; a table in `TENANT_TABLES` lacks RLS, `FORCE` or the `tenant_isolation` policy; a table in the schema is neither `tenants` nor in `TENANT_TABLES`; a `SECURITY DEFINER` function in the schema is not in `DEFINER_FUNCTIONS`, lacks the pinned `search_path`, or is executable by `PUBLIC`.
- `GET /readyz`: database reachable, migrations at head, `enforcement_status` (ND-5). 200 `{"status": "ready", "db_enforced": true}`, else 503 problem `tenancy.unenforced` / `audit.unenforced` (Task 4) / `db.unavailable` / `db.migrations_pending`.
- `tenancy.ratelimit`: `class RateLimiter(Protocol)`: `async check(tenant_id: UUID, principal_id: UUID, route: str) -> RateDecision(allowed: bool, retry_after: int | None)`; `NoLimit` default. Task 5 calls it after authentication; a refusal is 429 `rate.limited` with `Retry-After`. Per-tenant limits are configured by whichever limiter a deployment injects (slice 2 or later).
- Test harness (`control-plane/tests/isolation.py`):
  - `ROW_FACTORIES: dict[str, Callable[[UUID], dict]]`: one row factory per tenant-owned table, registered by the task that creates the table;
  - `OPERATIONS = ("select_other", "select_crafted_where", "select_no_context", "insert_other_tenant_id", "insert_no_context", "update_other", "delete_other")`;
  - `ENDPOINT_CASES: list[EndpointCase(family, method, path, make_object_in_tenant_b)]`, registered by Tasks 5-9 for every `/v1` route with a path parameter, plus list routes with a `must_not_list` check.

- [ ] **Step 1: Failing tests**:
  - `test_missing_tenant_context_returns_no_rows` (RF6): on a probe table created by the fixture through `enable_tenant_rls`, rows of tenants a and b exist; the app role with no tenant set sees 0 rows.
  - `test_missing_tenant_context_refuses_inserts`: insert with no tenant set raises the RLS `WITH CHECK` violation.
  - `test_tenant_context_is_transaction_local` (RF6): after a committed `tenant_tx(a)`, the next transaction on the same pooled connection (pool size 1) has `current_setting('app.tenant_id', true)` empty and sees no rows.
  - `test_tenant_context_does_not_leak_between_concurrent_tasks`: two tasks in `tenant_tx(a)` and `tenant_tx(b)` interleaved 50 times each see only their own rows.
  - `test_tenant_tx_refuses_a_mismatched_setting`: a session-level `set_config(..., false)` to b inside `tenant_tx(a)` is detected and raises `TenancyError`.
  - `test_tenant_isolation[<table>-<operation>]` (RF6): generated over `TENANT_TABLES x OPERATIONS` (here only the probe table; later tasks add theirs). Under `tenant_tx(a)` as the app role: reading b's rows, with or without `WHERE tenant_id = :b`, returns nothing; inserting with `tenant_id = b` raises the RLS violation; `UPDATE`/`DELETE ... WHERE tenant_id = :b` affects 0 rows (for insert-only tables: insufficient privilege); with no tenant, reads return nothing and inserts are refused.
  - `test_every_tenant_table_has_an_isolation_factory`: `set(TENANT_TABLES) == set(ROW_FACTORIES)` (minus the probe), so a new table without isolation tests fails.
  - `test_every_table_is_tenant_owned_and_forced` (catalogue): every table in schema `agentic` except `tenants` and `alembic_version` is in `TENANT_TABLES`, has `tenant_id` first in its primary key, `relrowsecurity` and `relforcerowsecurity` true and the `tenant_isolation` policy; every unique constraint and foreign key among them includes `tenant_id`.
  - `test_runtime_role_is_not_owner_and_cannot_bypass_rls` (RF6): `rolbypassrls`, `rolsuper`, `rolinherit` false; no `pg_auth_members` rows; owns no relation.
  - `test_runtime_sees_only_its_own_tenant_row`: `SELECT * FROM tenants` under `tenant_tx(a)` returns only a; `UPDATE tenants SET name = ...` is refused (column privilege); `bump_rbac_revision` works.
  - `test_definer_functions_are_registered_and_pinned` (RF6): every `prosecdef` function in the schema is in `DEFINER_FUNCTIONS`, has `proconfig` containing the pinned `search_path`, and `PUBLIC` lacks `EXECUTE`.
  - `test_active_tenant_ids_returns_ids_only`: the app role gets both ids from the function and still cannot read b's `tenants` row.
  - `test_enforcement_status_reports_rls_gaps`: `ALTER TABLE probe NO FORCE ROW LEVEL SECURITY`, `ALTER ROLE cp_app BYPASSRLS` and an unregistered definer function each give `enforced is False` with a reason naming it.
  - `test_readyz_fails_closed_when_tenancy_unenforced`: 503 `tenancy.unenforced` with `require_db_enforcement=True`; 200 with `db_enforced: false` when it is `False`.
  - `test_grant_runtime_is_idempotent_and_never_logs_password`: run twice; the statement log (caplog) holds no password.
  - `test_no_raw_sql_outside_db_layer` (RF6): an AST scan of `agentic_cp` finds `sqlalchemy.text(` and `exec_driver_sql` only in `db.py`, `db_roles.py`, `tenancy/` and `migrations/`.
- [ ] **Step 2: Run** `uv run pytest control-plane/tests/test_tenancy.py control-plane/tests/test_tenant_isolation.py control-plane/tests/test_db_roles.py control-plane/tests/test_no_raw_sql.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement.** The `db_roles` docstring credits the TokenLean enforcement pattern.
- [ ] **Step 4: Run.** Expected: PASS.
- [ ] **Step 5: Commit.** `feat(control-plane): tenancy foundation - RLS, per-transaction tenant, runtime role, isolation harness (M2-01)`

### Task 4: Hash-chained audit table per tenant, insert-only evidence (M2-05)

**Files:**
- Create: `control-plane/src/agentic_cp/audit/{__init__.py, chain.py}`, `migrations/versions/0003_audit_log.py`.
- Modify: `commons/src/sdlc_commons/ledger.py` (add `entry_hash`), `control-plane/src/agentic_cp/db_roles.py` (`INSERT_ONLY`, audit reasons), `tables.py` (`TENANT_TABLES += ("audit_log",)`), `control-plane/tests/isolation.py` (factory).
- Test: `control-plane/tests/test_audit_chain.py`, `control-plane/tests/test_audit_enforcement.py`, `commons/tests/test_ledger_entry_hash.py`.

**Interfaces:**
- `sdlc_commons.ledger.entry_hash(entry: Mapping[str, object]) -> str`: returns Zyroflo's `approve.entry_hash`.
- `audit_log(tenant_id uuid, seq bigint, at text, actor, actor_kind, event, team, target, before jsonb, after jsonb, reason, run_id, trace_id, policy_version, prev, hash; pk (tenant_id, seq))`, tenant-owned with RLS.
- `class AuditError(Exception)`.
- `@dataclass(frozen=True) AuditEvent(event, actor, actor_kind, target="", team=None, before=None, after=None, reason="", run_id=None, trace_id=None, policy_version=None)`; its validator refuses floats anywhere in `before`/`after`.
- `async append(conn, event: AuditEvent, *, clock) -> AuditRecord`: the tenant comes from the transaction's context (`AuditError` without one); takes that tenant's advisory lock, reads the head, then inserts; the record's `tenant` field is the tenant id.
- `async verify(conn, *, from_seq=1) -> list[str]`: recomputes every hash and link of the current tenant's chain and names the `seq` of each problem. The operator command `python -m agentic_cp admin audit verify --tenant <slug>` runs it inside that tenant's context.
- `INSERT_ONLY = ("audit_log", "run_events", "policy_versions")`; `grant_runtime` restricts those that exist, so re-running it after Tasks 7 and 9 (as `migrate` does) covers the later tables. `enforcement_status` adds audit reasons (code `audit.unenforced`), e.g. "may change audit rows directly".

- [ ] **Step 1: Failing tests**:
  - `test_entry_hash_is_zyroflos`: equals the `hash` a `RunLedger` writes for the same entry.
  - `test_append_chains_records_per_tenant`: seqs 1..3 in tenant a, `prev` links, `hash == entry_hash(record minus hash)`; tenant b starts at 1.
  - `test_append_without_tenant_context_is_refused`: `AuditError`, nothing inserted.
  - `test_record_hash_matches_ledger_formula`: an independent `hashlib` recompute per the master plan formula agrees.
  - `test_record_values_refuse_floats`: `before={"x": 1.5}` raises `AuditError`.
  - `test_verify_reports_an_edited_record_by_seq` (RF2): an owner-role UPDATE of seq 2's `reason` in tenant a makes `verify` name seq 2.
  - `test_verify_reports_a_deleted_record`: deleting seq 2 makes `verify` name seq 3 (broken link and gap).
  - `test_audit_chains_verify_independently_per_tenant` (RF2, RF6): tampering with tenant b's seq 2 leaves `verify` under tenant a at `[]`, and `verify` under b names seq 2.
  - `test_concurrent_appends_keep_one_chain` (RF2): 2 tasks x 50 appends in one tenant give seqs 1..100 and `verify == []`; a third task appending to tenant b at the same time is not blocked by a's lock.
  - `test_runtime_role_cannot_update_delete_or_truncate_evidence` (RF2): for each existing table in `INSERT_ONLY` (here `audit_log`), each of UPDATE, DELETE and TRUNCATE as `cp_app` raises asyncpg `InsufficientPrivilegeError`. Parametrised over `INSERT_ONLY`, so it covers `policy_versions` and `run_events` once Tasks 7 and 9 create them.
  - `test_runtime_role_can_insert_and_select_audit`.
  - `test_enforcement_status_reports_each_blocker`: a role granted UPDATE on `audit_log` gets `enforced is False` with reason "may change audit rows directly".
  - `test_readyz_fails_closed_when_audit_unenforced`: an app on the owner DSN with `require_db_enforcement=True` answers 503 `audit.unenforced`; with `False` it answers 200 with `db_enforced: false`.
  - `test_tenant_isolation[audit_log-*]` runs from the Task 3 harness once the factory is registered.
- [ ] **Step 2: Run** `uv run pytest control-plane/tests/test_audit_chain.py control-plane/tests/test_audit_enforcement.py control-plane/tests/test_tenant_isolation.py commons/tests/test_ledger_entry_hash.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run.** Expected: PASS.
- [ ] **Step 5: Commit.** `feat(control-plane): hash-chained audit table per tenant, insert-only evidence (M2-05)`

### Task 5: Token authentication, users, tenant bootstrap, `/v1/me` (M2-01)

**Files:**
- Create: `control-plane/src/agentic_cp/auth/{__init__.py, tokens.py, deps.py, api.py, bootstrap.py}`, `control-plane/src/agentic_cp/rbac/defaults.py`, `migrations/versions/0004_users_tokens_rbac.py` (tables `users`, `tokens`, `roles`, `assignments`, all tenant-owned with RLS; function `resolve_token`; roles are seeded per tenant by `seed_roles`; Task 6 adds the API and the enforcer).
- Modify: `core/src/agentic/orgapi/models.py` (`Me`, `TenantRef`, `TokenIssued`, `TokenInfo`), `__main__.py` (`admin tenant create`, `admin tenant list`, `admin issue-token`), `app.py` (rate-limit dependency), `tables.py` and `control-plane/tests/isolation.py` (four tables, endpoint cases).
- Test: `control-plane/tests/test_auth.py`, `control-plane/tests/test_bootstrap.py`, `control-plane/tests/test_api_isolation.py`.

**Interfaces:**
- Tables:
  - `users(tenant_id, id uuid, email (stored lower-case, unique per tenant), display_name, kind human|service, handles text[], status active|disabled, created_at; pk (tenant_id, id))`;
  - `tokens(tenant_id, id uuid, user_id, name, token_sha256 bytea unique, created_at, created_by, expires_at, revoked_at, last_used_at; pk (tenant_id, id); fk (tenant_id, user_id) -> users)`; `token_sha256` is unique across the table because resolution happens before the tenant is known;
  - `roles` and `assignments` as in Task 6.
- `agentic.resolve_token(sha256 bytea) RETURNS TABLE(tenant_id, tenant_slug, tenant_status, rbac_revision, token_id, user_id, email, kind, user_status, expires_at, revoked_at)`: definer function (registered), exact match on the hash, at most one row.
- `new_token() -> tuple[str, str]`: the value (`acp_` + `secrets.token_urlsafe(32)`) and its sha256.
- `@dataclass(frozen=True) TokenContext(tenant_id, tenant_slug, user_id, email, kind, token_id, rbac_revision)`.
- `current_token` FastAPI dependency (HTTPBearer):
  - resolves through `resolve_token`, then sets `tenant_ctx` for the rest of the request, so every later transaction is in that tenant;
  - 401 problems `auth.missing_token` / `auth.invalid_token` / `auth.token_expired` with `WWW-Authenticate: Bearer`; disabled users and suspended tenants count as invalid;
  - calls the `RateLimiter` (429 `rate.limited`);
  - `last_used_at` is updated at most once a minute.
- `GET /v1/me -> Me`: user, kind, `tenant: TenantRef(slug, name)`, `token_expires_at`. Task 6 adds `assignments` and `permissions`.
- `POST /v1/tokens` (body: `name`, `revoke_current: bool = False`): issues a token for the caller in the caller's tenant (rotation); returns `TokenIssued` (the value, once).
- `GET /v1/tokens` lists the caller's tokens (no values). `DELETE /v1/tokens/{id}` revokes the caller's own token (other users' tokens: Task 6, `rbac.assign`).
- Operator commands (ND-4; owner DSN, each step inside the new tenant's context):
  - `python -m agentic_cp admin tenant create --slug S --name N --admin-email E [--policy-repo URL] [--policy-branch B] [--gateway-tenant T]`: creates the tenant, runs `seed_roles` for it, creates the admin user with the org-scope `org-admin` assignment, audits `tenant.created` then `rbac.bootstrap` (actor `system:operator`, seq 1 and 2 of the new chain) and prints the admin token once. A used slug exits 2. This is also the first-tenant bootstrap of a fresh deployment;
  - `python -m agentic_cp admin tenant list` (slug, name, status, policy repo without credentials);
  - `python -m agentic_cp admin issue-token --tenant S --email E` for admin recovery, audited `token.issued` with actor `system:operator`.
- Audit events: `token.issued`, `token.revoked` (never the value).
- Endpoint cases (Task 3 harness): `DELETE /v1/tokens/{id}` with tenant b's token id; `GET /v1/tokens` never lists b's tokens.

- [ ] **Step 1: Failing tests**:
  - `test_missing_token_is_401_problem`: `WWW-Authenticate: Bearer`, `code == "auth.missing_token"`.
  - `test_unknown_malformed_and_revoked_tokens_look_the_same`: identical status, code and detail; a token of a suspended tenant too.
  - `test_expired_token_is_rejected_with_login_hint`: clock past `expires_at` gives `code == "auth.token_expired"`, and `hint` contains `agentic login --for control-plane`.
  - `test_user_and_service_token_lifetimes`: a user token expires after `user_token_days` (90); a service token request above `service_token_max_days` (365) is refused (owner row 75).
  - `test_disabled_user_tokens_are_rejected`.
  - `test_me_returns_the_principal_and_tenant`.
  - `test_tenant_comes_only_from_the_token` (RF6): with tenant a's token, a request carrying `X-Tenant: b`, `?tenant=b` or a body field `tenant` still acts in a (the body field is a 422, extra fields being forbidden), and `/v1/me` names a.
  - `test_resolve_token_returns_at_most_one_row_and_nothing_else`: unknown hash gives no row; the function's result columns are exactly the listed ones.
  - `test_user_can_rotate_own_token`: the new token works; with `revoke_current`, the old one gets 401.
  - `test_token_is_stored_hashed_and_never_logged` (RF5): no table column and no caplog record contains the value.
  - `test_token_events_are_audited`: `token.issued` and `token.revoked` records exist in the caller's tenant chain; neither contains the value.
  - `test_rate_limiter_refusal_is_429_for_that_tenant_only`: a limiter refusing tenant a gives a 429 `rate.limited` with `Retry-After`; tenant b is served.
  - `test_tenant_create_bootstraps_tenant_and_first_admin`: the token is printed once and works; `/v1/me` shows the org-admin assignment; the tenant's chain starts with `tenant.created`, `rbac.bootstrap`.
  - `test_tenant_create_refuses_a_used_slug`: exit 2; nothing is created.
  - `test_issue_token_is_scoped_to_the_named_tenant`: the same email in tenants a and b gets a token for the named tenant only.
  - `test_tenant_isolation[users-*]`, `[tokens-*]`, `[roles-*]`, `[assignments-*]` and `test_api_isolation[tokens-*]` (harness): b's token id answers 404 exactly like a random id.
- [ ] **Step 2: Run** `uv run pytest control-plane/tests/test_auth.py control-plane/tests/test_bootstrap.py control-plane/tests/test_tenant_isolation.py control-plane/tests/test_api_isolation.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run.** Expected: PASS.
- [ ] **Step 5: Commit.** `feat(control-plane): tenant-bound token auth, users, tenant and first-admin bootstrap, /v1/me (M2-01)`

### Task 6: RBAC through pycasbin: catalogue, default roles, assignments, `authorize`, check API (M2-03)

**Files:**
- Modify: `control-plane/pyproject.toml` (`pycasbin==2.8.0`, `simpleeval==1.0.8`, `wcmatch==11.0.1`, `bracex==3.0.1`); root `pyproject.toml` (the "casbin only behind authorize" contract; `casbin` added to the server-libraries list); `uv lock`; `control-plane/src/agentic_cp/rbac/defaults.py` (role contents final, per the §23 matrix).
- Create: `core/src/agentic/orgapi/rbac.py`, `control-plane/src/agentic_cp/rbac/{__init__.py, model.conf, authz.py, store.py, api.py}`, `control-plane/src/agentic_cp/audit/api.py`.
- Test: `core/tests/orgapi/test_rbac.py`, `control-plane/tests/rbac/{matrix.py, test_matrix.py, test_authz.py}`, `control-plane/tests/test_rbac_api.py`, `control-plane/tests/test_audit_api.py`.

**Interfaces:**
- **Core `agentic.orgapi.rbac`** (pure; no casbin, no rule engine):
  - `PERMISSIONS: dict[str, Literal["org", "team", "repo"]]`: the spec §12.3 set plus `agent.edit.repo` (CLI check 7), `rbac.assign.team`, `dashboard.view.repo`, `audit.read.own`, `audit.read.repo`. The value is where a grant counts: `org` only from an org-scope assignment; `team` from org scope or that team; `repo` from org scope, the repo's team, or a derived maintainership of that repo.
  - `ScopeRef(kind: "org"|"team"|"repo", value: str | None)`.
  - `EffectivePermissions(org: frozenset[str], teams: dict[str, frozenset[str]], repos: dict[str, frozenset[str]], repo_teams: dict[str, str])`: computed by the server from casbin; `allows(perms, permission, *, team=None, repo=None) -> bool` reads those sets for display and CLI hints only (authoritative answers come from `/v1/rbac/check`).
  - `holds_role(assignments, role, *, team=None) -> bool` and `role_held(assignments, role, *, team=None) -> str | None`: the named role in the team or at org scope, else `org-admin` when the user holds `gate.approve` (ND-8, owner row 74), else None.
  - `class OrgAuthorizer(me: Me, team: str)`: implements the engine's `Authorizer.holds(user, role)` (True only for the logged-in user) plus `role_held(user, role)`, which M2-21 records in the gate event. Four-eyes stays in the engine (`engine.decide`), unchanged.
- **`agentic_cp.rbac.authz`** (the only module that imports casbin):
  - `model.conf` (RBAC with domains, from casbin's `rbac_with_domains_model.conf`, with the scope inside the domain and roles defined tenant-wide):

    ```ini
    [request_definition]
    r = sub, dom, act
    [policy_definition]
    p = sub, act
    [role_definition]
    g = _, _, _
    [policy_effect]
    e = some(where (p.eft == allow))
    [matchers]
    m = g(r.sub, p.sub, r.dom) && r.act == p.act
    ```
  - policy rows: `p, <role>, <permission>` from `roles.permissions`; `g, user:<id>, <role>, <tenant_id>|<scope>` from `assignments` (scope `org`, `team:<slug>`) and from `repo_maintainers` (`repo-maintainer`, `repo:<key>`; Task 8), active users only (DR-2).
  - `Resource = Org() | Team(slug) | Repo(key, team)`.
  - `async authorize(ctx: TokenContext, permission: str, resource: Resource, *, conn) -> AuthzDecision(allowed: bool, role: str | None, scope: str | None)`: an unknown permission raises `ValueError` (a programming error); the candidate domains follow the permission's catalogue kind (an `org` permission asks only `org`; a `team` permission asks `org` and the team; a `repo` permission asks `org`, the repo's team and the repo); each is `enforcer.enforce_ex(f"user:{ctx.user_id}", dom, permission)`, whose explanation names the granting role. A domain whose tenant part differs from `ctx.tenant_id` is refused before casbin is asked.
  - `async require(ctx, permission, resource, *, conn) -> AuthzDecision`: raises 403 `rbac.forbidden` naming the permission and scope.
  - `async effective_permissions(ctx, *, conn) -> EffectivePermissions` (from `get_implicit_permissions_for_user` per domain the user has).
  - `class EnforcerCache(max_tenants)`: `get(tenant_id, revision, load_rows) -> casbin.Enforcer`; a hit needs the same revision; otherwise it builds a new enforcer from rows read in the request's transaction through a read-only `_RowsAdapter(casbin.persist.Adapter)` (`load_policy` fills the model; `save_policy`, `add_policy`, `remove_policy` raise `NotImplementedError`). The revision comes from `TokenContext.rbac_revision`, so a warm request costs no extra query.
  - `build_enforcer(rows) -> casbin.Enforcer` (also used by the matrix tests without a database).
- **Control plane store and API:**
  - Tables (created in Task 5's `0004`): `roles(tenant_id, name slug, permissions text[], builtin bool, team_assignable bool, updated_at; pk (tenant_id, name))`, `assignments(tenant_id, id uuid, user_id, role, scope_kind, scope_value, created_by, created_at; pk (tenant_id, id); unique (tenant_id, user_id, role, scope_kind, scope_value); fk (tenant_id, user_id) -> users; fk (tenant_id, role) -> roles)`.
  - `DEFAULT_ROLES` seed policy (`seed_roles(conn)`, run per tenant by `admin tenant create` and by `migrate` for every tenant; adds missing builtin roles only, so tenant edits survive releases): `org-admin` (all), `team-lead`, `developer`, `approver`, `auditor`, `exec-viewer`, `service-account` (`run.trigger` only), and `repo-maintainer` (derived only, never assigned by hand). `team-lead` may assign only roles marked `team_assignable` (`developer`, `approver`) within its team.
  - Every write below runs `bump_rbac_revision` and its audit `append` in the same transaction.
  - API:
    - `GET /v1/rbac/roles`; `GET/PUT /v1/rbac/roles/{name}` (create or replace; org `rbac.assign`; unknown permission gives 422 naming it); `DELETE /v1/rbac/roles/{name}` (not builtin);
    - `GET /v1/rbac/assignments?team=&user=`; `POST /v1/rbac/assignments {email, role, team?}`, which creates the user in the caller's tenant if absent (status active, no token yet: the invite flow is M2-18); `DELETE /v1/rbac/assignments/{id}`;
    - `POST /v1/rbac/check {permission, team?, repo?, user?} -> CheckResult(allowed, reason, role)`; another `user` needs `rbac.assign` and an email unknown in the tenant gives 404.
  - Guards:
    - the last org-scope `org-admin` of the tenant cannot be removed;
    - `org-admin` cannot lose `rbac.assign`;
    - an assignment's team must exist in the tenant's active policy `teams` when a policy is active (Task 7); before any policy, any slug is accepted.
  - `GET /v1/me` gains `assignments` and `permissions: EffectivePermissions`.
  - `GET /v1/audit?after_seq=&limit=` (`audit.read.org`, or `audit.read.team` filtered to `team`); `GET /v1/audit/verify -> {problems: [...]}` (`audit.read.org`); both read only the caller's tenant chain.
  - Audit: `role.updated`/`role.deleted` and `role.assigned`/`role.revoked`, with `before`/`after` and the new `rbac_revision`.
- Endpoint cases: `GET/DELETE /v1/rbac/roles/{name}` (a custom role existing only in b), `DELETE /v1/rbac/assignments/{id}`, `POST /v1/rbac/check` with a user only in b; `GET /v1/rbac/assignments` and `GET /v1/audit` never list b's rows.

- [ ] **Step 1: Failing tests**:
  - Core:
    - `test_permission_catalog_matches_spec`: `set(PERMISSIONS)` equals the spec list plus the five additions; every value is a scope kind.
    - `test_org_admin_holds_any_gate_role` (ND-8): with `gate.approve`, yes; an org-admin role edited to drop `gate.approve`, no.
    - `test_role_held_names_the_role_actually_held` (RF4): an approver gets `approver`; an org admin without the named role gets `org-admin`.
    - `test_service_account_cannot_approve_gates`.
    - `test_org_authorizer_refuses_other_users`.
  - Casbin, without a database (`control-plane/tests/rbac/`):
    - `test_permission_matrix_cell[<role>-<permission>-<scope>]` (RF4): generated from `matrix.py`, a table transcribed from §23 (for each role and permission: own team, other team, org, own repo, other repo). Builds the enforcer from `DEFAULT_ROLES` with `build_enforcer` and asserts `authorize(...).allowed is expected`.
    - `test_model_conf_snapshot` (RF7): the packaged `model.conf` equals the text in the test.
    - `test_team_scoped_role_does_not_reach_other_team`.
    - `test_org_only_permission_inert_at_team_scope`: an `auditor` at team scope does not get `audit.read.org`.
    - `test_enforcer_never_matches_another_tenants_domain` (RF6, RF7): a `g` row for tenant b in a's enforcer never allows a request from a's context; `authorize` refuses a domain with b's id before casbin.
    - `test_authorize_names_the_role_that_granted`.
    - `test_unknown_permission_is_a_programming_error`.
    - `test_rows_adapter_is_read_only`: `add_policy` and `save_policy` raise.
  - Database and API:
    - `test_enforcer_built_from_tables_equals_seed` (RF7): a freshly created tenant's tables build an enforcer whose policy equals `build_enforcer(DEFAULT_ROLES rows)`.
    - `test_role_change_visible_on_next_request_across_replicas` (RF7): two apps with separate `EnforcerCache`s on one database; an assignment made through app 1 is allowed on the next request to app 2; a revocation is refused there too.
    - `test_failed_change_does_not_bump_revision` (RF7): a guard refusal (last admin) rolls back; `rbac_revision` and the audit chain are unchanged.
    - `test_rbac_in_tenant_a_does_not_affect_tenant_b`: the same email is org admin in a and developer in b; each token gets its own tenant's answer.
    - `test_team_lead_assigns_only_developer_or_approver_in_own_team` (RF4): `approver` in payments gets 201; `team-lead` gets 403 `rbac.not_assignable`; team `billing` gets 403.
    - `test_last_org_admin_cannot_be_removed`: 409 `rbac.last_admin`.
    - `test_role_edit_cannot_remove_rbac_assign_from_org_admin`: 409.
    - `test_unknown_permission_in_role_refused`: 422 naming it.
    - `test_assignment_to_unknown_team_refused_when_policy_active`: 422; the hint names `teams/<team>.yaml` (exercised fully in Task 7's suite).
    - `test_every_rbac_change_is_audited_with_before_and_after`.
    - `test_check_api_answers_for_caller`; `test_checking_another_user_needs_rbac_assign`.
    - `test_me_lists_effective_permissions`.
    - `test_audit_read_needs_audit_read_permission`: developer gets 403; auditor gets 200; `verify` returns `[]`; the list holds only the caller's tenant records.
    - `test_api_isolation[rbac-*]`, `[audit-*]` (harness).
- [ ] **Step 2: Run** `uv run pytest core/tests/orgapi control-plane/tests/rbac control-plane/tests/test_rbac_api.py control-plane/tests/test_audit_api.py control-plane/tests/test_api_isolation.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement.** Pin the four packages, `uv lock`, check their `upload-time` (on or before 2026-10-08), run `uv run lint-imports` (the casbin contract must pass and must fail on a scratch import elsewhere) and the licence check (pycasbin declares "Apache 2.0" in free text; the OSI Apache classifier decides).
- [ ] **Step 4: Run.** Expected: PASS.
- [ ] **Step 5: Commit.** `feat(control-plane): RBAC through pycasbin - per-tenant domains, default roles, assignments, check API (M2-03)`
- [ ] **Step 6: Slice 1a checkpoint.**
  - `uv run python -m agentic_cp openapi --check`, `uv run lint-imports`, `uv run python scripts/licence_check.py`, `uv run python scripts/check.py --all` (Windows and the Linux container), `rg -n "fmt: (skip|off)" control-plane commons core` returns nothing.
  - Sonnet high-effort review subagent over Tasks 1-6 against RF2, RF4, RF5, RF6 and RF7; fix, or defer with DF entries. Slice 1a can merge here: it is a working, tenant-isolated auth/RBAC/audit service without policy or runs.

---

## Slice 1b: policy sync, repos, ingest, CLI side (Tasks 7-11)

### Task 7: Policy sync per tenant from its git policy repo (M2-02)

**Files:**
- Create: `control-plane/src/agentic_cp/gitops.py` (shared `GitRunner`), `control-plane/src/agentic_cp/policy_sync/{__init__.py, fetch.py, validate.py, service.py, api.py, worker.py}`, `control-plane/src/agentic_cp/tenancy/api.py`, `migrations/versions/0005_policy.py` (tables, function `resolve_webhook`).
- Modify: `core/src/agentic/policy/load.py` (`load_policy(root, *, version=None)` and `tree_digest(root)`; exported from `agentic.policy`), `core/src/agentic/orgapi/models.py` (`ActivePolicy`, `PolicyVersionInfo`, `SyncOutcome`, `TenantInfo`, `PolicySource`, `WebhookIssued`), `__main__.py` (`worker`, `admin tenant set-policy-repo`), `tables.py` and `control-plane/tests/isolation.py`.
- Test: `core/tests/policy/test_load_version.py`, `control-plane/tests/test_policy_sync.py`, `control-plane/tests/test_policy_api.py`, `control-plane/tests/test_tenant_api.py`.

**Interfaces:**
- **Core:**
  - `load_policy(root, *, version: str | None = None)`: an explicit version must match the ledger's pattern (`git:<40 hex>` or `sha256:<64 hex>`), else `PolicyError`; without it, behaviour is unchanged (backward compatible).
  - `tree_digest(root) -> str`: skips `.git`; length-prefixed.
- `GitRunner` (subprocess; the git flags and env from Global Constraints; `GIT_CONFIG_GLOBAL` from `git_config_dir/<slug>.gitconfig` when present, ND-15; error text scrubbed); injectable for tests.
- **Tables** (tenant-owned, RLS):
  - `policy_versions(tenant_id, version, commit, branch, status accepted|rejected, problems text[], bundle bytea null, digest, tree_digest, commit_at, received_at; pk (tenant_id, version))`, insert-only;
  - `policy_active(tenant_id pk, version, branch, activated_at; fk (tenant_id, version) -> policy_versions)`.
- `agentic.resolve_webhook(sha256 bytea) RETURNS uuid` (definer function, registered).
- `validate_policy_tree(root, version) -> list[str]` runs:
  - `load_policy(root / "policy", version=version)`;
  - `DefinitionSet.discover(org=root, team=root / "teams" / t, repo=None, allow_repo_pipelines=policy.allow_repo_pipelines)` for the org layer alone and for every `teams/<t>/` folder;
  - `agentic.skills.catalog.load_catalog(root / "skills")` when present (admission stays in policy-repo CI).
- `async sync_once(conn, git, tenant: TenantRow, settings, clock) -> SyncOutcome(kind: unchanged|activated|rejected|busy|unconfigured, version, problems)`, inside `tenant_tx(tenant.id)`:
  1. no `policy_repo_url`: `unconfigured`;
  2. `pg_try_advisory_xact_lock(hashtextextended('agentic.policy_sync:' || tenant_id, 0))`, else `busy`;
  3. `git fetch --depth 1 origin <tenant.policy_branch>` in `policy_workdir/<tenant_id>` and `FETCH_HEAD`;
  4. if the version is already stored for this tenant, `unchanged`;
  5. `git archive`, extract (`filter="data"`), validate;
  6. insert the version; when accepted, upsert `policy_active` and audit `policy.activated` (before: the previous version); otherwise audit `policy.rejected` (reason: the first problem; `after.problems`: all of them, at most 50).

  A bundle over 20 MiB is rejected (`policy.too_large`).
- `worker`: holds a session `pg_try_advisory_lock` for leadership; every `policy_poll_seconds` loops over `active_tenant_ids()` and runs `sync_once` per tenant (git timeout 120 s each), then the CODEOWNERS refresh from Task 8. One tenant's failure is logged with its slug and does not stop the others.
- API (all authenticated, all in the caller's tenant):
  - `GET /v1/tenant -> TenantInfo(slug, name, policy_repo (credentials scrubbed), policy_branch)`;
  - `PUT /v1/tenant/policy-source {repo, branch}` (org `policy.edit.org`): a URL with embedded credentials is 422 `policy.credentials_in_url`; audited `tenant.policy_source_changed` (before/after); triggers a sync;
  - `POST /v1/tenant/webhook-token` (org `policy.edit.org`): rotates the hook token and returns `WebhookIssued(url)` once; audited `tenant.webhook_rotated` (never the value);
  - `GET /v1/policy/active -> ActivePolicy(version, branch, digest, tree_digest, activated_at, commit_at, bundle_path)`, 404 `policy.none` before the tenant's first accepted sync;
  - `GET /v1/policy/versions?limit=20 -> list[PolicyVersionInfo]`;
  - `GET /v1/policy/versions/{version}/bundle`: `application/gzip`, `ETag: "<digest>"`, `Cache-Control: private, immutable`;
  - `POST /v1/policy/sync` (org `policy.edit.org`): runs `sync_once` for the caller's tenant and returns `SyncOutcome` (202 when `busy`);
  - `POST /v1/policy/webhook/{hook_token}` (no bearer, ND-14): the tenant comes from `resolve_webhook(sha256(hook_token))`; unknown or rotated tokens get 404; the payload is ignored; 202 and a sync of that tenant only; rate-limited per tenant.
- Operator: `python -m agentic_cp admin tenant set-policy-repo --tenant S --repo URL [--branch B]` (same validation and audit, actor `system:operator`).
- Endpoint cases: `GET /v1/policy/versions/{version}/bundle` with a version only b has; `GET /v1/policy/versions` never lists b's.

- [ ] **Step 1: Failing tests**:
  - `test_load_policy_accepts_explicit_version` (core): a bad string raises `PolicyError`.
  - `test_policy_tree_digest_is_length_prefixed` (core): files (`a`, `bc`) and (`ab`, `c`) differ; the same bytes give the same digest.
  - `test_valid_policy_is_activated_with_git_version`: a local repo seeded from `templates/standalone`; `active.version == "git:" + sha`; `policy.activated` audited in that tenant's chain.
  - `test_invalid_policy_rejected_and_previous_kept` (RF3): a commit breaking `tiers.yaml` gives `rejected`; the problems name `tiers.yaml`; `active.version` is unchanged; `GET /v1/policy/versions` shows the rejection.
  - `test_each_tenant_syncs_its_own_repo` (RF3, RF6): tenants a and b with different repos and branches get different active versions; a's `GET /v1/policy/active` shows a's, b's shows b's; work folders differ.
  - `test_one_tenant_failing_does_not_stop_others`: a's repo URL is unreachable; b still activates in the same worker pass; the log names a's slug.
  - `test_unconfigured_tenant_reports_unconfigured`.
  - `test_definitions_validated_for_every_team`: a broken `teams/payments/agents/x.md` is rejected, naming it.
  - `test_unchanged_head_is_not_reprocessed`.
  - `test_tenant_branch_is_used`: tenant branch `staging` activates its own commit.
  - `test_bundle_digest_matches_and_is_deterministic` (RF3): two fresh databases syncing the same commit get equal `digest` and `tree_digest`; the downloaded bytes hash to `digest`.
  - `test_git_runs_with_autocrlf_off_and_tenant_git_config`: the recorded `GitRunner` commands include `core.autocrlf=false`; with `<slug>.gitconfig` present, `GIT_CONFIG_GLOBAL` points at it.
  - `test_only_one_sync_at_a_time_per_tenant`: two concurrent `sync_once` calls for a give one `busy`; a call for b at the same time is not blocked.
  - `test_webhook_token_resolves_only_its_tenant`: a's hook token triggers a's sync only; an unknown token and a rotated one get 404; the stored column holds only the hash.
  - `test_policy_source_change_requires_policy_edit_org_and_is_audited`.
  - `test_policy_source_refuses_credentials_in_url` (RF5): `https://u:tok@host/p.git` gives 422; nothing stored or logged contains `tok`.
  - `test_manual_sync_requires_policy_edit_org`.
  - `test_active_policy_requires_authentication`.
  - `test_assignment_to_unknown_team_refused` (completes Task 6's case with a real active policy).
  - `test_tenant_isolation[policy_versions-*]`, `[policy_active-*]`, `test_api_isolation[policy-*]` (harness).
- [ ] **Step 2: Run** `uv run pytest core/tests/policy control-plane/tests/test_policy_sync.py control-plane/tests/test_policy_api.py control-plane/tests/test_tenant_api.py control-plane/tests/test_tenant_isolation.py control-plane/tests/test_api_isolation.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run.** Expected: PASS.
- [ ] **Step 5: Commit.** `feat(control-plane): per-tenant policy sync - git fetch, validation, versions, bundle, webhook (M2-02)`

### Task 8: Repo registration and maintainers from CODEOWNERS, per tenant (M2-03)

Moved after policy sync: registration checks the remote against the tenant's active policy.

**Files:**
- Create: `control-plane/src/agentic_cp/rbac/{codeowners.py, repos.py}`, `migrations/versions/0006_repos.py`, `core/src/agentic/policy/repos.py`.
- Modify: `cli/src/agentic_cli/repo_guard.py` (imports the moved matcher; behaviour unchanged), `rbac/authz.py` (maintainer `g` rows), `tables.py` and `control-plane/tests/isolation.py`.
- Test: `control-plane/tests/test_codeowners.py`, `control-plane/tests/test_repos_api.py`, `core/tests/policy/test_repos.py`.

**Interfaces:**
- **Core `agentic.policy.repos`** (moved from `repo_guard`): `normalise_remote(url) -> str` and `repo_allowed(policy, remote) -> bool`. The CLI keeps the same behaviour and tests; DF-199 stays at M1-50. The control plane and the CLI now decide `allowed_repo_patterns` with one function.
- `fetch_codeowners(git, remote, branch) -> CodeownersFile(commit, path, text) | None`: probes `.github/CODEOWNERS`, `CODEOWNERS`, `docs/CODEOWNERS`, `.gitlab/CODEOWNERS` at the fetched head (the tenant's `GitRunner`, ND-15).
- `PROBE_PATHS = (".agentic/config.yaml", ".agentic/agents/x.md", ".agentic/skills/x/SKILL.md", "AGENTS.md")`.
- `maintainers(text, host) -> Maintainers(handles: set[str], emails: set[str], unresolved: set[str])`: union of the last-matching owners over the probes (codeowners library); `@org/team` goes to `unresolved` (ND-6). Handles and emails are matched only against the tenant's own users.
- Tables (tenant-owned, RLS): `repos(tenant_id, repo_key host/owner/name, remote, team, default_branch, codeowners_commit, refreshed_at; pk (tenant_id, repo_key))`, `repo_maintainers(tenant_id, repo_key, user_id, source_entry; fks with tenant_id)`. Derived maintainers appear in `/v1/me` as `repo-maintainer` assignments with `derived: true` and as casbin `g` rows in domain `<tenant_id>|repo:<key>`. A maintainer sync that changes the set bumps `rbac_revision`.
- API:
  - `POST /v1/repos {remote, team}`: `agent.edit.repo` in `team`; the remote must pass `repo_allowed(tenant's active policy)` (422 `repo.not_allowed`); registers and refreshes;
  - `GET /v1/repos/{repo_key:path}/maintainers -> {users, unresolved, commit}`;
  - `POST /v1/repos/{repo_key:path}/refresh`;
  - `PUT /v1/users/{email}/handles` (org `rbac.assign`; bumps `rbac_revision`).
- The worker refreshes each tenant's registered repos every `AGENTIC_CP_CODEOWNERS_REFRESH_MINUTES` (default 60), inside that tenant's context.
- Audit: `repo.registered`, `maintainers.synced` (before and after sets).
- Endpoint cases: maintainers and refresh for a repo registered only in b; handles for a user only in b.

- [ ] **Step 1: Failing tests**:
  - `test_codeowners_maintainers_union_of_agentic_paths`: `* @carol` then `/.agentic/ @alice` then `AGENTS.md bob@x.com` gives `{alice, bob@x.com}`; carol is overridden.
  - `test_codeowners_team_entries_reported_unresolved`.
  - `test_handles_map_host_qualified`: `@alice` on github.com maps to the user with `github.com:alice`, not `gitlab.com:alice`.
  - `test_handles_resolve_only_within_the_tenant` (RF6): `github.com:alice` exists in tenants a and b; a's repo makes a's alice maintainer only.
  - `test_same_repo_in_two_tenants_has_separate_maintainers`.
  - `test_fetch_codeowners_from_git_default_branch`: a local bare repo through a `file://` URL; the commit sha is returned.
  - `test_codeowners_change_updates_maintainers` (tracker done criterion): a new commit drops alice; after refresh, alice loses `agent.approve.repo` on that repo on the next request (the revision moved); `maintainers.synced` has before and after.
  - `test_maintainer_allows_repo_permissions_only_for_that_repo`.
  - `test_repo_registration_requires_agent_edit_repo_and_allowed_pattern`.
  - `test_git_errors_never_show_credentials` (RF5): the remote `https://u:tok123@host/x.git` fails, and neither the problem, the log nor the audit contains `tok123`.
  - `test_repo_allowed_matches_cli_guard` (core): the cases from `cli/tests/test_repo_guard.py` give the same answer through `repo_allowed`.
  - `test_tenant_isolation[repos-*]`, `[repo_maintainers-*]`, `test_api_isolation[repos-*]`, `[users-*]` (harness).
- [ ] **Step 2: Run** `uv run pytest control-plane/tests/test_codeowners.py control-plane/tests/test_repos_api.py core/tests/policy/test_repos.py cli/tests/test_repo_guard.py control-plane/tests/test_tenant_isolation.py control-plane/tests/test_api_isolation.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run.** Expected: PASS (the CLI guard tests unchanged).
- [ ] **Step 5: Commit.** `feat(control-plane): per-tenant repo maintainers derived from CODEOWNERS over git (M2-03)`

### Task 9: Run event ingest with chain verification and idempotency, per tenant (M2-04)

**Files:**
- Create: `control-plane/src/agentic_cp/ingest/{__init__.py, verify.py, store.py, api.py}`, `migrations/versions/0007_runs.py`.
- Modify: `commons/src/sdlc_commons/ledger.py` (`verify_entries`), `core/src/agentic/orgapi/models.py` (`IngestRequest`, `IngestResult`, `ChainHead`, `RunSummary`), `tables.py` and `control-plane/tests/isolation.py`.
- Test: `commons/tests/test_ledger_verify_entries.py`, `control-plane/tests/test_ingest.py`.

**Interfaces:**
- **Commons** `verify_entries(entries: Sequence[Mapping], *, run_id: str, after: tuple[int, str] = (0, "")) -> list[str]`. It applies Zyroflo's rules (`prev` link, recorded hash through `entry_hash`, consecutive `seq`, `run_id` claim) to entries in memory, continuing from `after`, plus:
  - the event type is in Zyroflo's `events.TYPES`, and its fields are in `events.FIELDS`;
  - the `ext.agentic` values match `ledger.FIELDS` patterns;
  - the run identity (team, policy_version) is identical on every entry (the `RunLedger.verify` rule).

  Each problem names the entry's position (`event <seq>`).
- **Tables** (tenant-owned, RLS):
  - `runs(tenant_id, run_id, team, started_by user_id, policy_version, policy_known bool, status, head_seq, head_hash, opened_at, last_event_at; pk (tenant_id, run_id))`;
  - `run_events(tenant_id, run_id, seq, hash, prev, event, step, at text, payload jsonb, received_at; pk (tenant_id, run_id, seq); fk (tenant_id, run_id) -> runs)`, insert-only.
- `POST /v1/runs/{run_id}/events` with `IngestRequest(entries: list[dict], 1..max_ingest_entries)`; the body is capped at `max_body_bytes` (413 `ingest.too_large`). In one transaction in the caller's tenant:
  1. lock the run row (`SELECT ... FOR UPDATE`; a new run is inserted first with `ON CONFLICT DO NOTHING`);
  2. entries with `seq <= head` must match the stored `hash`: equal ones count as `duplicates`; a different hash is a 409 `ingest.fork` with `head`;
  3. the rest must start at `head + 1`, else 409 `ingest.gap` with `head`;
  4. `verify_entries(rest, after=head)`, else 422 `ingest.chain_broken` with `problems`;
  5. authorisation (through `require`):
     - the first batch must start at `run_opened`, and the caller needs `run.trigger` in the run's team (`ext.agentic.team`); the run is bound to that caller (`started_by`);
     - later batches come from `started_by`, or from a caller with `run.cancel.any` in that team;
  6. insert, move the head, set `status` from `run_closed`, flag a `policy_version` the tenant never accepted (ND-7).

  A rejection stores nothing for the run and writes an `ingest.chain_rejected` / `ingest.fork` audit record in its own transaction (same tenant). Returns `IngestResult(accepted, duplicates, head)`. A `run_id` used by another tenant is a different run (the key includes `tenant_id`).
- `GET /v1/runs/{run_id} -> RunSummary`: readable by the starter, by `dashboard.view.team` / `audit.read.team` in its team, or org-wide.
- `GET /v1/runs/{run_id}/events?after_seq=&limit=`: the stored entries exactly as received (for M2-26 `audit verify`); same permissions.
- Endpoint cases: summary and events of a run only b has.

- [ ] **Step 1: Failing tests**:
  - Commons:
    - `test_verify_entries_accepts_a_real_ledger`: a `RunLedger` in `tmp_path` with 6 events.
    - `test_verify_entries_continues_from_a_head`.
    - `test_verify_entries_names_an_edited_entry`.
    - `test_verify_entries_refuses_unknown_event_type`.
    - `test_verify_entries_refuses_another_runs_entry`.
    - `test_verify_entries_refuses_identity_change`.
  - Control plane:
    - `test_first_batch_registers_the_run`: `head.seq == 6`; `runs.team` is from `ext`.
    - `test_broken_chain_rejected_and_nothing_stored` (RF1): entry 3 of 5 edited gives 422 `ingest.chain_broken`; the problems name event 3; `GET /v1/runs/{id}` gives 404; `ingest.chain_rejected` is audited.
    - `test_duplicate_replay_is_a_noop` (RF1): the second identical POST gives `accepted == 0`, `duplicates == 5`; there are 5 rows.
    - `test_overlapping_replay_accepts_only_new`: 1..5 then 3..8 gives `accepted == 3`, `duplicates == 3`.
    - `test_fork_is_rejected` (RF1): seq 4 with another hash gives 409; the head is unchanged.
    - `test_gap_is_409_with_head`: 1..3 then 6..7 gives 409 with `head.seq == 3`.
    - `test_caller_needs_run_trigger_in_the_runs_team`: 403.
    - `test_only_the_starter_or_run_cancel_any_may_append`.
    - `test_batch_too_large_is_413`.
    - `test_unknown_policy_version_is_flagged_not_dropped` (ND-7).
    - `test_policy_version_known_only_to_another_tenant_is_flagged` (ND-7, RF6).
    - `test_same_run_id_in_two_tenants_is_two_runs` (RF6): a and b ingest different chains under one `run_id`; each reads back its own; neither head moves the other.
    - `test_run_closed_sets_status`.
    - `test_run_summary_and_events_readable_by_starter_and_team_auditor`: another team's developer gets 403; a token of another tenant gets 404.
    - `test_concurrent_batches_for_one_run_serialise` (RF1): two concurrent identical posts store n rows; the results sum to `accepted + duplicates == 2n`.
    - `test_runtime_role_cannot_rewrite_run_events` (RF2, also covered in Task 4).
    - `test_tenant_isolation[runs-*]`, `[run_events-*]`, `test_api_isolation[runs-*]` (harness).
- [ ] **Step 2: Run** `uv run pytest commons/tests/test_ledger_verify_entries.py control-plane/tests/test_ingest.py control-plane/tests/test_tenant_isolation.py control-plane/tests/test_api_isolation.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run.** Expected: PASS.
- [ ] **Step 5: Commit.** `feat(control-plane): per-tenant run event ingest with chain verification and idempotent replay (M2-04)`

### Task 10: CLI side: client, login, policy from the control plane, org roles, sink (M2-01..M2-04 client)

**Files:**
- Create: `cli/src/agentic_cli/control_plane.py`.
- Modify:
  - `cli/src/agentic_cli/commands/login.py` (`--for control-plane`);
  - `cli/src/agentic_cli/policy_source.py` (control-plane source);
  - `cli/src/agentic_cli/workspace.py` (recorded version and tamper check);
  - `cli/src/agentic_cli/commands/init.py` and `dev_init.py` (roles from the control plane, DF-203);
  - `cli/src/agentic_cli/repo_guard.py` (check 7 asks the control plane, DF-184);
  - `cli/src/agentic_cli/publishing.py` (ND-11, DF-291).
- Test: `cli/tests/test_control_plane_client.py`, `cli/tests/test_login.py` (new cases), `cli/tests/test_policy_from_control_plane.py`, `cli/tests/test_init_org_rbac.py`, `cli/tests/test_control_plane_sink.py`, `cli/tests/run/test_runtime.py` (link case), `control-plane/tests/contract/test_cli_against_control_plane.py`.

**Interfaces:**
- Credential `CONTROL_PLANE_TOKEN = "control_plane_token"` (env `AGENTIC_CONTROL_PLANE_TOKEN`; the keyring in `agentic.auth`). One token per control-plane URL (ND-17).
- `control_plane_url(ws)`: moves from `publishing.py`; the same lookup (`AGENTIC_CONTROL_PLANE_URL`, then the user config `control_plane_url`).
- `class ControlPlaneError(CliError)`, with subclasses `ControlPlaneUnavailable` (exit 1), `ControlPlaneRefused` (401/403/426/429, exit 2, the server's hint or `agentic login --for control-plane`) and `ChainRejected` (`problems`, `head`).
- `ControlPlaneClient(base_url, token, *, transport=None, timeout=(5, 15))` over `httpx2.Client`:
  - TLS through truststore, like the gateway client; honours `HTTPS_PROXY` / `NO_PROXY`;
  - the Global Constraints headers, and no tenant anywhere;
  - methods: `me() -> Me`, `active_policy() -> ActivePolicy`, `bundle(policy: ActivePolicy) -> bytes` (digest checked, else `ControlPlaneRefused`), `check(permission, *, team=None, repo=None) -> CheckResult`, `ingest(run_id, entries) -> IngestResult`, `register_repo(remote, team)`, `run_url(run_id) -> str`.
  - `from_workspace(ws) -> ControlPlaneClient | None` (None without a URL).
- `agentic login --for control-plane [--url U]` (ND-13):
  - prompts, or reads `--key-stdin`;
  - calls `me()` first and stores the token only on 200;
  - prints `Signed in to <url> (<tenant name>) as <email> - <team>: <roles>`;
  - JSON: `{"stored": "control_plane_token", "verified": true, "tenant": ..., "user": ..., "teams": {...}}`.
- `policy_source`, org mode with a control plane (ND-2):
  - fetches `active_policy()` and `bundle()`, extracts into the scratch folder (`filter="data"`) and loads `load_policy(folder, version=active.version)`;
  - `commit()` writes `fetched.json` with `source_kind: "control-plane"`, `url`, `tenant` (slug, for display and to refuse a cache from another tenant after re-login), `version`, `digest`, `tree_digest`, `fetched_at`, `expires_at`;
  - an unreachable control plane falls back to the unexpired cache with a warning; an expired one is refused (today's rules); a cache whose `tenant` differs from `me().tenant` is refused with hint `agentic init`;
  - without a control plane URL, the git clone path is unchanged.
- `Workspace.policy()`: when `fetched.json` says `control-plane`, recompute `tree_digest(cache/repo)`. A mismatch is a `CliError` REFUSED: "the cached org policy was changed locally", hint `agentic init`. Then load with `version=fetched.version`.
- Org-mode `init` with a control plane:
  - roles come from `me().assignments` (`org-admin` or `team-lead` in the user's team selects authoring skills); `config.roles` is ignored, with one warning;
  - check 7 is `client.check("agent.edit.repo", team=..., repo=normalise_remote(origin))`, refused as `GuardRefusal(7)` with hint "ask your team lead or org admin";
  - after the guard passes, `init --repo` registers the repo (`POST /v1/repos`, idempotent).
- `OrgAuthorizer` (Task 6) is built from a fresh `me()` at decision time; `role_held` is available to M2-21.
- `class ControlPlaneSink` (`name = "control-plane"`): implements `agentic.telemetry.sinks.EventSink`; `deliver(run_id, entries)` calls `client.ingest`, and any refusal raises, so `forward` keeps its cursor. It is **not wired into runs here**: M2-20 wires it, with a short timeout, the offline replay command and DF-167/172/179/185.
- PR links (ND-11): `run_url(run_id)` = `<cp>/v1/runs/<run_id>`; handoff links stay repo-relative.

- [ ] **Step 1: Failing tests**:
  - `test_client_sends_bearer_and_cli_version_headers` (MockTransport).
  - `test_client_never_sends_a_tenant` (RF6): no request header, query or body field names a tenant.
  - `test_problem_json_becomes_cli_error_with_hint`: 401 gives exit 2, and the hint contains `agentic login --for control-plane`.
  - `test_unreachable_control_plane_is_a_clear_error`: `ConnectError` gives `ControlPlaneUnavailable`, naming the URL without credentials.
  - `test_login_for_control_plane_verifies_before_storing`: the output names the tenant, the user and teams.
  - `test_login_for_control_plane_refuses_a_rejected_token_and_stores_nothing`.
  - `test_login_for_control_plane_without_url_is_invalid`: the hint names `control_plane_url` and `AGENTIC_CONTROL_PLANE_URL`.
  - `test_org_init_fetches_the_active_bundle`: `LoadedPolicy.policy.version == active.version`; `fetched.json` has `source_kind == "control-plane"` and the tenant slug.
  - `test_cached_policy_of_another_tenant_is_refused`.
  - `test_bundle_digest_mismatch_refused`: nothing is cached.
  - `test_bundle_with_path_traversal_refused`.
  - `test_unreachable_control_plane_uses_unexpired_cache_with_warning`.
  - `test_expired_cache_with_unreachable_control_plane_is_refused`.
  - `test_workspace_policy_uses_recorded_version` (RF3).
  - `test_locally_edited_cached_policy_is_refused` (RF3).
  - `test_git_policy_source_still_works_without_control_plane`.
  - `test_org_init_takes_roles_from_control_plane` (RF4, DF-203).
  - `test_org_init_repo_checks_agent_edit_repo` (DF-184): allowed passes check 7; denied gives `GuardRefusal` with `check == 7`.
  - `test_sink_delivers_entries_via_ingest`.
  - `test_sink_raises_on_chain_rejection_so_the_cursor_stays`.
  - `test_pr_links_point_at_control_plane_run_api` (DF-291).
  - **Contract tests** (real app under uvicorn on `127.0.0.1:0` in a thread, plus Postgres, two tenants):
    - `test_login_me_roundtrip`;
    - `test_policy_fetch_end_to_end`: git repo, `sync_once`, CLI org fetch; the run's policy version equals `git:<sha>`; a token of the other tenant gets that tenant's policy;
    - `test_ledger_forward_to_control_plane_end_to_end`: `RunLedger` + `ForwardingRecorder([ControlPlaneSink])`; the control plane's head equals the local last hash; after deleting the cursor and forwarding again, only duplicates are reported; the other tenant's token gets 404 for the run.
- [ ] **Step 2: Run** `uv run pytest cli/tests/test_control_plane_client.py cli/tests/test_login.py cli/tests/test_policy_from_control_plane.py cli/tests/test_init_org_rbac.py cli/tests/test_control_plane_sink.py control-plane/tests/contract -q`. Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the whole suite: `uv run pytest -q`. Expected: PASS.
- [ ] **Step 5: Commit.** `feat(cli): control-plane client - login, policy bundle, org roles, ingest sink (M2-01..M2-04)`

### Task 11: Server entry points, final verification, review

**Files:**
- Modify: `control-plane/src/agentic_cp/__main__.py` (`serve` runs uvicorn programmatically on `AGENTIC_CP_HOST`/`AGENTIC_CP_PORT` and refuses to start under ND-5; `worker`; `migrate` runs `alembic upgrade`, then `grant-runtime`, then `seed_roles` for every tenant, then `enforcement_status`).
- Test: `control-plane/tests/test_main.py`.

- [ ] **Step 1: Failing tests**:
  - `test_serve_refuses_unenforced_database`: exit 2, and the message names `AGENTIC_CP_REQUIRE_DB_ENFORCEMENT` and each reason.
  - `test_migrate_applies_grants_and_seeds_every_tenant`: a builtin role added in a new release appears in both tenants; an edited role is left alone.
  - `test_help_lists_commands`: `serve`, `worker`, `migrate`, `admin` (with `tenant create`, `tenant list`, `tenant set-policy-repo`, `issue-token`, `audit verify`), `db`, `openapi`.
  - `test_every_object_route_has_an_isolation_case` (RF6): every `/v1` route with a path parameter, except the webhook (its own test), is covered by an `ENDPOINT_CASES` entry; every list route has a `must_not_list` check.
- [ ] **Step 2: Implement; run** `uv run pytest control-plane -q`. Expected: PASS.
- [ ] **Step 3: Verify:**
  - `uv run python -m agentic_cp openapi --check`
  - `uv run lint-imports`
  - `uv run python scripts/licence_check.py`
  - `uv run python scripts/check.py --all` (Windows natively with Rancher Desktop or Podman, and the Linux container with the Postgres sidecar)
  - `rg -n "fmt: (skip|off)" control-plane commons core cli` returns nothing.
- [ ] **Step 4: Review.** Sonnet high-effort review subagent over slice 1b and the slice as a whole against the Review Focus (all seven) and the Deferred items table; fix, or defer with DF entries.
- [ ] **Step 5: Commit.** `feat(control-plane): serve/worker/migrate entry points; slice 1 verified (M2-01..M2-05)`

---

## Slice 2: remaining SP15 rows (interfaces only; each gets its TDD plan before it starts)

The same package and conventions apply: problem+json, `agentic.orgapi` models, audit on every change in the tenant's chain, insert-only evidence, CLI client methods in `agentic_cli/control_plane.py`. **Every new table is tenant-owned** (created through `enable_tenant_rls`, registered in `TENANT_TABLES` with an isolation factory), every object route gets an `ENDPOINT_CASES` entry, permission checks go through `authorize`/`require`, and every worker job loops over `active_tenant_ids()` inside each tenant's context.

| Row | Module and tables | API | CLI / client side | Key tests (from tracker) | Depends |
|---|---|---|---|---|---|
| M2-06 kill switch | `killswitch/`; `kill_switches(tenant_id, id, scope_kind tenant\|team\|user\|repo\|pipeline\|agent\|model\|backend, scope_value, mode kill\|pause, reason, created_by, created_at, cleared_by, cleared_at)`. The spec's "global" switch is tenant-wide (`scope_kind tenant`); a platform-wide stop is the operator command `admin killswitch set --all-tenants`, which writes one tenant switch in every tenant, each audited in that tenant's chain as `system:operator` | `POST /v1/killswitch {scope_kind, scope_value, mode, reason}` (tenant-wide or any scope: `killswitch.global`; team leads only within their team: `killswitch.team`); `DELETE /v1/killswitch/{id}`; `GET /v1/killswitch/state?team&user&repo&pipeline&agent&model&backend -> KillState(killed, mode, switches, checked_at, ttl_seconds=30)` with `ETag`/304 for cheap polling, always for the caller's tenant | `ControlPlaneClient.kill_state(scope) -> KillState`; core protocol `KillSwitchSource` for the engine (M2-19); the CLI caches at most 30 s | scope matching per kind (agent by name and name@version); team lead limited to own team; a tenant switch in b never stops a run in a; the all-tenants command reaches every tenant; every change audited `killswitch.set` / `.cleared` | M2-03, M2-05 |
| M2-07 notifications | `notify/`; `notifications(tenant_id, id, kind, to, subject, body, dedupe_key, status pending\|sent\|failed, attempts, next_attempt_at; unique (tenant_id, dedupe_key))` outbox; `Notifier` protocol with `SmtpNotifier` (aiosmtplib 5.1.3, STARTTLS) and a `WebhookNotifier` seam; deployment relay settings `AGENTIC_CP_SMTP_*`, per-tenant sender address and display name in `tenant_settings(tenant_id pk, smtp_from, smtp_from_name)` (a per-tenant relay is a later option); templates as package data (`string.Template`), rendered with the tenant's name | none public; the worker drains each tenant's outbox in turn under an advisory lock keyed by tenant | — | aiosmtpd 1.4.6 capture server: gate-waiting, budget and invite mails rendered with the tenant's sender; retry with backoff; one mail per `(tenant, dedupe_key)`; a mail of a never goes to an address that exists only in b's users | M2-01 |
| M2-08 min CLI version + concurrency | middleware reads `X-Agentic-CLI-Version`; `min_cli_version` from the tenant's org policy (`org.yaml`, new field), else `AGENTIC_CP_MIN_CLI_VERSION` (deployment floor; the higher wins); `POST /v1/runs {run_id, team, pipeline, repo}` registers a run atomically against the tenant's `limits.yaml` concurrency (advisory lock on tenant and team) before its first event | 426 `cli.too_old` with an upgrade hint; `POST /v1/runs` gives 201, or 409 `runs.concurrency_cap` naming the cap; `GET /v1/concurrency?team=` | the CLI maps 426 to exit 2 with the upgrade message; preflight (M2-21) registers the run; closes DF-182's version part | an old CLI refused with the message; cap enforced under a race (two registrations at cap-1); caps of a and b counted separately | M2-01, M2-03 |
| M2-09 routing generation | `routing/generate.py` pure `routing_rules(policy) -> RoutingDocument` (tiers, team overrides, classification restrictions, team limits); `routing/apply.py` `GatewayAdmin` client to TokenLean's ST-10 endpoint, addressed to the tenant's `gateway_tenant` (ND-16); runs on every `policy.activated` of that tenant | `GET /v1/routing/current` (org admin of the tenant) | — | generated rules match a fixture; applied and read back for the right TokenLean tenant; a's activation never rewrites b's routing; audited `routing.applied` with the policy version | M2-02, M2-30, M2-35 |
| M2-10 graceful pause | `mode: pause` on M2-06 switches; maintenance pause = a tenant pause switch (all tenants: the operator command) | as M2-06 | engine (M2-19): finish the current turn, checkpoint, hold; resume when cleared | a paused run resumes without loss; a killed run does not | M2-06, M2-19 |
| M2-11 auto-trip | `autotrip/` worker job per tenant: team spend rate (TokenLean `usage_events` via M2-37 views, filtered to the tenant's `gateway_tenant`) and error rate against thresholds in the tenant's policy; creates a pause switch with actor `system:auto-trip`, notifies, audits; cooldown plus manual clear | `GET /v1/autotrip/rules` | — | synthetic spend series trips and resets correctly; one alert per trip; b's spend never trips a | M2-10, M2-07, M2-37 |
| M2-12 health + metrics | `/readyz` (exists, includes tenancy enforcement); `/metrics` via prometheus-client 0.26.0 with bounded labels (route template, status class, rejection code, tenant slug: spec §14 keeps `tenant` as a bounded label): `ingest_events_total{tenant}`, `ingest_rejected_total{tenant,code}`, `ingest_lag_seconds`, `policy_sync_last_success_timestamp{tenant}`, `notification_queue_depth{tenant}`, `db_enforced`; alert rules `control-plane/deploy/alerts.yaml` | `GET /metrics` (no auth, cluster-internal; a NetworkPolicy in M3-02) | — | metrics move on ingest; tenant label present and bounded by the tenant count; `promtool check rules` passes inside the Linux check container | M2-01 |
| M2-13 audit export | `audit/export.py`: a time-range export of the caller's tenant chain skeleton (`seq`, `prev`, `hash` of every record) plus full bodies only for the requested team's records, and the head at export time; JSONL or CSV | `GET /v1/audit/export?team&from&to&format` (streamed; `audit.read.team`/`.org`) | `agentic audit export` (M2-25/26 side) | the export verifies independently: skeleton links, each body hashes to its skeleton hash, other teams' bodies absent, no record of another tenant | M2-05, M2-26 |
| M2-14 service accounts + CSV import | `users.kind = service` in the tenant; role `service-account` (team scope); tokens bound to the tenant and require `expires_at` at most `service_token_max_days` (365, owner row 75); `rbac/importer.py` CSV `email,role,team,handle` with a dry-run diff, applied to the caller's tenant only | `POST /v1/service-accounts {name, team, days}` gives the token once; `POST /v1/rbac/import?dry_run=` | `agentic rbac import` (M2-25) | a service account cannot approve gates; its token never works in another tenant; the CSV import is idempotent and the dry run shows the diff | M2-03, M1-48 |
| M2-15 versioned rollout | mostly core definitions (`rollout:`, `pin`, `deprecated_after` in policy-repo agents, pipelines and skills); the control plane validates them in `validate_policy_tree` and serves them in the tenant's bundle | none new | preflight warns or blocks per date | a team not in `rollout:` keeps the old version; deprecated blocks after its date | M2-02 |
| M2-16 budget alerts | ingest watches `step_finished.cost` per run; team spend from TokenLean for the tenant's `gateway_tenant`; thresholds 80/100 % produce outbox rows with `dedupe_key = (scope, threshold, period)` in the tenant | — | — | one alert per threshold crossing per tenant | M2-07, M1-13 |
| M2-17 key rotation / revocation | `keys/` client for TokenLean `/admin/keys` (ST-9) in the tenant's `gateway_tenant`; `POST /v1/users/{email}/proxy-key/rotate`, revoke on offboard; audit `key.rotated` / `.revoked` | as listed | the CLI maps TokenLean's revoked-key 401 to "your gateway key was revoked or rotated - run `agentic login`"; takes DF-182's gateway-verify part | revoked key rejected within the TTL; actionable CLI message; a user of b cannot be rotated from a (404) | M1-48, M2-03, M2-34 |
| M2-18 provisioning | `invites(tenant_id, id, user_id, token_sha256 unique, expires_at, used_at)`; definer function `resolve_invite` (registered); a new user's first assignment creates an invite and an email (M2-07); offboarding disables the user, revokes control-plane tokens and the proxy key (M2-17), and sets a user-scope kill switch for running work | `POST /v1/invites/{token}/redeem` (single use, no bearer; the tenant comes from the invite token) gives the proxy key, a tenant-bound control-plane token and `control_plane_url`; `POST /v1/users/{email}/offboard` | `agentic login --invite <url>` redeems once and stores both secrets in the keyring (no copy-paste of keys; settles ND-13's URL question) | link single-use; the redeemed token works only in the invite's tenant; an offboarded user cannot run (401 at the control plane, kill switch, revoked gateway key) | M2-03, M2-07, M2-17, M2-34 |
| Usage views (M2-37 consumers) | the views slice 2 reads (`usage_by_team`, `spend_rate`) are filtered by the tenant's `gateway_tenant` inside the tenant's context; no view spans tenants | via M2-11/M2-16 | — | a's queries never return b's usage | M2-37 |

### What slice 1 gives the consuming rows (M2-19..M2-26)

| Row | Uses |
|---|---|
| M2-19 kill-switch checks (SP6) | `KillSwitchSource` from M2-06; the policy cache expiry from `fetched.json` (Task 10) for "pause when stale" |
| M2-20 control-plane sink (SP12) | `ControlPlaneSink` and `ControlPlaneClient.ingest` (Task 10); 409 `ingest.gap` carries the server head, so a lost or empty cursor (DF-179) re-delivers from `head + 1`; duplicates are no-ops (DF-167). It must not block the engine: `ForwardingRecorder.record` is synchronous and runs inside the async engine, so delivery needs a short timeout or a background thread (new DF). Personal-skill sync and init audit events (DF-185) need new endpoints (`PUT /v1/users/me/skills/{name}`, `POST /v1/audit/client-events`, chained by the control plane on insert into the caller's tenant chain) |
| M2-21 preflight org checks (SP6) | `OrgAuthorizer` and `/v1/rbac/check` (Task 6); run registration and caps (M2-08); kill state (M2-06); the pipeline integrity hash from the validated bundle (Task 7); a run with an unknown policy version is refused (ND-7). ND-8 hand-off: the gate `approve` record gains `required_role` and `role_held` (from `OrgAuthorizer.role_held`) in its `extra`, so every approval records approver and role; four-eyes is unchanged in `engine.decide` |
| M2-22..M2-24 (SP6) | none from the control plane |
| M2-25 admin commands (SP14) | `/v1/rbac/*` (Task 6), `/v1/tenant/*` (Task 7), `/v1/killswitch` (M2-06), pause (M2-10), CSV import (M2-14); tenant creation stays a server-side operator command |
| M2-26 `agentic audit verify` (SP14) | `GET /v1/audit` and `GET /v1/runs/{id}/events` return records exactly as hashed, for the caller's tenant; one verifier, `sdlc_commons.ledger.verify_entries` and `entry_hash`, checks both chains |

## Effort

Tracker estimates: M2-01 1.5, M2-02 1.5, M2-03 2, M2-04 1.5, M2-05 1 = 7.5 PW, plus the CLI work in Task 10 (about 1 PW, not in any row) = 8.5 PW for slice 1 before this revision.

Impact of the owner decisions, roughly:

| Change | Added |
|---|---|
| Tenancy foundation (Task 3: RLS helper, tenant context, runtime role, definer functions, enforcement probe, isolation harness) | +1.5 PW |
| Tenant-aware tables, per-table isolation tests and the per-family API isolation tests across Tasks 4-9 | +0.75 PW |
| Per-tenant policy sync (tenant policy source API, hook tokens, worker loop over tenants, per-tenant git config) | +0.5 PW |
| Tenant and first-admin bootstrap, operator commands | +0.25 PW |
| pycasbin instead of own checks (model, read-only adapter, revision cache, matrix through the enforcer); roughly neutral against the typed checks it replaces | +0.25 PW |
| **Slice 1 total** | **about 11.75 PW (+3.25, ~40 %)**: slice 1a (Tasks 1-6) about 6.5 PW, slice 1b (Tasks 7-11) about 5.25 PW |
| Slice 2: tenant scoping in every row (all-tenants operator kill switch, per-tenant notifications and sender, TokenLean tenant mapping for routing, keys, usage and budgets, tenant metrics label) | 10 PW -> about 11.25 PW (+1.25) |

SP15 overall: about 18.5 PW -> about 23 PW (+4.5 PW, ~25 %). Suggested tracker re-estimates (sum 11.75): M2-01 1.5 -> 3.25 (tenancy foundation, bootstrap), M2-02 1.5 -> 2.5 (per-tenant sync, plus 0.5 of Task 10's CLI work), M2-03 2 -> 3.0 (casbin and isolation, plus 0.5 of Task 10: DF-184/DF-203), M2-04 1.5 -> 1.75 (isolation), M2-05 1 -> 1.25 (per-tenant chain, isolation).

**Slice 1 is split:** slice 1a (Tasks 1-6: skeleton, database, tenancy/RLS, audit, auth, RBAC; ends with a full check and review at Task 6 Step 6) and slice 1b (Tasks 7-11: policy sync, repos and CODEOWNERS, ingest, CLI side, entry points; ends with Task 11's review). Both stay on one developer track; 1b starts after the 1a review is green.
