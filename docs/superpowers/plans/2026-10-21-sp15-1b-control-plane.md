# SP15 Control Plane, Slice 1b Implementation Plan (track sp15-1b-control-plane)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build slice 1b of the SP15 control plane: Tasks 7-11 of the committed parent plan `docs/superpowers/plans/2026-10-21-sp15-control-plane.md`, on top of slice 1a exactly as merged (`docs/superpowers/plans/2026-10-21-sp15-1a-control-plane.md`, rulings R1-R11). Each tenant's git policy repo is fetched, validated with the CLI's own core code, versioned as `git:<commit>` and served as a digest-checked bundle; repos register per tenant and their maintainers come from CODEOWNERS over git; run event chains are ingested per tenant, verified entry by entry, idempotently; the CLI logs in to the control plane (verified through `/v1/me`), fetches the tenant's policy bundle, takes roles from the control plane in org mode, and delivers its run ledger through a control-plane sink with an offline queue and idempotent replay (M2-20). The CLI never sends a tenant and refuses a cached policy bundle from another tenant.

This file does not restate the parent plan. It records what this track builds, the names it chooses where the parent plan leaves room, the deferred items it pulls in, and every ruling it made.

**Rows:** M2-02 (policy sync), M2-03 rest (CODEOWNERS maintainers, repo-scoped checks, DF items), M2-04 (ingest), M2-20 (control-plane sink, offline queue, replay; personal-skill/memory sync stays with M2-45).

**Architecture** (parent plan Architecture, slice-1b parts):
- `control-plane/src/agentic_cp/`: `gitops.py` (`GitRunner`: one subprocess runner, the parent plan's flags and env, per-tenant `GIT_CONFIG_GLOBAL`, scrubbed errors, URL policy), `policy_sync/` (`validate.py`, `service.py`, `api.py`, `worker.py`), `tenancy/api.py` (`/v1/tenant`), `rbac/codeowners.py` + `rbac/repos.py` + `rbac/repos_api.py`, `ingest/` (`store.py`, `api.py`), `body_limit.py` (ASGI body cap, DF-420); migrations `0005_policy.py`, `0006_repos.py`, `0007_runs.py`, `0008_role_seed.py`;
- core: `agentic.policy.load` gains `load_policy(root, *, version=None)` and `tree_digest(root)`; `agentic.policy.repos` (the repo matcher moved out of the CLI guard); `agentic.orgapi.models` gains the slice-1b wire models;
- commons: `sdlc_commons.ledger.verify_entries` (Zyroflo's chain rules over entries in memory);
- CLI: `agentic_cli/control_plane.py` (client, errors, login record, `ControlPlaneSink`), changes to `commands/login.py`, `policy_source.py`, `workspace.py`, `commands/init.py`, `repo_guard.py`, `publishing.py`, `runtime.py`, `commands/runs.py` (`agentic runs sync`); core `agentic.telemetry.sinks` gains the cursor fixes and background forwarding.

**Tech Stack:** as slice 1a, plus `codeowners==0.9.0` (control plane only). No other new dependency.

## OSS reuse decisions

The parent plan's table stands. Re-checked 2026-10-22 for what this track adds:

| Need | Reused | Not reused, why |
|---|---|---|
| CODEOWNERS matching | `codeowners` **0.9.0** (MIT per `license_expression`, `py3-none-any` wheel, no dependencies, `requires_python >=3.10`, uploaded 2026-06-28, older than two weeks); imported only by `agentic_cp.rbac.codeowners` | Own parser (last-match-wins and anchoring are easy to get subtly wrong) |
| Offline queue for the sink | **The run ledger itself** (`events.jsonl`, Zyroflo's durable, append-only, hash-chained log) plus the existing per-sink cursor file: the ledger is the queue, the cursor its committed offset | persist-queue (BSD-3) and diskcache (Apache-2.0): a second durable copy of every event that can disagree with the chain; nothing they add (ordering, durability, at-least-once) is missing from ledger + cursor |
| Background delivery | stdlib `threading` (one daemon thread per run's recorder, woken by an event) | an asyncio task: `RunRecorder.record` is synchronous and Zyroflo's writes are blocking file I/O |
| Policy bundle | `git archive` + stdlib `tarfile` / `gzip` (`mtime=0`, empty file name) | dulwich (parent plan: a second git implementation) |
| CLI HTTP client | httpx2 (already a CLI and core dependency) with `agentic.transport.network.ssl_context` (truststore) | — |
| ASGI body limit | own 40-line middleware (`Content-Length` check plus a streaming count) | Starlette has no body-size middleware; a reverse-proxy limit is still recommended |

## Rulings made by this track (for the coordinator's decisions log)

Numbered after slice 1a's R1-R11.

- **R12 Tenant-set repository URLs must be remote.** A policy repo or registered repo URL must be `https://`, `ssh://` or the scp form `user@host:path`. `file://` URLs, bare paths, `ext::` and anything starting with `-` are refused (`repo.bad_url`), unless the deployment sets `AGENTIC_CP_ALLOW_LOCAL_REPOS=true` (development and tests only). Without this, a tenant admin could point their policy repo at the server's disk - including another tenant's policy work folder - and read it back through the bundle API. git also runs with `-c protocol.ext.allow=never -c protocol.file.allow=<user|never>`.
- **R13 Symlinks and gitlinks are refused in a policy tree.** `validate_policy_tree` rejects a commit holding a symbolic link or submodule (`policy.symlink`): Windows laptops cannot create links without a privilege, and a link may point outside the tree when extracted.
- **R14 The team guard reads teams stored with the version.** `policy_versions.teams text[]` records `OrgPolicy.teams` at validation; an assignment's team must be in the active version's list once a policy is active (DF-404), the hint naming `policy/teams/<team>.yaml`.
- **R15 Webhook syncs run after the 202.** `POST /v1/policy/webhook/{hook_token}` answers 202 and runs the tenant's `sync_once` as a Starlette background task in the same process; the rate limiter is asked with the tenant id as principal (`route = "webhook"`). The payload is never read.
- **R16 One ingest transaction per batch, rejection audited separately.** Locking, duplicate and fork checks, verification, authorisation and insert run in one transaction in the caller's tenant; any refusal rolls it back, then a second transaction writes the `ingest.*` audit record. The run's team is `ext.agentic.team` of entry 1, its policy version `ext.agentic.policy_version`.
- **R17 Ingested entries are stored as jsonb and re-verified on read in tests.** `run_events.entry` holds the whole entry as received (jsonb). Postgres `numeric` keeps every decimal digit Python's `repr` writes, so a float cost re-hashes identically; `test_ingested_entries_rehash_after_the_jsonb_round_trip` proves it.
- **R18 The CLI binds its control-plane token to the URL it verified.** `agentic login --for control-plane` stores the token in the keyring (`control_plane_token`) and the URL, tenant and user it verified in `~/.agentic/control-plane.json` (no secret). A later command whose control-plane URL differs from the recorded one is refused with the login hint, so changing `AGENTIC_CONTROL_PLANE_URL` (or a config edit) can never send the token to another server. A token from the environment (`AGENTIC_CONTROL_PLANE_TOKEN`, CI) has no record and is used as given.
- **R19 In org mode a control plane wins over `policy_repo`.** With `org` and a control-plane URL in the user config, init fetches the tenant's bundle (ND-2); `policy_repo` cloning stays for orgs without a control plane.
- **R20 Sink delivery never blocks a run (DF-304).** With a control plane, runs record through a `ForwardingRecorder(background=True)`: `record` appends to the ledger and wakes a delivery thread. The ledger and the cursor are the offline queue; a failed delivery waits for the next event or `agentic runs sync`. A CLI command that ran the engine waits at most 5 s for pending deliveries before it exits, then reports how many events stay queued.
- **R21 `ingest.gap` rewinds the cursor once.** When the control plane's head is behind the sink's cursor (its database was restored, or a cursor was copied), `ControlPlaneSink` raises `SinkBehind(head)`; `forward` checks the local chain at that head (same `seq` and `hash`) and re-delivers from `head + 1`. A head that is not in the local chain is a `SinkError` (the two chains forked).
- **R22 Builtin roles grow with the catalogue without undoing tenant edits (DF-419).** `roles.seed_permissions` records what the seed last gave each builtin role; `seed_roles` (run by `migrate` for every tenant) adds the permissions that are in the release's seed but not in `seed_permissions` - new in this release - and leaves permissions a tenant removed removed. Audited `rbac.seeded` with the additions.

## Global Constraints

The parent plan's Global Constraints apply in full (wire contract, headers, tenancy, tokens, RBAC, audit records, evidence tables, bundles, import contracts, provider-agnostic, Windows and macOS, pins, formatting). In particular:
- Every new table (`policy_versions`, `policy_active`, `repos`, `repo_maintainers`, `runs`, `run_events`) is created through `enable_tenant_rls`, listed in `TENANT_TABLES`, has a row factory in `control-plane/tests/isolation.py`, and every object route has an `ENDPOINT_CASES` entry; the completeness guards (`test_every_tenant_table_has_an_isolation_factory`, `test_every_table_is_tenant_owned_and_forced`, `test_every_object_route_has_an_isolation_case`) stay green.
- `policy_versions` and `run_events` are insert-only for the runtime role (`INSERT_ONLY` already names them).
- Another tenant's object answers 404 exactly like a missing one; the tenant comes only from the token (or the webhook token through the registered `resolve_webhook`).
- New import contract: `codeowners` only in `agentic_cp.rbac.codeowners`; `codeowners` joins the server-libraries list.
- Never `# fmt: skip` / `# fmt: off`; line length 100; check pytest's exit code directly; `uv run python scripts/check.py --all` green on Windows and Linux.
- TRACKER.md, DEFERRED.md and DECISIONS-*.md are not edited here; the structured output carries the updates.

## Review Focus

The parent plan's Review Focus 1, 3, 4, 5 and 6 (slice-1b parts), plus:
1. **R12 URL policy.** No tenant-reachable path lets git read the server's disk or run a transport helper. Tests: `test_policy_source_refuses_local_and_option_urls`, `test_repo_registration_refuses_local_urls`.
2. **Ingest all-or-nothing.** Parent Review Focus 1 tests, plus `test_ingested_entries_rehash_after_the_jsonb_round_trip`.
3. **The CLI's token never leaves for another server (R18), and a cached bundle of another tenant is refused.** Tests: `test_token_is_not_sent_to_a_different_url`, `test_cached_policy_of_another_tenant_is_refused`.
4. **The sink loses nothing and blocks nothing (R20, R21).** Tests: `test_events_queued_offline_replay_once_in_order`, `test_background_recorder_does_not_block_on_a_slow_sink`, `test_gap_rewinds_to_the_server_head`, `test_cursor_hash_mismatch_is_refused` (DF-172), `test_empty_cursor_redelivers_from_the_start` (DF-179).

## Deferred items pulled in

| Item | Handling here |
|---|---|
| DF-404 team must exist in the active policy | Task 7 (R14) |
| DF-377 policy cache leftovers never removed | Task 10: `_swap_in` first removes every `.repo.new-*`, `.repo.old-*` and `.fetched.json.tmp-*` beside the cache, ignoring errors |
| DF-405 repo maintainers from CODEOWNERS | Task 8 |
| DF-418 `/v1/rbac/check` takes the repo team from the body | Task 8: a registered repo's team comes from `repos`; a conflicting `team` is 422 `rbac.repo_team_mismatch`; an unregistered repo has no team scope |
| DF-417 `put_role` validates before authorising | Task 8 (RBAC touch-ups): `require(rbac.assign)` first |
| DF-419 builtin roles do not pick up new catalogue permissions | Task 8 (R22) |
| DF-182 / DF-340 verify at login, register with the control plane, min CLI version | Control-plane half in Task 10 (`login --for control-plane` verifies through `/v1/me` before storing; `init --repo` registers the repo). Gateway-key verification: retarget M2-17. Minimum CLI version: M2-08 |
| DF-184 `agent.edit.repo` for `init --repo` | Task 10 (check 7 asks `/v1/rbac/check`) |
| DF-203 self-asserted roles | Task 10 (org mode with a control plane takes roles from `/v1/me`; standalone documented as "chooses skills only, never authorises") |
| DF-420 `max_body_bytes` / `max_ingest_entries` not enforced | Task 9: app-wide body cap (413 `too_large`) and the ingest batch cap |
| DF-291 control-plane run URL | Task 10: PR run link `<cp>/v1/runs/<run_id>` (ND-11); handoff links stay repo-relative. Run pages stay M2-38 |
| DF-167, DF-172, DF-179, DF-304 sink | Task 10b (M2-20) |
| DF-303 approvals chain mirror, DF-185 init audit events, DF-364 secret-scan events | Not in this slice: each needs a second ingest family (approvals entries, client audit events). Stay open at M2-20 |
| DF-288 git host settings in org policy | Stays retargeted to M2-51 (not RBAC) |

---

### Task 7: Policy sync per tenant (M2-02)

As parent Task 7, with R12-R15.
- Files: `core/src/agentic/policy/load.py` (+ `__init__` exports), `core/src/agentic/orgapi/models.py`; `control-plane/src/agentic_cp/{gitops.py, settings.py, tables.py, db_roles.py, app.py, __main__.py}`, `policy_sync/{__init__.py, validate.py, service.py, api.py, worker.py}`, `tenancy/api.py`, `rbac/api.py` (team guard), `migrations/versions/0005_policy.py`; `control-plane/tests/isolation.py`.
- Interfaces (names chosen here): `GitRunner(settings).run(args, *, cwd, tenant_slug, timeout) -> str`, `GitError`; `check_repo_url(url, *, allow_local) -> str`; `PolicyRepoTree` helpers in `validate.py`: `validate_policy_tree(root, version) -> tuple[list[str], OrgPolicy | None]`; `service.sync_once(engine, git, tenant_id, settings, clock) -> SyncOutcome`; `service.make_bundle(git, workdir, commit) -> bytes`; `worker.run_pass(engine, git, settings, clock) -> dict[slug, SyncOutcome | str]`; settings `allow_local_repos: bool = False`.
- Tests: `core/tests/policy/test_load_version.py`, `control-plane/tests/{policy_repos.py (helpers), test_policy_sync.py, test_policy_api.py, test_tenant_api.py}` plus the parent plan's list and `test_policy_source_refuses_local_and_option_urls`.
- [ ] Step 1: write the tests. - [ ] Step 2: run, see them fail. - [ ] Step 3: implement. - [ ] Step 4: pass. - [ ] Step 5: commit `feat(control-plane): per-tenant policy sync - git fetch, validation, versions, bundle, webhook (M2-02)`.

### Task 8: Repos and maintainers from CODEOWNERS; RBAC touch-ups (M2-03)

As parent Task 8, plus DF-417, DF-418, DF-419.
- Files: `control-plane/pyproject.toml` (`codeowners==0.9.0`), root `pyproject.toml` (contract), `uv.lock`; `core/src/agentic/policy/repos.py`; `cli/src/agentic_cli/repo_guard.py` (imports the moved matcher); `control-plane/src/agentic_cp/rbac/{codeowners.py, repos.py, repos_api.py, store.py, authz.py, api.py, defaults.py}`, `migrations/versions/{0006_repos.py, 0008_role_seed.py}`, `tables.py`, `policy_sync/worker.py` (refresh); `control-plane/tests/isolation.py`.
- Tests: `core/tests/policy/test_repos.py`, `control-plane/tests/{test_codeowners.py, test_repos_api.py, test_rbac_touchups.py}`.
- [ ] Steps 1-5 as above; commit `feat(control-plane): per-tenant repo maintainers derived from CODEOWNERS over git (M2-03)`.

### Task 9: Run event ingest per tenant (M2-04)

As parent Task 9, with R16, R17 and DF-420.
- Files: `commons/src/sdlc_commons/ledger.py` (`verify_entries`), `core/src/agentic/orgapi/models.py`, `control-plane/src/agentic_cp/{body_limit.py, app.py, tables.py}`, `ingest/{__init__.py, store.py, api.py}`, `migrations/versions/0007_runs.py`; `control-plane/tests/isolation.py`.
- Tests: `commons/tests/test_ledger_verify_entries.py`, `control-plane/tests/test_ingest.py`, `control-plane/tests/test_body_limit.py`.
- [ ] Steps 1-5; commit `feat(control-plane): per-tenant run event ingest with chain verification and idempotent replay (M2-04)`.

### Task 10: CLI side (M2-01..M2-04 client)

As parent Task 10, with R18, R19, DF-182 (control-plane half), DF-184, DF-203, DF-377, DF-291.
- Files: `cli/src/agentic_cli/control_plane.py`, `commands/login.py`, `policy_source.py`, `workspace.py`, `commands/init.py`, `dev_init.py`, `repo_guard.py`, `publishing.py`.
- Tests: `cli/tests/{cp_fakes.py, test_control_plane_client.py, test_login_control_plane.py, test_policy_from_control_plane.py, test_init_org_rbac.py}`, `cli/tests/run/` link case.
- [ ] Steps 1-5; commit `feat(cli): control-plane client - login, policy bundle, org roles, repo checks (M2-01..M2-04)`.

### Task 10b: Control-plane sink, offline queue, replay (M2-20)

- Files: `core/src/agentic/telemetry/sinks.py` (DF-172 cursor-hash check, DF-179 fsync and empty cursor, `SinkBehind` rewind R21, `ForwardingRecorder(background=True)` R20), `cli/src/agentic_cli/control_plane.py` (`ControlPlaneSink`), `cli/src/agentic_cli/runtime.py` (sinks per run, `flush_sinks`), `cli/src/agentic_cli/commands/runs.py` (`agentic runs sync [RUN_ID]`).
- Tests: `core/tests/telemetry/test_sinks_*` new cases, `cli/tests/test_control_plane_sink.py`, `cli/tests/run/test_runs_sync.py`.
- [ ] Steps 1-5; commit `feat(cli): control-plane sink with offline queue and idempotent replay (M2-20)`.

### Task 11: Entry points, contract tests, verification, review

As parent Task 11.
- Files: `control-plane/src/agentic_cp/__main__.py` (`worker`, `serve` names `AGENTIC_CP_REQUIRE_DB_ENFORCEMENT` and exits 2 on refusal, `migrate` prints the enforcement status), `control-plane/tests/test_cp_main.py`, `control-plane/tests/contract/test_cli_against_control_plane.py` (uvicorn in a thread on `127.0.0.1:0`, two tenants: login/me, policy fetch end to end, ledger forward end to end).
- [ ] Step 1: tests. - [ ] Step 2: implement, `uv run pytest control-plane -q`. - [ ] Step 3: `openapi --check`, `lint-imports`, licence check, `check.py --all`, no `fmt:` markers. - [ ] Step 4: high-effort review subagent over slice 1b against the Review Focus; fix or defer. - [ ] Step 5: commit.
