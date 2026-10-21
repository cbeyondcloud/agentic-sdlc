# SP15 Control Plane Implementation Plan (slice 1: M2-01..M2-05; slice 2: M2-06..M2-18 interfaces)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Org mode gets its server. A small FastAPI + Postgres service (`agentic-control-plane`, package `agentic_cp`) that:
- authenticates every caller with a revocable control-plane token (M2-01);
- keeps a hash-chained, per-tenant audit table that the running service can only append to (M2-05);
- holds roles, permissions and team-scoped assignments, derives repo maintainers from CODEOWNERS, and answers permission checks (M2-03);
- pulls the git policy repo, validates it with the same core code the CLI uses, versions it as `git:<commit>`, keeps the previous version when a new one is invalid, and serves the validated bundle (M2-02);
- ingests each run's Zyroflo event chain, verifying it entry by entry, idempotently (M2-04).

The CLI side of each piece ships with it: `agentic login --for control-plane` verifies the token, org-mode `init` fetches the policy bundle from the control plane, roles come from the control plane instead of the user config, and a `ControlPlaneSink` delivers ledger entries (wired into runs by M2-20).

**Architecture:**
- **Package `agentic_cp`** (`control-plane/src/agentic_cp/`, layout from spec §4.1):
  - `settings.py`, `app.py` (`create_app`), `errors.py` (RFC 9457 problem+json), `db.py` + `tables.py` (SQLAlchemy Core), `migrations/` (Alembic, shipped as package data), `__main__.py` (`serve`, `worker`, `migrate`, `admin`, `openapi`);
  - `auth/` (tokens, principal dependency, bootstrap), `audit/` (chain, insert-only role, enforcement status), `rbac/` (roles, assignments, CODEOWNERS maintainers, check), `policy_sync/` (git fetch, validate, versions, bundle, webhook), `ingest/` (chain verification, run store).
- **Shared wire contract in core: `agentic.orgapi`** (pydantic models and the permission matcher only; no HTTP, no server libraries). The control plane and the CLI both import it, so the contract has one definition. Core never imports `agentic_cp`; the CLI never imports `agentic_cp`.
- **Chain code is not rewritten.** `sdlc_commons.ledger` gains `entry_hash` (Zyroflo's `approve.entry_hash`) and `verify_entries` (Zyroflo's chain rules over entries in memory). Ingest verifies with it; the audit table hashes with the same formula, so one verifier (M2-26) checks both chains.
- **Processes:** `serve` (stateless API, 2+ replicas behind a Service, spec §25) and `worker` (policy sync loop; one active replica through a Postgres advisory lock). Tables live in the Postgres schema `agentic`, so the service can share TokenLean's database server (spec §25 topology).
- **Two database roles** (TokenLean `src/proxy/audit/enforcement.py` pattern, re-implemented): the owner role runs migrations and grants; the runtime role the API uses may only `SELECT, INSERT` on the evidence tables `audit_log`, `run_events` and `policy_versions`.

**Tech Stack:** Python 3.12; FastAPI 0.142.4, Starlette 1.7.0, uvicorn 0.54.0 (no `[standard]` extra); SQLAlchemy 2.0.54 (Core, asyncio) with asyncpg 0.31.0 and greenlet 3.5.6; Alembic 1.20.0; codeowners 0.9.0; git CLI; httpx2 (CLI client, D36); pytest with the anyio plugin; Postgres 17 in a container (docker or podman, D40). Details and sources below.

**Spec:** `docs/superpowers/specs/2026-10-16-agentic-sdlc-design.md` §3 (modes, responsibility split), §4.1 (layout), §12.1 (policy as code), §12.3 + §23 (RBAC, permission matrix, ownership D30), §13 (identity), §14.5 (audit record fields, two chains), §25 (deployment), decisions D5, D12, D14, D16, D17, D18, D19, D33, D40, D43. Master plan rows M2-01..M2-18 and the "Deployment landscape" and "Users and roles" sections.

**Prerequisite:** start after batch 4 (acceptance M1-68..71 and the M1-81 hardening tracks) has merged into `m1-foundation`. Task 9 edits `cli/.../commands/login.py`, `policy_source.py`, `workspace.py`, `commands/init.py`, `repo_guard.py` and `publishing.py`, which the CLI hardening track may also touch. Branch: `track/sp15-control-plane`.

## OSS reuse decisions

Checked 2026-10-21 against the PyPI JSON API (`https://pypi.org/pypi/<name>/json`, release upload dates) and each project's licence metadata. Every pin was released on or before 2026-10-07.

| Need | Reused (licence, version, released) | Rejected, why |
|---|---|---|
| HTTP API, OpenAPI | FastAPI (MIT) **0.142.4**, 2026-10-07; Starlette (BSD-3) **1.7.0**, 2026-09-23, pinned explicitly | FastAPI 0.143+ (after the cutoff). Starlette 1.9.0 is in `uv.lock` today through mcp (code-review-graph extra) but was released 2026-10-18; pinning 1.7.0 satisfies mcp (`>=0.27`), sse-starlette (`>=0.49.1`) and fastmcp (`>=1.0.1`). Fallback if the strict 14x24 h rule is applied at lock time: FastAPI 0.142.2 (2026-09-30) |
| ASGI server | uvicorn (BSD-3) **0.54.0**, 2026-09-25, plain (h11, pure Python) | `uvicorn[standard]`: uvloop has no Windows build and httptools is native; neither is needed at this scale |
| SQL toolkit | SQLAlchemy (MIT) **2.0.54**, 2026-09-15, Core only (tables and expressions, no ORM) | SQLAlchemy 2.1.x: 2.1.0 went GA 2026-09-24 and had four patch releases in two weeks; revisit at M3. ORM: append-only evidence tables and explicit grants read better as Core |
| Migrations | Alembic (MIT) **1.20.0**, 2026-09-11 (pulls Mako 1.4.3 MIT, MarkupSafe BSD-3) | — |
| Postgres driver | asyncpg (Apache-2.0) **0.31.0**, 2025-11-24; greenlet (MIT AND PSF-2.0, allowed by `licence_check.py`) **3.5.6**, 2026-09-14, via `sqlalchemy[asyncio]` | psycopg 3.3.6 (LGPL-3.0-only) and psycopg2 (LGPL): outside the allow-list. pg8000 1.31.5 (BSD-3, pure Python, 2025-09-14) is the fallback if native wheels are refused (ND-9). asyncpg 0.32.0 (2026-10-06) is acceptable once older |
| Control-plane tokens | stdlib `secrets` + `hashlib` (opaque bearer tokens, sha256 at rest) | PyJWT 2.15.1 (MIT): JWTs cannot be revoked instantly without a deny list; JWT/OIDC acceptance comes with SSO (phase 3) |
| RBAC | Own typed checks (spec §1.2: no general policy engine in v1), behind a small protocol | pycasbin (Apache-2.0) 2.8.0, 2026-02-02: workable (RBAC with domains), but its generic `casbin_rule` table sits outside our hash-chained audit, its DSL is a second policy language to review, and multi-replica consistency needs a watcher; kept as the named fallback (ND-1). oso 0.27.3 (2024-01): the vendor's PyPI page says "We have deprecated the legacy Oso open source library" |
| CODEOWNERS parsing | codeowners (MIT) **0.9.0**, 2026-06-28, no dependencies (sbdchd/codeowners, follows hmarr's Go library) | Own parser: last-match-wins and glob anchoring are easy to get subtly wrong |
| Audit chain + insert-only role | Own code; pattern from TokenLean `src/proxy/audit/enforcement.py` (Apache-2.0, same owner): restricted LOGIN role, `SELECT, INSERT` only on audit tables, an `enforcement_status` probe. Hash formula: Zyroflo `approve.entry_hash` through `sdlc_commons` | Copying TokenLean's module: it is asyncpg-specific and TokenLean's audit rows are not chained; the pattern is re-implemented and attributed |
| Policy repo fetch | git CLI (already required by the CLI's policy clone; run as a separate process) | dulwich 1.2.17 (Apache-2.0 OR GPL-2.0-or-later, 2026-10-03): a second git implementation whose proxy, CA bundle and credential-helper behaviour differs from git's on corporate networks (spec §15) |
| Background jobs | None: `worker` process with asyncio loops; leader by `pg_try_advisory_lock` | APScheduler 3.11.3 (MIT): one periodic job does not need a scheduler; retention and alert jobs become K8s CronJobs (spec §25) |
| Settings | Own pydantic model read from `AGENTIC_CP_*` variables (unknown `AGENTIC_CP_*` names are refused) | pydantic-settings: the locked 2.16.0 was released 2026-10-14 (breaks the age rule) and silently ignores typos by default |
| SMTP (slice 2, M2-07) | aiosmtplib (MIT) **5.1.3**, 2026-09-08; tests capture with aiosmtpd (Apache-2.0) **1.4.6**, 2024-05-18 | — |
| Metrics (slice 2, M2-12) | prometheus-client (Apache-2.0 AND BSD-2-Clause) **0.26.0**, 2026-07-24; `promtool` only inside the Linux check container | — |
| Test Postgres | Own session fixture that starts `postgres:17` through the docker/podman CLI (`scripts/check.py`'s `container_engine()`, D40), or uses `AGENTIC_CP_TEST_DSN` | testcontainers 4.15.0 (Apache-2.0): talks to the Docker Engine API (podman needs its socket service) and starts a Ryuk reaper container. pytest-postgresql 9.1.1: LGPL-3.0-or-later and needs local Postgres binaries (unsigned on Windows laptops). testing.postgresql 1.3.0: unmaintained since 2016 |
| Async tests | anyio's pytest plugin (anyio 4.15.1, MIT, already locked); Starlette's TestClient over httpx 0.28.1 (BSD-3, already locked) | pytest-asyncio 1.4.0: one more dependency for what anyio already does |
| Postgres server | `postgres:17` image (PostgreSQL licence), pinned by digest of a release at least two weeks old; supported range 15+ (ND-12) | — |

## Needs decision (owner)

Each item names the recommended default the plan builds unless the owner says otherwise. The coordinator records them in DEFERRED.md as `Needs decision` against the named row.

| ID | Question | Recommended default (built by this plan) | Row |
|---|---|---|---|
| ND-1 | RBAC engine: own typed checks or pycasbin? | Own typed checks behind a small interface (spec §1.2 rules out a general engine in v1; every role change must land in our hash-chained audit); pycasbin is the documented fallback, Cedar later (E3) | M2-03 |
| ND-2 | How do CLIs get the org policy in org mode: from the control plane, or by cloning the policy repo themselves? | From the control plane: the validated `git:<commit>` bundle, digest-checked. Developers need no read access to the policy repo, and a commit the control plane refused never reaches a laptop. Cloning stays for orgs without a control plane and for standalone mode | M2-02 |
| ND-3 | Control-plane token form and lifetime | Opaque bearer tokens (`acp_` + 32 random bytes), stored as sha256, revocable at once; user tokens expire after 90 days (renewed with `agentic login`), service-account tokens at most 365 days (M2-14) | M2-01 |
| ND-4 | How the first org admin is created | A one-off server-side command, `python -m agentic_cp admin bootstrap --email <admin>` (run via `kubectl exec` or `compose run`), which refuses once any org admin exists and prints the token once | M2-01 |
| ND-5 | Fail closed when the database does not enforce insert-only audit | Yes: `/readyz` answers 503 (`audit.unenforced`) and `serve` refuses to start; `AGENTIC_CP_REQUIRE_AUDIT_ENFORCEMENT=false` only for local development | M2-05 |
| ND-6 | Where repo maintainers come from, and how a CODEOWNERS `@handle` maps to a person | The control plane fetches CODEOWNERS itself over git from the repo's default branch (provider-neutral, never trusts a client copy). Handles are host-qualified (`github.com:alice`) and set only by admins or the CSV import (M2-14), never self-declared. `@org/team` entries are reported as unresolved until host team sync (phase 3 IdP/group sync) | M2-03 |
| ND-7 | An ingested run whose `policy_version` the control plane never accepted (a locally edited policy) | Accept the events (evidence is never dropped), mark the run `policy_known = false` and write an `ingest.unknown_policy_version` audit record; M2-21 refuses such runs before they start | M2-04 |
| ND-8 | Does an org admin satisfy any gate's `approver_role`? | Yes (spec §23 matrix: org admin approves gates), provided they hold `gate.approve`; the approval records the role actually held | M2-03 / M2-21 |
| ND-9 | Server-side native wheels (asyncpg, greenlet, SQLAlchemy's compiled extension) installed on framework developers' laptops through the workspace dev group | Accept: the control plane never ships to the 1000 user laptops (an import contract keeps server libraries out of the CLI), and core already uses native wheels (pydantic-core, regex). Fallback: pg8000 (pure Python, sync) | M2-01 |
| ND-10 | Tenancy | One tenant per deployment (`AGENTIC_CP_TENANT`, default `default`); the `tenant` column is kept on every table so multi-tenant is a later change, not a migration of evidence | M2-01 |
| ND-11 | Run links in PR bodies in org mode (DF-291) | `<control_plane_url>/v1/runs/<run_id>` (the JSON run summary) until a web UI exists (phase 3); handoff links stay repo-relative, because handoff bodies are not uploaded | M2-04 |
| ND-12 | Supported Postgres versions | 15 and later (managed services all offer 15-17); CI tests on 17 | M2-01 |
| ND-13 | Should `agentic login --for control-plane --url X` write `control_plane_url` into `~/.agentic/config.yaml`? | No for now (writing user config is DF-274, still open): the URL comes from the user config or `AGENTIC_CONTROL_PLANE_URL`, which the invite (M2-18) and MDM set; `--url` only overrides for that command | M2-01 / M2-18 |

## Global Constraints

- **Wire contract.** Every request and response body is a model in `agentic.orgapi`. Paths are under `/v1`. Errors are `application/problem+json` with `type`, `title`, `status`, `detail`, a stable machine `code` (`auth.invalid_token`, `ingest.chain_broken`, ...) and an optional `hint` the CLI shows as is. An error never echoes a submitted secret, token or DSN.
- **Headers.** The CLI sends `Authorization: Bearer <token>`, `User-Agent: agentic-cli/<version>` and `X-Agentic-CLI-Version: <version>` on every call (M2-08 reads the last one).
- **Tokens.** Only `sha256(token)` is stored; a token value appears once, in the issuing response, and never in logs, audit records, problems or `repr`s. Unknown, malformed and revoked tokens get the same 401 detail (no oracle); an expired token says so and hints `agentic login --for control-plane`.
- **Audit records** carry the spec §14.5 fields: `seq`, `tenant`, `team`, `actor`, `actor_kind` (`human|service|system`), `event`, `target`, `before`, `after`, `reason`, `run_id`, `trace_id`, `policy_version`, `at`, `prev`, `hash`. `hash = sdlc_commons.ledger.entry_hash(record minus hash)`, the master plan's ledger formula. `at` is stored as the exact ISO-8601 UTC text that was hashed (milliseconds, `Z`). `before`/`after` hold JSON without floats (money as decimal strings), so a jsonb round trip re-hashes identically. One chain per tenant; appends serialise on `pg_advisory_xact_lock(hashtextextended('agentic.audit:' || tenant, 0))` and `UNIQUE (tenant, seq)` backs it up.
- **Evidence tables are insert-only for the runtime role:** `audit_log`, `run_events`, `policy_versions` (the active pointer lives in the updatable `policy_active`). `migrate` re-applies the grants after every upgrade.
- **Policy versions** are `git:<40-hex commit>`, the pattern the run ledger accepts (`sdlc_commons.ledger.FIELDS["policy_version"]`). git runs with `-c core.autocrlf=false -c core.eol=lf -c core.fsmonitor=false`, `GIT_TERMINAL_PROMPT=0`, and its error text goes through `agentic.hosting.base.scrub_url_credentials` before it is logged or returned.
- **Bundles** are `git archive --format=tar <commit>` gzipped with `mtime=0`; `digest = "sha256:" + sha256(bundle)`. `tree_digest` is `agentic.policy.tree_digest` over the extracted files: length-prefixed path and bytes, sorted by POSIX path (the DF-140 lesson). The CLI extracts with `tarfile` `filter="data"`.
- **Import contracts (D43), added to `pyproject.toml`:**
  - "The CLI never imports the control plane": `agentic_cli` must not import `agentic_cp`.
  - "Server libraries only in the control plane": `agentic`, `sdlc_commons` and `agentic_cli` must not import `fastapi`, `starlette`, `uvicorn`, `sqlalchemy`, `alembic`, `asyncpg` or `codeowners`.
  - The existing rules still apply: Zyroflo only through `sdlc_commons`; provider SDKs only in `agentic.transport`; core imports nothing above it.
- **Provider-agnostic.** No model or vendor names in `agentic_cp` or `agentic.orgapi`. Git hosting is reached through git only; host names appear only as data (handles, remotes).
- **Windows and macOS.** Everything except the Postgres container runs natively on both. Postgres runs in a container through `CONTAINER_ENGINE` (docker or podman). Without an engine and without `AGENTIC_CP_TEST_DSN`, database tests skip with a reason that names both; with `AGENTIC_CP_REQUIRE_PG=1` (set by `check.py --linux` and CI) a missing database fails instead.
- **Pins.** Exact `==` pins for the new direct dependencies; after `uv lock`, check that every newly added package's `upload-time` in `uv.lock` is on or before 2026-10-07 (DF-95 still has no automatic check).
- **Formatting, CI and records:**
  - never add `# fmt: skip` or `# fmt: off`; line length 100;
  - check pytest's exit code directly;
  - `uv run python scripts/check.py --all` must be green on Windows and Linux;
  - TRACKER.md and DEFERRED.md are updated by the coordinator from this track's output.

## Review Focus

1. **Ingest takes a chain exactly as written, or nothing.** An edited, inserted, removed or reordered entry, a fork (same `seq`, different `hash`), a gap, an entry claiming another run, or an identity that changes mid-run is refused, and no row of that request is stored. A replay is a no-op; an overlapping replay stores only the new tail. Tests: Task 8 `test_broken_chain_rejected_and_nothing_stored`, `test_fork_is_rejected`, `test_gap_is_409_with_head`, `test_duplicate_replay_is_a_noop`, `test_overlapping_replay_accepts_only_new`, `test_concurrent_batches_for_one_run_serialise`.
2. **The running service cannot rewrite evidence.** The runtime role gets an insufficient-privilege error on `UPDATE`, `DELETE` and `TRUNCATE` of `audit_log`, `run_events` and `policy_versions`, and the service fails closed when that is not true. The audit chain stays one contiguous chain under concurrent writers. Tests: Task 3 `test_runtime_role_cannot_update_delete_or_truncate_evidence`, `test_readyz_fails_closed_when_audit_unenforced`, `test_concurrent_appends_keep_one_chain`, `test_verify_reports_an_edited_record_by_seq`.
3. **No invalid policy ever becomes active, and the version is the same everywhere.** A broken policy commit is rejected with its problems, the previous version stays active, and the version id the CLI records equals the commit the control plane validated, on Windows and Linux alike. Tests: Task 7 `test_invalid_policy_rejected_and_previous_kept`, `test_bundle_digest_matches_and_is_deterministic`; Task 9 `test_workspace_policy_uses_recorded_version`, `test_locally_edited_cached_policy_is_refused`.
4. **RBAC matches spec §23 cell by cell.** A team lead cannot reach another team or assign beyond Developer/Approver. The last org admin cannot be removed or stripped of `rbac.assign`. A service account cannot approve. Self-asserted roles in `~/.agentic/config.yaml` never authorise anything in org mode. Tests: Task 5 `test_permission_matrix_cell[...]`, `test_team_lead_assigns_only_developer_or_approver_in_own_team`, `test_last_org_admin_cannot_be_removed`; Task 9 `test_org_init_takes_roles_from_control_plane`.
5. **Secrets stay secret.** Tokens are never stored or logged in clear. Git credentials in a remote URL never appear in a problem, a log line or an audit record. The DSN never appears in `repr(Settings)`. Tests: Task 4 `test_token_is_stored_hashed_and_never_logged`; Task 6 `test_git_errors_never_show_credentials`; Task 1 `test_settings_repr_hides_secrets`.

## Deferred items pulled in

| Item | Handling here |
|---|---|
| DF-182 (verify the gateway key at login, register with the control plane, check the minimum CLI version) | Control-plane half done in Task 9 (`login --for control-plane` verifies through `/v1/me` before storing). Minimum CLI version is M2-08 (slice 2). Gateway-key verification needs a TokenLean key introspection call: retarget to M2-17 |
| DF-184 (`agent.edit.repo` for `init --repo` in org mode) | Task 9: check 7 asks the control plane |
| DF-203 (roles from `~/.agentic/config.yaml` are self-asserted) | Task 9: org mode with a control plane takes roles from `/v1/me`; the config's `roles` is ignored there (warned once). Standalone keeps it, documented as "chooses skills only, never authorises" |
| DF-288 (git host settings in org policy) | Not in this slice: it is an `OrgPolicy` field plus CLI hosting wiring. Retarget to M2-51 (policy-repo template), which the bundle from M2-02 then distributes |
| DF-291 (control-plane run URL shape) | Task 8 adds `GET /v1/runs/{run_id}`; Task 9 changes the PR links per ND-11 |
| DF-02 (policy_version CRLF/LF hash) | Org mode no longer depends on it once the CLI records the control plane's `git:` version (Task 9); stays open at M1-81 for standalone |
| DF-95 (two-week rule for transitive packages) | Instance fixed: Starlette pinned to 1.7.0 (Task 1). The general check stays M1-81 |

---

### Task 1: Package skeleton, settings, app factory, problem+json, OpenAPI snapshot (M2-01)

**Files:**
- Modify: `control-plane/pyproject.toml` (dependencies: `agentic-core`, `sdlc-commons`, `fastapi==0.142.4`, `starlette==1.7.0`, `uvicorn==0.54.0`, `sqlalchemy[asyncio]==2.0.54`, `asyncpg==0.31.0`, `alembic==1.20.0`, `codeowners==0.9.0`; `[tool.uv.build-backend]` includes `migrations/` and `alembic.ini` as package data); root `pyproject.toml` (the two import contracts above); `uv lock`.
- Create: `control-plane/src/agentic_cp/{settings.py, app.py, errors.py, __main__.py}`, `core/src/agentic/orgapi/{__init__.py, models.py}`, `control-plane/openapi.json` (generated).
- Test: `control-plane/tests/test_settings.py`, `control-plane/tests/test_app.py`, `control-plane/tests/conftest.py`.

**Interfaces:**
- `agentic.orgapi`: `API_PREFIX = "/v1"`, `TOKEN_PREFIX = "acp_"`, `CLI_VERSION_HEADER = "X-Agentic-CLI-Version"`, `class Problem(BaseModel)` (`type`, `title`, `status`, `detail`, `code`, `hint: str | None`).
- `class SettingsError(Exception)`.
- `Settings` (frozen pydantic model): `database_url: SecretStr`, `owner_database_url: SecretStr | None`, `db_schema = "agentic"`, `tenant = "default"` (slug), `policy_repo: str | None`, `policy_branch = "main"`, `policy_poll_seconds = 60` (at least 10), `policy_workdir: Path`, `webhook_secret: SecretStr | None`, `require_audit_enforcement = True`, `max_ingest_entries = 500`, `max_body_bytes = 1_048_576`, `user_token_days = 90`, `service_token_max_days = 365`.
- `Settings.from_env(env: Mapping[str, str]) -> Settings`: reads `AGENTIC_CP_<FIELD>`; an unknown `AGENTIC_CP_*` name raises `SettingsError` naming it.
- `create_app(settings, *, engine=None, clock=None, git=None) -> FastAPI`: lifespan opens and closes the engine; installs the problem handlers (HTTPException, RequestValidationError, unhandled exceptions as a 500 with `code="internal"` and no traceback in the body); adds `GET /healthz` (no database access).
- `python -m agentic_cp openapi [--write | --check]` writes or compares `control-plane/openapi.json` (sorted keys, LF).

- [ ] **Step 1: Write the failing tests**:
  - `test_settings_from_env_reads_prefixed_variables`: `Settings.from_env({... "AGENTIC_CP_TENANT": "acme"}).tenant == "acme"`.
  - `test_settings_refuse_unknown_prefixed_variable`: `AGENTIC_CP_TENNANT` raises `SettingsError` whose message contains `AGENTIC_CP_TENNANT`.
  - `test_settings_refuse_bad_values`: poll seconds `5` and tenant `"Acme Corp"` are each refused, naming the field.
  - `test_settings_repr_hides_secrets` (RF5): a DSN password is not in `repr(settings)` or `str(settings)`.
  - `test_healthz_is_ok_without_database`: an app whose engine would fail on connect still answers `GET /healthz` with 200 `{"status": "ok"}`.
  - `test_unknown_route_is_problem_json`: 404, content type `application/problem+json`, `code == "not_found"`.
  - `test_validation_error_is_problem_json_with_field_names`: 422, `detail` names the field, `code == "invalid_request"`.
  - `test_unhandled_error_is_500_without_traceback`: the body has `code == "internal"` and no `Traceback`.
  - `test_openapi_snapshot_is_current`: `app.openapi()` equals the committed `openapi.json`; the failure message says `python -m agentic_cp openapi --write`.
- [ ] **Step 2: Run** `uv run pytest control-plane/tests/test_settings.py control-plane/tests/test_app.py -q`. Expected: FAIL (no module).
- [ ] **Step 3: Implement.** Pin the dependencies, run `uv lock`, check the new `upload-time` values, run `uv run lint-imports` and `uv run python scripts/licence_check.py`.
- [ ] **Step 4: Run.** Expected: PASS; lint-imports and the licence check green.
- [ ] **Step 5: Commit.** `feat(control-plane): skeleton - settings, app factory, problem+json, OpenAPI snapshot (M2-01)`

### Task 2: Database, migrations, owner and runtime roles, test Postgres (M2-01)

**Files:**
- Create: `control-plane/src/agentic_cp/{db.py, tables.py}`, `control-plane/src/agentic_cp/migrations/{env.py, script.py.mako, versions/0001_schema_users_tokens.py}`, `control-plane/src/agentic_cp/alembic.ini`, `control-plane/tests/pg.py` (fixture helpers).
- Modify: `scripts/check.py` (the `--linux` stage starts a Postgres sidecar), `control-plane/tests/conftest.py`.
- Test: `control-plane/tests/test_migrations.py`, `control-plane/tests/test_pg_fixture.py`, `scripts/tests/test_check_postgres.py`.

**Interfaces:**
- `tables.metadata` (schema from settings); `0001` creates the schema plus:
  - `users(id uuid pk, tenant, email (stored lower-case, unique per tenant), display_name, kind human|service, handles text[], status active|disabled, created_at)`;
  - `tokens(id uuid pk, tenant, user_id fk, name, token_sha256 unique, created_at, created_by, expires_at, revoked_at, last_used_at)`.
- `migrate(owner_dsn, schema) -> str` runs `alembic upgrade head` programmatically with an async env and returns the head revision. `python -m agentic_cp migrate` uses `AGENTIC_CP_OWNER_DATABASE_URL`.
- `make_engine(settings) -> AsyncEngine` (asyncpg; `search_path` set to the schema; pool pre-ping).
- Test fixtures (session scope):
  - `pg_server`: `AGENTIC_CP_TEST_DSN`, else `<engine> run -d --rm -p 127.0.0.1::5432 -e POSTGRES_PASSWORD=... postgres:17@sha256:<digest>`, waiting for `pg_isready` inside the container;
  - `pg_template` migrates a template database once and creates the roles `cp_owner` and `cp_app`;
  - function scope: `db` is `CREATE DATABASE t_<uuid> TEMPLATE ...` (about 100 ms) with an owner engine and an app engine.
- `check.py --linux`:
  - `<engine> network create agentic-check-<pid>`, then start the pinned `postgres:17` on it;
  - run the check container on the same network with `AGENTIC_CP_TEST_DSN=postgresql://...@pg:5432/postgres` and `AGENTIC_CP_REQUIRE_PG=1`;
  - remove both in a `finally`.

- [ ] **Step 1: Failing tests**:
  - `test_upgrade_head_on_empty_database`: `migrate()` returns the head; `users` and `tokens` exist in schema `agentic`.
  - `test_migrate_is_idempotent`: a second `migrate()` changes nothing.
  - `test_migrations_match_metadata`: Alembic `compare_metadata(...) == []`.
  - `test_downgrade_to_base_and_upgrade_again`: development safety; succeeds.
  - `test_missing_database_skips_with_reason`: with no engine and no DSN (monkeypatched `which`), the skip reason names `CONTAINER_ENGINE` and `AGENTIC_CP_TEST_DSN`.
  - `test_missing_database_fails_when_required`: with `AGENTIC_CP_REQUIRE_PG=1`, the same case fails instead.
  - `test_linux_stage_starts_postgres_sidecar_on_a_private_network`: a fake runner shows network create, postgres run with `--network`, the check container with both variables, then cleanup even when the checks fail.
- [ ] **Step 2: Run** `uv run pytest control-plane/tests/test_migrations.py control-plane/tests/test_pg_fixture.py scripts/tests/test_check_postgres.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement.** Pin the image by digest (a `postgres:17.x` release at least two weeks old) in one constant shared by `pg.py` and `check.py`.
- [ ] **Step 4: Run.** Expected: PASS, natively with Rancher Desktop or Podman, and in `check.py --linux`.
- [ ] **Step 5: Commit.** `feat(control-plane): Postgres schema, Alembic migrations, test database fixture (M2-01)`

### Task 3: Hash-chained audit table and insert-only runtime role (M2-05)

**Files:**
- Create: `control-plane/src/agentic_cp/audit/{__init__.py, chain.py, enforcement.py}`, `migrations/versions/0002_audit_log.py`.
- Modify: `commons/src/sdlc_commons/ledger.py` (add `entry_hash`), `control-plane/src/agentic_cp/app.py` (`GET /readyz`), `__main__.py` (`db grant-runtime`).
- Test: `control-plane/tests/test_audit_chain.py`, `control-plane/tests/test_audit_enforcement.py`, `commons/tests/test_ledger_entry_hash.py`.

**Interfaces:**
- `sdlc_commons.ledger.entry_hash(entry: Mapping[str, object]) -> str`: returns Zyroflo's `approve.entry_hash`.
- `audit_log(tenant, seq bigint, at text, actor, actor_kind, event, team, target, before jsonb, after jsonb, reason, run_id, trace_id, policy_version, prev, hash; pk (tenant, seq))`.
- `class AuditError(Exception)`.
- `@dataclass(frozen=True) AuditEvent(event, actor, actor_kind, target="", team=None, before=None, after=None, reason="", run_id=None, trace_id=None, policy_version=None)`; its validator refuses floats anywhere in `before`/`after`.
- `async append(conn, tenant, event: AuditEvent, *, clock) -> AuditRecord`: takes the tenant's advisory lock, reads the head, then inserts.
- `async verify(conn, tenant, *, from_seq=1) -> list[str]`: recomputes every hash and link and names the `seq` of each problem.
- `INSERT_ONLY = ("audit_log", "run_events", "policy_versions")`; `grant_runtime` restricts those that exist, so re-running it after Tasks 7 and 8 (as `migrate` does) covers the later tables.
- `async grant_runtime(owner_conn, role, password, schema)`, idempotent:
  - the role is LOGIN, NOCREATEROLE and a member of no role;
  - full DML on the other tables, `SELECT, INSERT` on `INSERT_ONLY`;
  - default privileges for later tables;
  - the password is sent as a SCRAM verifier, never in clear.
- `async enforcement_status(conn) -> EnforcementStatus(enforced: bool, reasons: list[str])`: fail closed; a check that is missing or NULL counts against.
- `GET /readyz`: database reachable, migrations at head, audit enforcement (ND-5). 200 `{"status": "ready", "audit_enforced": true}`, else 503 problem `audit.unenforced` / `db.unavailable` / `db.migrations_pending`.

- [ ] **Step 1: Failing tests**:
  - `test_entry_hash_is_zyroflos`: equals the `hash` a `RunLedger` writes for the same entry.
  - `test_append_chains_records_per_tenant`: seqs 1..3, `prev` links, `hash == entry_hash(record minus hash)`; another tenant starts at 1.
  - `test_record_hash_matches_ledger_formula`: an independent `hashlib` recompute per the master plan formula agrees.
  - `test_record_values_refuse_floats`: `before={"x": 1.5}` raises `AuditError`.
  - `test_verify_reports_an_edited_record_by_seq` (RF2): an owner-role UPDATE of seq 2's `reason` makes `verify` name seq 2.
  - `test_verify_reports_a_deleted_record`: deleting seq 2 makes `verify` name seq 3 (broken link and gap).
  - `test_concurrent_appends_keep_one_chain` (RF2): 2 tasks x 50 appends give seqs 1..100 and `verify == []`.
  - `test_runtime_role_cannot_update_delete_or_truncate_evidence` (RF2): for each existing table in `INSERT_ONLY` (here `audit_log`), each of UPDATE, DELETE and TRUNCATE as `cp_app` raises asyncpg `InsufficientPrivilegeError`. The test is parametrised over `INSERT_ONLY`, so it covers `policy_versions` and `run_events` once Tasks 7 and 8 create them.
  - `test_runtime_role_can_insert_and_select_audit`.
  - `test_enforcement_status_reports_each_blocker`: a role granted UPDATE on `audit_log` gets `enforced is False` with reason "may change audit rows directly".
  - `test_readyz_fails_closed_when_audit_unenforced`: an app on the owner DSN with `require_audit_enforcement=True` answers 503 `audit.unenforced`; with `False` it answers 200 with `audit_enforced: false`.
  - `test_grant_runtime_is_idempotent_and_never_logs_password`: run twice; the statement log (caplog) holds no password.
- [ ] **Step 2: Run** `uv run pytest control-plane/tests/test_audit_chain.py control-plane/tests/test_audit_enforcement.py commons/tests/test_ledger_entry_hash.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement.** The module docstring credits the TokenLean pattern.
- [ ] **Step 4: Run.** Expected: PASS.
- [ ] **Step 5: Commit.** `feat(control-plane): hash-chained audit table and insert-only runtime role (M2-05)`

### Task 4: Token authentication, users, bootstrap, `/v1/me` (M2-01)

**Files:**
- Create: `control-plane/src/agentic_cp/auth/{__init__.py, tokens.py, deps.py, api.py, bootstrap.py}`, `control-plane/src/agentic_cp/rbac/defaults.py`, `migrations/versions/0003_rbac_tables.py` (tables `roles` and `assignments`, seeded from `DEFAULT_ROLES`; Task 5 adds the API).
- Modify: `core/src/agentic/orgapi/models.py` (`Me`, `TokenIssued`, `TokenInfo`), `__main__.py` (`admin bootstrap`, `admin issue-token`).
- Test: `control-plane/tests/test_auth.py`, `control-plane/tests/test_bootstrap.py`.

**Interfaces:**
- `new_token() -> tuple[str, str]`: the value (`acp_` + `secrets.token_urlsafe(32)`) and its sha256.
- `@dataclass(frozen=True) Principal(user_id, email, kind, tenant, token_id)`.
- `current_principal` FastAPI dependency (HTTPBearer):
  - 401 problems `auth.missing_token` / `auth.invalid_token` / `auth.token_expired` with `WWW-Authenticate: Bearer`;
  - disabled users count as invalid;
  - `last_used_at` is updated at most once a minute.
- `GET /v1/me -> Me`: user, kind, tenant, `token_expires_at`. Task 5 adds `assignments` and `permissions`.
- `POST /v1/tokens` (body: `name`, `revoke_current: bool = False`): issues a token for the caller (rotation); returns `TokenIssued` (the value, once).
- `GET /v1/tokens` lists the caller's tokens (no values). `DELETE /v1/tokens/{id}` revokes the caller's own token (other users' tokens: Task 5, `rbac.assign`).
- `python -m agentic_cp admin bootstrap --email E [--new-token]`:
  - with no org admin yet: creates the user, records the org-scope `org-admin` assignment (role seeded by `0003`) and prints the token once;
  - when one exists, refuses with exit 2 unless `--new-token` is given for that same admin;
  - audits `rbac.bootstrap` with actor `system:bootstrap`.
- Audit events: `token.issued`, `token.revoked` (never the value).

- [ ] **Step 1: Failing tests**:
  - `test_missing_token_is_401_problem`: `WWW-Authenticate: Bearer`, `code == "auth.missing_token"`.
  - `test_unknown_malformed_and_revoked_tokens_look_the_same`: identical status, code and detail.
  - `test_expired_token_is_rejected_with_login_hint`: clock past `expires_at` gives `code == "auth.token_expired"`, and `hint` contains `agentic login --for control-plane`.
  - `test_disabled_user_tokens_are_rejected`.
  - `test_me_returns_the_principal`.
  - `test_user_can_rotate_own_token`: the new token works; with `revoke_current`, the old one gets 401.
  - `test_token_is_stored_hashed_and_never_logged` (RF5): no table column and no caplog record contains the value.
  - `test_token_events_are_audited`: `token.issued` and `token.revoked` records exist; neither contains the value.
  - `test_bootstrap_creates_first_org_admin_and_prints_token_once`.
  - `test_bootstrap_refuses_when_an_org_admin_exists`: exit 2; the message names `--new-token`.
- [ ] **Step 2: Run** `uv run pytest control-plane/tests/test_auth.py control-plane/tests/test_bootstrap.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run.** Expected: PASS.
- [ ] **Step 5: Commit.** `feat(control-plane): token auth, users, first-admin bootstrap, /v1/me (M2-01)`

### Task 5: RBAC core: permissions, default roles, assignments, check API (M2-03)

**Files:**
- Create: `core/src/agentic/orgapi/rbac.py`, `control-plane/src/agentic_cp/rbac/{__init__.py, store.py, api.py}`, `control-plane/src/agentic_cp/audit/api.py`.
- Modify: `control-plane/src/agentic_cp/rbac/defaults.py` (role contents final, per the §23 matrix).
- Test: `core/tests/orgapi/test_rbac.py`, `control-plane/tests/test_rbac_api.py`, `control-plane/tests/test_audit_api.py`.

**Interfaces:**
- **Core `agentic.orgapi.rbac`** (pure):
  - `PERMISSIONS: dict[str, Literal["org", "team", "repo"]]`: the spec §12.3 set plus `agent.edit.repo` (CLI check 7), `rbac.assign.team`, `dashboard.view.repo`, `audit.read.own`, `audit.read.repo`. The value is where a grant counts: `org` only from an org-scope assignment; `team` from org scope or that team; `repo` from org scope, the repo's team, or a derived maintainership of that repo.
  - `ScopeRef(kind: "org"|"team"|"repo", value: str | None)`.
  - `EffectivePermissions(org: frozenset[str], teams: dict[str, frozenset[str]], repos: dict[str, frozenset[str]], repo_teams: dict[str, str])`.
  - `allows(perms, permission, *, team=None, repo=None) -> bool`.
  - `holds_role(assignments, role, *, team=None, repo=None) -> bool`: ND-8, org admin counts when they hold `gate.approve`.
  - `class OrgAuthorizer(me: Me, team: str, repo: str | None)`: implements the engine's `Authorizer.holds(user, role)`; True only for the logged-in user. M2-21 and the gate commands use it.
- **Control plane:**
  - Tables (from Task 4's `0003`): `roles(tenant, name slug pk, permissions text[], builtin bool, team_assignable bool, updated_at)`, `assignments(id uuid, tenant, user_id, role, scope_kind, scope_value, created_by, created_at; unique (tenant, user_id, role, scope_kind, scope_value))`.
  - `DEFAULT_ROLES` seed (migration data plus an idempotent `seed_roles` that `migrate` runs, so a later release can add a builtin role without touching edited ones): `org-admin` (all), `team-lead`, `developer`, `approver`, `auditor`, `exec-viewer`, `service-account` (`run.trigger` only), and `repo-maintainer` (derived only, never assigned by hand). `team-lead` may assign only roles marked `team_assignable` (`developer`, `approver`) within its team.
  - API:
    - `GET /v1/rbac/roles`; `PUT /v1/rbac/roles/{name}` (create or replace; org `rbac.assign`; unknown permission gives 422 naming it); `DELETE /v1/rbac/roles/{name}` (not builtin);
    - `GET /v1/rbac/assignments?team=&user=`; `POST /v1/rbac/assignments {email, role, team?}`, which creates the user if absent (status active, no token yet: the invite flow is M2-18); `DELETE /v1/rbac/assignments/{id}`;
    - `POST /v1/rbac/check {permission, team?, repo?, user?} -> CheckResult(allowed, reason)`; another `user` needs `rbac.assign`.
  - Guards:
    - the last org-scope `org-admin` cannot be removed;
    - `org-admin` cannot lose `rbac.assign`;
    - an assignment's team must exist in the active policy's `teams` when a policy is active (Task 7); before any policy, any slug is accepted.
  - `GET /v1/me` gains `assignments` and `permissions: EffectivePermissions`.
  - `GET /v1/audit?after_seq=&limit=` (`audit.read.org`, or `audit.read.team` filtered to `team`); `GET /v1/audit/verify -> {problems: [...]}` (`audit.read.org`).
  - Audit: `role.updated`/`role.deleted` and `role.assigned`/`role.revoked`, with `before`/`after`.

- [ ] **Step 1: Failing tests**:
  - `test_permission_catalog_matches_spec`: `set(PERMISSIONS)` equals the spec list plus the five additions; every value is a scope kind.
  - `test_permission_matrix_cell[<role>-<permission>-<scope>]` (RF4): one parametrised case per §23 cell, from a table transcribed into `core/tests/orgapi/matrix.py` (for each role: allowed in own team, other team, org, own repo). Asserts `allows(...) is expected`.
  - `test_team_scoped_role_does_not_reach_other_team`.
  - `test_org_only_permission_inert_at_team_scope`: an `auditor` at team scope does not get `audit.read.org`.
  - `test_service_account_cannot_approve_gates`.
  - `test_org_admin_holds_any_gate_role` (ND-8).
  - `test_org_authorizer_refuses_other_users`.
  - `test_team_lead_assigns_only_developer_or_approver_in_own_team` (RF4): `approver` in payments gets 201; `team-lead` gets 403 `rbac.not_assignable`; team `billing` gets 403.
  - `test_last_org_admin_cannot_be_removed`: 409 `rbac.last_admin`.
  - `test_role_edit_cannot_remove_rbac_assign_from_org_admin`: 409.
  - `test_unknown_permission_in_role_refused`: 422 naming it.
  - `test_assignment_to_unknown_team_refused_when_policy_active`: 422; the hint names `teams/<team>.yaml` (exercised fully in Task 7's suite).
  - `test_every_rbac_change_is_audited_with_before_and_after`.
  - `test_check_api_answers_for_caller`.
  - `test_checking_another_user_needs_rbac_assign`.
  - `test_me_lists_effective_permissions`.
  - `test_audit_read_needs_audit_read_permission`: developer gets 403; auditor gets 200; `verify` returns `[]`.
- [ ] **Step 2: Run** `uv run pytest core/tests/orgapi control-plane/tests/test_rbac_api.py control-plane/tests/test_audit_api.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run.** Expected: PASS.
- [ ] **Step 5: Commit.** `feat(control-plane): RBAC - permissions, default roles, team-scoped assignments, check API (M2-03)`

### Task 6: Repo registration and maintainers from CODEOWNERS (M2-03)

**Files:**
- Create: `control-plane/src/agentic_cp/rbac/{codeowners.py, repos.py}`, `control-plane/src/agentic_cp/gitops.py` (shared `GitRunner`), `migrations/versions/0004_repos.py`, `core/src/agentic/policy/repos.py`.
- Modify: `cli/src/agentic_cli/repo_guard.py` (imports the moved matcher; behaviour unchanged).
- Test: `control-plane/tests/test_codeowners.py`, `control-plane/tests/test_repos_api.py`, `core/tests/policy/test_repos.py`.

**Interfaces:**
- **Core `agentic.policy.repos`** (moved from `repo_guard`): `normalise_remote(url) -> str` and `repo_allowed(policy, remote) -> bool`. The CLI keeps the same behaviour and tests; DF-199 stays at M1-50. The control plane and the CLI now decide `allowed_repo_patterns` with one function.
- `GitRunner` (subprocess; the git flags and env from Global Constraints; error text scrubbed); injectable for tests.
- `fetch_codeowners(git, remote, branch) -> CodeownersFile(commit, path, text) | None`: probes `.github/CODEOWNERS`, `CODEOWNERS`, `docs/CODEOWNERS`, `.gitlab/CODEOWNERS` at the fetched head.
- `PROBE_PATHS = (".agentic/config.yaml", ".agentic/agents/x.md", ".agentic/skills/x/SKILL.md", "AGENTS.md")`.
- `maintainers(text, host) -> Maintainers(handles: set[str], emails: set[str], unresolved: set[str])`: union of the last-matching owners over the probes (codeowners library); `@org/team` goes to `unresolved` (ND-6).
- Tables: `repos(tenant, repo_key host/owner/name pk, remote, team, default_branch, codeowners_commit, refreshed_at)`, `repo_maintainers(tenant, repo_key, user_id, source_entry)`. Derived maintainers appear in `/v1/me` as `repo-maintainer` assignments with `derived: true`.
- API:
  - `POST /v1/repos {remote, team}`: `agent.edit.repo` in `team`; the remote must pass `repo_allowed(active policy)` (422 `repo.not_allowed`); registers and refreshes;
  - `GET /v1/repos/{repo_key:path}/maintainers -> {users, unresolved, commit}`;
  - `POST /v1/repos/{repo_key:path}/refresh`;
  - `PUT /v1/users/{email}/handles` (org `rbac.assign`).
- The worker refreshes registered repos every `AGENTIC_CP_CODEOWNERS_REFRESH_MINUTES` (default 60).
- Audit: `repo.registered`, `maintainers.synced` (before and after sets).

- [ ] **Step 1: Failing tests**:
  - `test_codeowners_maintainers_union_of_agentic_paths`: `* @carol` then `/.agentic/ @alice` then `AGENTS.md bob@x.com` gives `{alice, bob@x.com}`; carol is overridden.
  - `test_codeowners_team_entries_reported_unresolved`.
  - `test_handles_map_host_qualified`: `@alice` on github.com maps to the user with `github.com:alice`, not `gitlab.com:alice`.
  - `test_fetch_codeowners_from_git_default_branch`: a local bare repo through a `file://` URL; the commit sha is returned.
  - `test_codeowners_change_updates_maintainers` (tracker done criterion): a new commit drops alice; after refresh, alice loses `agent.approve.repo` on that repo; `maintainers.synced` has before and after.
  - `test_maintainer_allows_repo_permissions_only_for_that_repo`.
  - `test_repo_registration_requires_agent_edit_repo_and_allowed_pattern`.
  - `test_git_errors_never_show_credentials` (RF5): the remote `https://u:tok123@host/x.git` fails, and neither the problem, the log nor the audit contains `tok123`.
  - `test_repo_allowed_matches_cli_guard` (core): the cases from `cli/tests/test_repo_guard.py` give the same answer through `repo_allowed`.
- [ ] **Step 2: Run** `uv run pytest control-plane/tests/test_codeowners.py control-plane/tests/test_repos_api.py core/tests/policy/test_repos.py cli/tests/test_repo_guard.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run.** Expected: PASS (the CLI guard tests unchanged).
- [ ] **Step 5: Commit.** `feat(control-plane): repo maintainers derived from CODEOWNERS over git (M2-03)`

### Task 7: Policy sync from the git policy repo (M2-02)

**Files:**
- Create: `control-plane/src/agentic_cp/policy_sync/{__init__.py, fetch.py, validate.py, service.py, api.py, worker.py}`, `migrations/versions/0005_policy.py`.
- Modify: `core/src/agentic/policy/load.py` (`load_policy(root, *, version=None)` and `tree_digest(root)`; exported from `agentic.policy`), `core/src/agentic/orgapi/models.py` (`ActivePolicy`, `PolicyVersionInfo`, `SyncOutcome`), `__main__.py` (`worker`).
- Test: `core/tests/policy/test_load_version.py`, `control-plane/tests/test_policy_sync.py`, `control-plane/tests/test_policy_api.py`.

**Interfaces:**
- **Core:**
  - `load_policy(root, *, version: str | None = None)`: an explicit version must match the ledger's pattern (`git:<40 hex>` or `sha256:<64 hex>`), else `PolicyError`; without it, behaviour is unchanged (backward compatible).
  - `tree_digest(root) -> str`: skips `.git`; length-prefixed.
- **Tables:**
  - `policy_versions(tenant, version pk-part, commit, branch, status accepted|rejected, problems text[], bundle bytea null, digest, tree_digest, commit_at, received_at)`, insert-only;
  - `policy_active(tenant, branch pk, version, activated_at)`.
- `validate_policy_tree(root, version) -> list[str]` runs:
  - `load_policy(root / "policy", version=version)`;
  - `DefinitionSet.discover(org=root, team=root / "teams" / t, repo=None, allow_repo_pipelines=policy.allow_repo_pipelines)` for the org layer alone and for every `teams/<t>/` folder;
  - `agentic.skills.catalog.load_catalog(root / "skills")` when present (admission stays in policy-repo CI).
- `async sync_once(conn, git, settings, clock) -> SyncOutcome(kind: unchanged|activated|rejected|busy, version, problems)`:
  1. `pg_try_advisory_xact_lock('agentic.policy_sync:' || tenant)`, else `busy`;
  2. `git fetch --depth 1 origin <branch>` and `FETCH_HEAD`;
  3. if the version is already stored, `unchanged`;
  4. `git archive`, extract (`filter="data"`), validate;
  5. insert the version; when accepted, upsert `policy_active` and audit `policy.activated` (before: the previous version); otherwise audit `policy.rejected` (reason: the first problem; `after.problems`: all of them, at most 50).

  A bundle over 20 MiB is rejected (`policy.too_large`).
- `worker`: holds a session `pg_try_advisory_lock` for leadership; loops `sync_once` every `policy_poll_seconds`, and the CODEOWNERS refresh from Task 6.
- API (all authenticated):
  - `GET /v1/policy/active -> ActivePolicy(version, branch, digest, tree_digest, activated_at, commit_at, bundle_path)`, 404 `policy.none` before the first accepted sync;
  - `GET /v1/policy/versions?limit=20 -> list[PolicyVersionInfo]`;
  - `GET /v1/policy/versions/{version}/bundle`: `application/gzip`, `ETag: "<digest>"`, `Cache-Control: private, immutable`;
  - `POST /v1/policy/sync` (org `policy.edit.org`): runs `sync_once` and returns `SyncOutcome` (202 when `busy`);
  - `POST /v1/policy/webhook` (no bearer): needs `X-Hub-Signature-256: sha256=<hmac>` over the raw body or `X-Agentic-Webhook-Token`, compared in constant time; the payload is ignored (it only nudges a fetch); 401 `webhook.bad_signature`; 404 when no secret is configured.

- [ ] **Step 1: Failing tests**:
  - `test_load_policy_accepts_explicit_version` (core): a bad string raises `PolicyError`.
  - `test_policy_tree_digest_is_length_prefixed` (core): files (`a`, `bc`) and (`ab`, `c`) differ; the same bytes give the same digest.
  - `test_valid_policy_is_activated_with_git_version`: a local repo seeded from `templates/standalone`; `active.version == "git:" + sha`; `policy.activated` audited.
  - `test_invalid_policy_rejected_and_previous_kept` (RF3): a commit breaking `tiers.yaml` gives `rejected`; the problems name `tiers.yaml`; `active.version` is unchanged; `GET /v1/policy/versions` shows the rejection.
  - `test_definitions_validated_for_every_team`: a broken `teams/payments/agents/x.md` is rejected, naming it.
  - `test_unchanged_head_is_not_reprocessed`.
  - `test_branch_is_configurable`: branch `staging` activates its own commit.
  - `test_bundle_digest_matches_and_is_deterministic` (RF3): two fresh databases syncing the same commit get equal `digest` and `tree_digest`; the downloaded bytes hash to `digest`.
  - `test_git_runs_with_autocrlf_off`: the recorded `GitRunner` commands include `core.autocrlf=false`.
  - `test_only_one_sync_at_a_time`: two concurrent `sync_once` calls; one answers `busy`.
  - `test_webhook_requires_valid_signature`: bad gives 401, good gives 202 and triggers a sync, unconfigured gives 404.
  - `test_manual_sync_requires_policy_edit_org`.
  - `test_active_policy_requires_authentication`.
  - `test_assignment_to_unknown_team_refused` (completes Task 5's case with a real active policy).
- [ ] **Step 2: Run** `uv run pytest core/tests/policy control-plane/tests/test_policy_sync.py control-plane/tests/test_policy_api.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run.** Expected: PASS.
- [ ] **Step 5: Commit.** `feat(control-plane): policy sync - git fetch, validation, versions, bundle, webhook (M2-02)`

### Task 8: Run event ingest with chain verification and idempotency (M2-04)

**Files:**
- Create: `control-plane/src/agentic_cp/ingest/{__init__.py, verify.py, store.py, api.py}`, `migrations/versions/0006_runs.py`.
- Modify: `commons/src/sdlc_commons/ledger.py` (`verify_entries`), `core/src/agentic/orgapi/models.py` (`IngestRequest`, `IngestResult`, `ChainHead`, `RunSummary`).
- Test: `commons/tests/test_ledger_verify_entries.py`, `control-plane/tests/test_ingest.py`.

**Interfaces:**
- **Commons** `verify_entries(entries: Sequence[Mapping], *, run_id: str, after: tuple[int, str] = (0, "")) -> list[str]`. It applies Zyroflo's rules (`prev` link, recorded hash through `entry_hash`, consecutive `seq`, `run_id` claim) to entries in memory, continuing from `after`, plus:
  - the event type is in Zyroflo's `events.TYPES`, and its fields are in `events.FIELDS`;
  - the `ext.agentic` values match `ledger.FIELDS` patterns;
  - the run identity (team, policy_version) is identical on every entry (the `RunLedger.verify` rule).

  Each problem names the entry's position (`event <seq>`).
- **Tables:**
  - `runs(tenant, run_id, team, started_by user_id, policy_version, policy_known bool, status, head_seq, head_hash, opened_at, last_event_at; pk (tenant, run_id))`;
  - `run_events(tenant, run_id, seq, hash, prev, event, step, at text, payload jsonb, received_at; pk (tenant, run_id, seq))`, insert-only.
- `POST /v1/runs/{run_id}/events` with `IngestRequest(entries: list[dict], 1..max_ingest_entries)`; the body is capped at `max_body_bytes` (413 `ingest.too_large`). In one transaction:
  1. lock the run row (`SELECT ... FOR UPDATE`; a new run is inserted first with `ON CONFLICT DO NOTHING`);
  2. entries with `seq <= head` must match the stored `hash`: equal ones count as `duplicates`; a different hash is a 409 `ingest.fork` with `head`;
  3. the rest must start at `head + 1`, else 409 `ingest.gap` with `head`;
  4. `verify_entries(rest, after=head)`, else 422 `ingest.chain_broken` with `problems`;
  5. authorisation:
     - the first batch must start at `run_opened`, and the caller needs `run.trigger` in the run's team (`ext.agentic.team`); the run is bound to that caller (`started_by`);
     - later batches come from `started_by`, or from a caller with `run.cancel.any` in that team;
  6. insert, move the head, set `status` from `run_closed`, flag an unknown `policy_version` (ND-7).

  A rejection stores nothing for the run and writes an `ingest.chain_rejected` / `ingest.fork` audit record in its own transaction. Returns `IngestResult(accepted, duplicates, head)`.
- `GET /v1/runs/{run_id} -> RunSummary`: readable by the starter, by `dashboard.view.team` / `audit.read.team` in its team, or org-wide.
- `GET /v1/runs/{run_id}/events?after_seq=&limit=`: the stored entries exactly as received (for M2-26 `audit verify`); same permissions.

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
    - `test_run_closed_sets_status`.
    - `test_run_summary_and_events_readable_by_starter_and_team_auditor`: another team's developer gets 403.
    - `test_concurrent_batches_for_one_run_serialise` (RF1): two concurrent identical posts store n rows; the results sum to `accepted + duplicates == 2n`.
    - `test_runtime_role_cannot_rewrite_run_events` (RF2, also covered in Task 3).
- [ ] **Step 2: Run** `uv run pytest commons/tests/test_ledger_verify_entries.py control-plane/tests/test_ingest.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run.** Expected: PASS.
- [ ] **Step 5: Commit.** `feat(control-plane): run event ingest with chain verification and idempotent replay (M2-04)`

### Task 9: CLI side: client, login, policy from the control plane, org roles, sink (M2-01..M2-04 client)

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
- Credential `CONTROL_PLANE_TOKEN = "control_plane_token"` (env `AGENTIC_CONTROL_PLANE_TOKEN`; the keyring in `agentic.auth`).
- `control_plane_url(ws)`: moves from `publishing.py`; the same lookup (`AGENTIC_CONTROL_PLANE_URL`, then the user config `control_plane_url`).
- `class ControlPlaneError(CliError)`, with subclasses `ControlPlaneUnavailable` (exit 1), `ControlPlaneRefused` (401/403/426, exit 2, the server's hint or `agentic login --for control-plane`) and `ChainRejected` (`problems`, `head`).
- `ControlPlaneClient(base_url, token, *, transport=None, timeout=(5, 15))` over `httpx2.Client`:
  - TLS through truststore, like the gateway client; honours `HTTPS_PROXY` / `NO_PROXY`;
  - the Global Constraints headers;
  - methods: `me() -> Me`, `active_policy() -> ActivePolicy`, `bundle(policy: ActivePolicy) -> bytes` (digest checked, else `ControlPlaneRefused`), `check(permission, *, team=None, repo=None) -> CheckResult`, `ingest(run_id, entries) -> IngestResult`, `run_url(run_id) -> str`.
  - `from_workspace(ws) -> ControlPlaneClient | None` (None without a URL).
- `agentic login --for control-plane [--url U]` (ND-13):
  - prompts, or reads `--key-stdin`;
  - calls `me()` first and stores the token only on 200;
  - prints `Signed in to <url> as <email> - <team>: <roles>`;
  - JSON: `{"stored": "control_plane_token", "verified": true, "user": ..., "teams": {...}}`.
- `policy_source`, org mode with a control plane (ND-2):
  - fetches `active_policy()` and `bundle()`, extracts into the scratch folder (`filter="data"`) and loads `load_policy(folder, version=active.version)`;
  - `commit()` writes `fetched.json` with `source_kind: "control-plane"`, `url`, `version`, `digest`, `tree_digest`, `fetched_at`, `expires_at`;
  - an unreachable control plane falls back to the unexpired cache with a warning; an expired one is refused (today's rules);
  - without a control plane URL, the git clone path is unchanged.
- `Workspace.policy()`: when `fetched.json` says `control-plane`, recompute `tree_digest(cache/repo)`. A mismatch is a `CliError` REFUSED: "the cached org policy was changed locally", hint `agentic init`. Then load with `version=fetched.version`.
- Org-mode `init` with a control plane:
  - roles come from `me().assignments` (`org-admin` or `team-lead` in the user's team selects authoring skills); `config.roles` is ignored, with one warning;
  - check 7 is `client.check("agent.edit.repo", team=..., repo=normalise_remote(origin))`, refused as `GuardRefusal(7)` with hint "ask your team lead or org admin";
  - after the guard passes, `init --repo` registers the repo (`POST /v1/repos`, idempotent).
- `class ControlPlaneSink` (`name = "control-plane"`): implements `agentic.telemetry.sinks.EventSink`; `deliver(run_id, entries)` calls `client.ingest`, and any refusal raises, so `forward` keeps its cursor. It is **not wired into runs here**: M2-20 wires it, with a short timeout, the offline replay command and DF-167/172/179/185.
- PR links (ND-11): `run_url(run_id)` = `<cp>/v1/runs/<run_id>`; handoff links stay repo-relative.

- [ ] **Step 1: Failing tests**:
  - `test_client_sends_bearer_and_cli_version_headers` (MockTransport).
  - `test_problem_json_becomes_cli_error_with_hint`: 401 gives exit 2, and the hint contains `agentic login --for control-plane`.
  - `test_unreachable_control_plane_is_a_clear_error`: `ConnectError` gives `ControlPlaneUnavailable`, naming the URL without credentials.
  - `test_login_for_control_plane_verifies_before_storing`: the output names the user and teams.
  - `test_login_for_control_plane_refuses_a_rejected_token_and_stores_nothing`.
  - `test_login_for_control_plane_without_url_is_invalid`: the hint names `control_plane_url` and `AGENTIC_CONTROL_PLANE_URL`.
  - `test_org_init_fetches_the_active_bundle`: `LoadedPolicy.policy.version == active.version`; `fetched.json` has `source_kind == "control-plane"`.
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
  - **Contract tests** (real app under uvicorn on `127.0.0.1:0` in a thread, plus Postgres):
    - `test_login_me_roundtrip`;
    - `test_policy_fetch_end_to_end`: git repo, `sync_once`, CLI org fetch; the run's policy version equals `git:<sha>`;
    - `test_ledger_forward_to_control_plane_end_to_end`: `RunLedger` + `ForwardingRecorder([ControlPlaneSink])`; the control plane's head equals the local last hash; after deleting the cursor and forwarding again, only duplicates are reported.
- [ ] **Step 2: Run** `uv run pytest cli/tests/test_control_plane_client.py cli/tests/test_login.py cli/tests/test_policy_from_control_plane.py cli/tests/test_init_org_rbac.py cli/tests/test_control_plane_sink.py control-plane/tests/contract -q`. Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the whole suite: `uv run pytest -q`. Expected: PASS.
- [ ] **Step 5: Commit.** `feat(cli): control-plane client - login, policy bundle, org roles, ingest sink (M2-01..M2-04)`

### Task 10: Server entry points, final verification, review

**Files:**
- Modify: `control-plane/src/agentic_cp/__main__.py` (`serve` runs uvicorn programmatically on `AGENTIC_CP_HOST`/`AGENTIC_CP_PORT` and refuses to start under ND-5; `worker`; `migrate` runs `alembic upgrade` then `grant-runtime`).
- Test: `control-plane/tests/test_main.py`.

- [ ] **Step 1: Failing tests**:
  - `test_serve_refuses_unenforced_audit`: exit 2, and the message names `AGENTIC_CP_REQUIRE_AUDIT_ENFORCEMENT`.
  - `test_migrate_applies_grants`.
  - `test_help_lists_commands`: `serve`, `worker`, `migrate`, `admin`, `db`, `openapi`.
- [ ] **Step 2: Implement; run** `uv run pytest control-plane -q`. Expected: PASS.
- [ ] **Step 3: Verify:**
  - `uv run python -m agentic_cp openapi --check`
  - `uv run lint-imports`
  - `uv run python scripts/licence_check.py`
  - `uv run python scripts/check.py --all` (Windows natively with Rancher Desktop or Podman, and the Linux container with the Postgres sidecar)
  - `rg -n "fmt: (skip|off)" control-plane commons core cli` returns nothing.
- [ ] **Step 4: Review.** Sonnet high-effort review subagent over the whole slice against the Review Focus and the Deferred items table; fix, or defer with DF entries.
- [ ] **Step 5: Commit.** `feat(control-plane): serve/worker/migrate entry points; slice 1 verified (M2-01..M2-05)`

---

## Slice 2: remaining SP15 rows (interfaces only; each gets its TDD plan before it starts)

The same package and conventions apply: problem+json, `agentic.orgapi` models, audit on every change, insert-only evidence, CLI client methods in `agentic_cli/control_plane.py`.

| Row | Module and tables | API | CLI / client side | Key tests (from tracker) | Depends |
|---|---|---|---|---|---|
| M2-06 kill switch | `killswitch/`; `kill_switches(id, tenant, scope_kind global\|team\|user\|repo\|pipeline\|agent\|model\|backend, scope_value, mode kill\|pause, reason, created_by, created_at, cleared_by, cleared_at)` | `POST /v1/killswitch {scope_kind, scope_value, mode, reason}` (global/any scope: `killswitch.global`; team leads only within their team: `killswitch.team`); `DELETE /v1/killswitch/{id}`; `GET /v1/killswitch/state?team&user&repo&pipeline&agent&model&backend -> KillState(killed, mode, switches, checked_at, ttl_seconds=30)` with `ETag`/304 for cheap polling | `ControlPlaneClient.kill_state(scope) -> KillState`; core protocol `KillSwitchSource` for the engine (M2-19); the CLI caches at most 30 s | scope matching per kind (agent by name and name@version); team lead limited to own team; every change audited `killswitch.set` / `.cleared` | M2-03, M2-05 |
| M2-07 notifications | `notify/`; `notifications(id, tenant, kind, to, subject, body, dedupe_key unique, status pending\|sent\|failed, attempts, next_attempt_at)` outbox; `Notifier` protocol with `SmtpNotifier` (aiosmtplib 5.1.3, STARTTLS, org relay settings `AGENTIC_CP_SMTP_*`) and a `WebhookNotifier` seam; templates as package data (`string.Template`) | none public; the worker drains the outbox under an advisory lock | — | aiosmtpd 1.4.6 capture server: gate-waiting, budget and invite mails rendered; retry with backoff; one mail per `dedupe_key` | M2-01 |
| M2-08 min CLI version + concurrency | middleware reads `X-Agentic-CLI-Version`; `min_cli_version` from org policy (`org.yaml`, new field) or `AGENTIC_CP_MIN_CLI_VERSION`; `POST /v1/runs {run_id, team, pipeline, repo}` registers a run atomically against `limits.yaml` concurrency (team advisory lock) before its first event | 426 `cli.too_old` with an upgrade hint; `POST /v1/runs` gives 201, or 409 `runs.concurrency_cap` naming the cap; `GET /v1/concurrency?team=` | the CLI maps 426 to exit 2 with the upgrade message; preflight (M2-21) registers the run; closes DF-182's version part | an old CLI refused with the message; cap enforced under a race (two registrations at cap-1) | M2-01, M2-03 |
| M2-09 routing generation | `routing/generate.py` pure `routing_rules(policy) -> RoutingDocument` (tiers, team overrides, classification restrictions, team limits); `routing/apply.py` `GatewayAdmin` client to TokenLean's ST-10 endpoint; runs on every `policy.activated` | `GET /v1/routing/current` (admin) | — | generated rules match a fixture; applied and read back; audited `routing.applied` with the policy version | M2-02, M2-30, M2-35 |
| M2-10 graceful pause | `mode: pause` on M2-06 switches; maintenance pause = a global pause switch | as M2-06 | engine (M2-19): finish the current turn, checkpoint, hold; resume when cleared | a paused run resumes without loss; a killed run does not | M2-06, M2-19 |
| M2-11 auto-trip | `autotrip/` worker job: team spend rate (TokenLean `usage_events` via M2-37 views) and error rate against thresholds in policy; creates a pause switch with actor `system:auto-trip`, notifies, audits; cooldown plus manual clear | `GET /v1/autotrip/rules` | — | synthetic spend series trips and resets correctly; one alert per trip | M2-10, M2-07, M2-37 |
| M2-12 health + metrics | `/readyz` (exists); `/metrics` via prometheus-client 0.26.0 with bounded labels (route template, status class, rejection code): `ingest_events_total`, `ingest_rejected_total{code}`, `ingest_lag_seconds` (received_at minus entry `at`), `policy_sync_last_success_timestamp`, `notification_queue_depth`, `audit_enforced`; alert rules `control-plane/deploy/alerts.yaml` | `GET /metrics` (no auth, cluster-internal; a NetworkPolicy in M3-02) | — | metrics move on ingest; `promtool check rules` passes inside the Linux check container | M2-01 |
| M2-13 audit export | `audit/export.py`: a time-range export of the tenant chain skeleton (`seq`, `prev`, `hash` of every record) plus full bodies only for the requested team's records, and the head at export time; JSONL or CSV | `GET /v1/audit/export?team&from&to&format` (streamed; `audit.read.team`/`.org`) | `agentic audit export` (M2-25/26 side) | the export verifies independently: skeleton links, each body hashes to its skeleton hash, other teams' bodies absent | M2-05, M2-26 |
| M2-14 service accounts + CSV import | `users.kind = service`; role `service-account` (team scope); tokens require `expires_at` at most `service_token_max_days`; `rbac/importer.py` CSV `email,role,team,handle` with a dry-run diff | `POST /v1/service-accounts {name, team, days}` gives the token once; `POST /v1/rbac/import?dry_run=` | `agentic rbac import` (M2-25) | a service account cannot approve gates; the CSV import is idempotent and the dry run shows the diff | M2-03, M1-48 |
| M2-15 versioned rollout | mostly core definitions (`rollout:`, `pin`, `deprecated_after` in policy-repo agents, pipelines and skills); the control plane validates them in `validate_policy_tree` and serves them in the bundle | none new | preflight warns or blocks per date | a team not in `rollout:` keeps the old version; deprecated blocks after its date | M2-02 |
| M2-16 budget alerts | ingest watches `step_finished.cost` per run; team spend from TokenLean; thresholds 80/100 % produce outbox rows with `dedupe_key = (scope, threshold, period)` | — | — | one alert per threshold crossing | M2-07, M1-13 |
| M2-17 key rotation / revocation | `keys/` client for TokenLean `/admin/keys` (ST-9); `POST /v1/users/{email}/proxy-key/rotate`, revoke on offboard; audit `key.rotated` / `.revoked` | as listed | the CLI maps TokenLean's revoked-key 401 to "your gateway key was revoked or rotated - run `agentic login`"; takes DF-182's gateway-verify part | revoked key rejected within the TTL; actionable CLI message | M1-48, M2-03, M2-34 |
| M2-18 provisioning | `invites(id, tenant, user_id, token_sha256, expires_at, used_at)`; a new user's first assignment creates an invite and an email (M2-07); offboarding disables the user, revokes control-plane tokens and the proxy key (M2-17), and sets a user-scope kill switch for running work | `POST /v1/invites/{token}/redeem` (single use, no bearer) gives the proxy key, a control-plane token and `control_plane_url`; `POST /v1/users/{email}/offboard` | `agentic login --invite <url>` redeems once and stores both secrets in the keyring (no copy-paste of keys; settles ND-13's URL question) | link single-use; an offboarded user cannot run (401 at the control plane, kill switch, revoked gateway key) | M2-03, M2-07, M2-17, M2-34 |

### What slice 1 gives the consuming rows (M2-19..M2-26)

| Row | Uses |
|---|---|
| M2-19 kill-switch checks (SP6) | `KillSwitchSource` from M2-06; the policy cache expiry from `fetched.json` (Task 9) for "pause when stale" |
| M2-20 control-plane sink (SP12) | `ControlPlaneSink` and `ControlPlaneClient.ingest` (Task 9); 409 `ingest.gap` carries the server head, so a lost or empty cursor (DF-179) re-delivers from `head + 1`; duplicates are no-ops (DF-167). It must not block the engine: `ForwardingRecorder.record` is synchronous and runs inside the async engine, so delivery needs a short timeout or a background thread (new DF). Personal-skill sync and init audit events (DF-185) need new endpoints (`PUT /v1/users/me/skills/{name}`, `POST /v1/audit/client-events`, chained by the control plane on insert) |
| M2-21 preflight org checks (SP6) | `OrgAuthorizer` and `/v1/rbac/check` (Task 5); run registration and caps (M2-08); kill state (M2-06); the pipeline integrity hash from the validated bundle (Task 7); a run with an unknown policy version is refused (ND-7) |
| M2-22..M2-24 (SP6) | none from the control plane |
| M2-25 admin commands (SP14) | `/v1/rbac/*` (Task 5), `/v1/killswitch` (M2-06), pause (M2-10), CSV import (M2-14) |
| M2-26 `agentic audit verify` (SP14) | `GET /v1/audit` and `GET /v1/runs/{id}/events` return records exactly as hashed; one verifier, `sdlc_commons.ledger.verify_entries` and `entry_hash`, checks both chains |

## Effort

Tracker estimates: M2-01 1.5, M2-02 1.5, M2-03 2, M2-04 1.5, M2-05 1 = 7.5 PW for slice 1. Task 9's CLI work (about 1 PW) was not in any row: it absorbs DF-184/DF-203 (M2-03) and part of DF-182, and is suggested as a re-estimate of M2-03 (+0.5) and M2-02 (+0.5). Slice 2: 10 PW as tracked.
