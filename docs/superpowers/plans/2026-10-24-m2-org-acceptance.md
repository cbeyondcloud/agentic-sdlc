# M2 Org Acceptance (M2-53, M2-55) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:**
- **M2-53, Acceptance 3 (org), spec §1.3.** An org admin changes the tier -> model mapping
  and a tool restriction in the tenant's policy repo (a real git repo the control plane syncs
  from). The next run honours both:
  - the model the gateway is asked for changes;
  - the restricted tool is refused at preflight (forbidden while an agent type still grants
    it), then invisible to the agent (taken out of the type);
  - the run record (the run's state, the dry-run plan and the control plane's run) shows the
    new policy version;
  - the routing overlay the control plane generates for TokenLean (M2-09) changes to match,
    against the `FakeRouting` gateway.
- **M2-55, Audit E2E.** On a real (scripted) org run:
  - `agentic audit verify` passes for the local ledger and the control plane's chain;
  - `agentic audit export` writes an export that the standalone verifier
    (`core/src/agentic/auditproof.py`, copied away and run alone) verifies;
  - a tampered local ledger entry and a tampered export row are detected and located (file,
    entry or seq, class of change) with the documented exit codes.
- **Deferred items targeting the rows:**
  - DF-519: org mode with `control_plane_url` removed is refused, not warned, once the person
    signed in to a control plane or holds its policy;
  - DF-653: the CLI-side half is proven here (the tier header the CLI sends equals the value
    the generated rule matches). The TokenLean half needs a running TokenLean and is
    retargeted.

**Architecture:**

- **The pattern is M1's** (`cli/tests/acceptance`). The real `agentic` CLI runs in process on
  the sample repo. The cassette server stands where TokenLean stands: it replays recorded
  model turns, and `scripts/record_acceptance.py` records them from a `ScriptedUpstream`, so
  no model is called. GitHub is `FakeGitHub`. Cassettes live in `cassettes/m2-53/` and
  `cassettes/m2-55/`, each with its prompt fingerprint.
- **The org stack runs in process** (`cli/tests/acceptance/_org_stack.py`, `OrgStack`):
  - the merged control plane app (`agentic_cp.app.create_app`, unchanged) is served by uvicorn
    on a loopback port, in a thread;
  - the database is a fresh one migrated to head, with forced RLS and the runtime role, on the
    Postgres the test fixtures already use (`control-plane/tests/pg.py`): the Linux CI
    sidecar (`AGENTIC_CP_TEST_DSN`, `AGENTIC_CP_REQUIRE_PG=1`), or a throw-away container
    started through docker or podman. With neither, the scenarios skip, naming both;
  - the policy repo is a local git repo (`file://`, allowed by `allow_local_repos` as in the
    control-plane tests);
  - the admin acts through the API with their bootstrap token: `PUT
    /v1/tenant/policy-source`, `POST /v1/rbac/assignments` and `POST /v1/policy/sync`;
  - the routing apply is the worker's own `apply_due`, against `FakeRouting`;
  - users get tokens as `python -m agentic_cp admin token issue` gives them (owner role). The
    developer signs in with `agentic login --for control-plane --key-stdin`, so the token is
    verified against `/v1/me` for real.
- **CLI change 1: an org run follows the tenant's active policy**
  (`policy_source.follow_active_policy`, called by `agentic run`). Today the cached policy
  changes only at `agentic init`, so an admin's change would wait up to
  `policy_cache_hours`. In org mode with a control plane, `run` (live and `--dry-run`) asks
  `/v1/policy/active`. When the active version differs from the cached one, it fetches,
  verifies and swaps in the new bundle exactly as init does, and says so (a notice). When the
  control plane cannot be asked, nothing changes here: the org checks refuse the run as
  before (ruling R6).
- **CLI change 2 (DF-519)** (`org_preflight.org_checks`). Org mode without a control-plane URL
  stays a warning only for an org that never had a control plane. When the person has a
  control-plane login record, or the cached org policy came from a control plane, it is a
  preflight error: removing the URL no longer skips RBAC, the kill switch, caps and the
  minimum version.
- **CLI change 3 (found while writing the scenario):** the run summary (`run`, `status`,
  `approve`... with `--json`, and the human view) did not show the run's policy version,
  although the run state holds it. It now does (`policy_version`; a `policy <version>`
  line), so the person's own run record shows it, as §1.3 asks.
- **No control-plane, migration or table change.** Nothing new needs RLS, an `EndpointCase`
  or an Alembic revision (the head stays `m2_37_usage_views`).

**Tech Stack:** Python 3.12, pytest, the workspace's existing uvicorn, httpx, asyncpg and
SQLAlchemy (control-plane deps, used only by tests here). No new dependency.

**Spec:** §1.3 (scenario 3), §11.3 (policy flow), §14.5 (audit), §8.2 and §8.6 (org checks,
"cannot confirm, pauses"). Rows M2-53 and M2-55 and their Notes in TRACKER.md.

## OSS reuse decisions

| Need | Reused | Not reused, why |
|---|---|---|
| Serving the control plane in a test | uvicorn (already pinned, BSD-3) `Server.run(sockets=...)` in a thread | docker compose (`deploy/org-local`): too heavy for CI and the Windows host; it needs a TokenLean checkout with two unmerged branches |
| A Postgres for the stack | the control-plane test fixtures' `pg.py` (sidecar DSN or throw-away container) | testcontainers: another dependency for what `pg.py` already does |
| The gateway | the cassette server (M1-63) for model calls, `FakeRouting` for the routing admin API | TokenLean LITE `--mock`: it needs the TokenLean checkout and a network service; owner decision 73 forbids live calls |
| Export verification | `agentic.auditproof` run as a copied, standalone script | — |

## Rulings (logged for owner review)

- **R1 (stack):** the control plane runs in process (uvicorn thread) against the fixtures'
  Postgres, never `docker compose`. It runs end to end in the Linux stage (sidecar,
  `AGENTIC_CP_REQUIRE_PG=1`, so a missing database fails there) and on Windows or macOS when
  docker or podman is present; otherwise the scenarios skip with the reason.
- **R2 (roles):** the stack's Postgres roles are `acc_owner` and `acc_app`, not the
  control-plane tests' `cp_owner` and `cp_app`. Role passwords are cluster-wide, so sharing
  the names would break the control-plane tests' session template on the shared CI server.
- **R3 (sync trigger):** the admin's manual sync (`POST /v1/policy/sync`) stands for the
  webhook or the worker's poll. All three call the same `sync_once`.
- **R4 (policy follow):** `agentic run` refreshes a stale control-plane policy cache by
  itself rather than refusing with "run agentic init". Governance takes effect at the next
  run, and the person has nothing extra to do.
- **R5 (DF-519):** "signed in once or holds a control-plane policy" is the trigger for the
  refusal. An org that never had a control plane keeps the M1 behaviour (warning). Deleting
  `~/.agentic/control-plane.json` and the policy cache still gets back to the warning: the
  client can never be the last line. The gateway is, through key revocation, routing and the
  M2-30 deny rules.
- **R6 (tool restriction):** shown both ways the rules allow. `tools.yaml: forbidden` with
  the type still granting the tool refuses the run at preflight (nothing registered, no
  model call). Removing the tool from the agent type makes it invisible (the gateway sees no
  such function in the request).

## Global Constraints

- No live model, provider, TokenLean or Jira call: the cassette server, `FakeRouting`,
  `FakeGitHub` only.
- Nothing reads the developer's real home, keyring or network settings. The empty keyring
  fixture and the cleared proxy variables of `cli/tests/conftest.py` apply.
- Windows and macOS laptops, Linux CI. Paths are built with `pathlib`. Policy files are
  written with LF bytes. The uvicorn thread is stopped by `should_exit` (never by killing
  processes). Git runs with a fixed identity and `core.autocrlf=false`.
- Fake secrets are built from pieces or marked `# gitleaks:allow`.
- Never add `# fmt: skip` or `# fmt: off`. Line length 100. Check pytest exit codes directly.
- TRACKER.md, DEFERRED.md and DECISIONS-*.md are not edited: the coordinator applies the
  structured output.

## Review Focus

1. **The next run honours the admin's change without the developer re-running init.** The
   cached policy is replaced only by a bundle whose digest and tree digest verify, and a
   control plane that cannot be asked never swaps or deletes the cache. Tests:
   `test_a_run_follows_a_newer_active_policy`,
   `test_an_unreachable_control_plane_leaves_the_cache`,
   `test_a_tampered_newer_bundle_is_refused_and_the_cache_kept` (Task 1); the M2-53 scenario.
2. **DF-519: removing `control_plane_url` no longer skips the org checks** for someone who
   signed in, while an org without a control plane keeps working with the warning. Tests:
   `test_a_signed_in_person_cannot_skip_the_org_checks_by_removing_the_url`,
   `test_org_mode_without_a_control_plane_warns_that_org_checks_were_skipped` (Task 2).
3. **The scenario proves what it claims, from the outside.**
   - Models: from the request bodies the gateway received (`model`, the `tools` function
     names, the `X-Agentic-Tier` header), not from the CLI's own report.
   - Policy version: from the control plane's run record and the ingested events.
   - Routing: from the overlay `FakeRouting` holds after the real `apply_due`.
   Test: `test_admin_override_on_the_org_stack` (Task 4).
4. **Tamper detection is located and uses the documented exit codes.** CLI verify: 0 intact,
   3 broken, 1 unreadable. `auditproof.py`: 0, 3, 1, 64. The tampered entry is named by
   file, entry, seq and class. The verifier is run as a copied file with `python -I`, so it
   imports nothing of ours. Test: `test_audit_verify_and_export_on_an_org_run` (Task 5).
5. **The stack is the merged control plane, unchanged:** forced RLS with the runtime role
   (`/readyz` reports `db_enforced: true`), per-tenant audit chain and tenant-bound tokens.
   Nothing is stubbed between the CLI and the API but the network address. Test:
   `test_the_org_stack_is_the_real_control_plane` (Task 3).

---

### Task 1: An org run follows the tenant's active policy (M2-53 prerequisite)

**Files:**
- Modify: `cli/src/agentic_cli/policy_source.py`, `cli/src/agentic_cli/commands/runs.py`
- Modify (test helper): `cli/tests/cp_fakes.py` (`FakeControlPlane.version`)
- Test: `cli/tests/test_policy_from_control_plane.py`

**Interfaces:**
- `follow_active_policy(home: Path, config: dict, env: Mapping[str, str]) -> tuple[str, str] | None`:
  `(old, new)` when the control-plane cache was replaced by the tenant's active version.
  None otherwise: not org mode, no control plane, no control-plane cache yet (init has not
  run), already current, or the control plane cannot be asked (the org checks refuse then).
  A bundle that fails its digest or tree digest raises `CliError` (exit 2) and leaves the
  cache as it was.
- `agentic run` calls it before loading the workspace. A replacement prints a notice
  `{"policy": {"from": old, "to": new}}` ("org policy updated to <new> (was <old>)").

- [ ] **Step 1: Failing tests.**
  - `test_a_run_follows_a_newer_active_policy`: init, then the fake's version and a policy
    file change; `follow_active_policy` returns `(old, new)`; the cache holds the new file
    and `fetched.json` the new version.
  - `test_a_current_cache_is_not_fetched_again`: no bundle request.
  - `test_an_unreachable_control_plane_leaves_the_cache`.
  - `test_a_tampered_newer_bundle_is_refused_and_the_cache_kept`.
  - `test_no_control_plane_cache_means_nothing_to_follow`.
- [ ] **Step 2: Run** `uv run pytest cli/tests/test_policy_from_control_plane.py -q`. Expected:
  FAIL (no `follow_active_policy`).
- [ ] **Step 3: Implement**, then wire it into `run`.
- [ ] **Step 4: Run.** Expected: PASS. Also run `cli/tests/run`.
- [ ] **Step 5: Commit.** `feat(cli): an org run follows the tenant's active policy (M2-53)`

### Task 2: DF-519, removing the control-plane URL does not skip the org checks

**Files:**
- Modify: `cli/src/agentic_cli/org_preflight.py`
- Test: `cli/tests/run/test_org_preflight.py`

**Interfaces:**
- `org_checks`: when `client is None` (no URL) and
  `control_plane.login_record(home)` exists or the org cache's `fetched.json` is a
  control-plane stamp, it adds an error `Problem(None, "org", ...)` that names where the
  policy came from and how to fix it. Otherwise it keeps the `SKIPPED` warning.

- [ ] **Step 1: Failing tests:**
  `test_a_signed_in_person_cannot_skip_the_org_checks_by_removing_the_url` (login record);
  `test_a_control_plane_policy_cannot_be_run_without_its_control_plane` (stamp only). The
  existing warning test stays green.
- [ ] **Step 2: Run.** Expected: FAIL (a warning, the run starts).
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `uv run pytest cli/tests/run/test_org_preflight.py -q`. Expected: PASS.
- [ ] **Step 5: Commit.** `fix(cli): org mode cannot skip the org checks by removing the URL (DF-519)`

### Task 3: The in-process org stack

**Files:**
- Create: `cli/tests/acceptance/_org_stack.py`
- Modify: `cli/tests/acceptance/conftest.py` (fixtures `pg_server` for the session and `org`
  per test); `cli/tests/acceptance/_acceptance_support.py` (`Scenario.models` takes a
  `watch` server class; `WatchedCassetteServer` keeps each request's model, tool names and
  identity headers)
- Test: `cli/tests/acceptance/test_accept_org_stack.py`

**Interfaces:**
- `OrgStack(server, tmp)`:
  - `start()` makes the database (`acc_owner` and `acc_app`, migrate, `grant_runtime`),
    serves `create_app(settings)` and bootstraps tenant `acme` (admin token);
  - `stop()`;
  - `url`, `tenant_id`, `admin_token`;
  - `api(method, path, token, body)` returns an httpx response;
  - `user(email, role, team=None) -> token`;
  - `policy_repo(path)` (assembled from the standalone template: real model names,
    `allowed_repo_patterns`, team `payments`, the scenario pipeline), `commit(message)`,
    `connect_policy()` and `sync() -> SyncOutcome dict`;
  - `gateway_tenant(name)`;
  - `apply_routing(fake) -> ApplyReport`, which runs `apply_due` with a runtime-role engine;
  - `run_record(run_id, token)` and `run_events(run_id, token)`.
- Settings: `allow_local_repos`, a policy workdir under `tmp`, gateway admin URL and key set
  (so activations queue routing applies), `require_db_enforcement` left on.

- [ ] **Step 1: Failing test** `test_the_org_stack_is_the_real_control_plane`: `/readyz` is
  ready with `db_enforced: true`; the admin's `/v1/me`; a policy sync activates `git:<sha>`;
  another tenant's token is a 404 on the run record of this tenant's run id.
- [ ] **Step 2: Run.** Expected: FAIL (no `_org_stack`).
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run.** Expected: PASS (or SKIP naming the missing database).
- [ ] **Step 5: Commit.** `test(acceptance): the in-process org stack (M2-53, M2-55)`

### Task 4: Acceptance 3 (org): the admin override (M2-53)

**Files:**
- Create: `cli/tests/acceptance/test_accept_org_policy.py`,
  `cli/tests/acceptance/cassettes/m2-53/acceptance/{1.json,prompts.sha256}` (recorded)
- Modify: `cli/tests/acceptance/test_accept_cassette_drift.py` (SCENARIOS)

**Story** (the org pipeline `org-plan` is business-analyst, qa-baseline, design: the
control plane's own validation refused a design step without an earlier test report):
1. The admin connects the policy repo and syncs it (v1). Routing is applied:
   `FakeRouting` holds the tier rule `balanced -> acme/balanced-model`.
2. The developer signs in, runs `agentic init` (the bundle from the control plane) and
   `agentic run org-plan` (business-analyst, then design). The gateway is asked for
   `acme/balanced-model` with the `search` function present and `X-Agentic-Tier: balanced`,
   the value the rule matches (DF-653, CLI half). The control plane's run record and every
   ingested event carry v1.
3. The admin commits v2: `balanced -> acme/balanced-model-v2` and
   `tools.yaml: forbidden: [search]`, then syncs. `agentic run --dry-run` follows v2 (a
   notice) and is refused at preflight: `search` is forbidden by org policy. The live run is
   refused too, before any model call, with nothing registered (registration is not an
   audit event, so this is checked as a 404 on the refused run's id).
4. The admin commits v3, which takes `search` out of the business-analyst and design types,
   and syncs. Routing is applied again: the overlay now pins `acme/balanced-model-v2`, and
   the gateway version moves on. The next run completes. The gateway is asked for the v2
   model, with no `search` function. The run's state, its dry-run plan and the control
   plane's run record show v3. The audit chain holds `policy.activated` for each version and
   `routing.applied` for v1 and v3.

- [ ] **Step 1: Write the scenario** with its script. Run it in replay. Expected: FAIL (no
  cassette).
- [ ] **Step 2: Record** with `uv run python scripts/record_acceptance.py -k org_policy`, then
  replay. Expected: PASS.
- [ ] **Step 3: Commit.** `test(acceptance): admin override on the org stack (M2-53)`

### Task 5: Audit E2E (M2-55)

**Files:**
- Create: `cli/tests/acceptance/test_accept_org_audit.py`,
  `cli/tests/acceptance/cassettes/m2-55/acceptance/{1.json,prompts.sha256}`

**Story:** a scripted org run (`org-plan`) completes. An auditor (role `auditor`) signs in.
1. `agentic audit verify` (local) and `agentic audit verify --control-plane` exit 0, intact.
   The chain holds the run's registration and policy records.
2. `agentic audit export -o org.jsonl` and `--format csv` exit 0 and say intact. The
   standalone verifier, copied to a fresh folder and run with `python -I auditproof.py`,
   exits 0 on both.
3. A changed `events.jsonl` entry: `agentic audit verify <run>` exits 3, naming
   `events.jsonl`, the entry, its seq and `content`.
4. A changed export row: `auditproof.py` exits 3, naming the seq and `content`.
   `agentic audit verify --export` exits 3 with the same finding. The CSV export, a cut
   export (no proof line) and a missing file give 3, 3 and 1. A call with no file gives 64.

- [ ] **Step 1: Write the scenario.** Run it in replay. Expected: FAIL (no cassette).
- [ ] **Step 2: Record**, then replay. Expected: PASS.
- [ ] **Step 3: Commit.** `test(acceptance): audit verify and export on an org run (M2-55)`

### Task 6: Full check

- [ ] `uv run ruff check . && uv run ruff format --check .`, and grep the touched files for
  `fmt: skip`.
- [ ] `uv run python scripts/check.py --all` (Windows native and Linux container) until green.
  Report its final lines.

### Task 7: Final review fixes

- F3 (re-graded Important: the run record must name the policy every step used, and the
  follow made a mismatch routine). `Runtime.check_same_policy` refuses `resume`, `approve`,
  `reject` and `edit` (before the editor opens) of a run whose recorded policy version is
  not the cached control-plane policy's version: exit 2, naming both versions, with the
  hint to stop and start again. `stop` still works. Only with a control-plane policy cache
  (`Workspace.control_plane_policy`); git and standalone policies keep their behaviour.
  Tests: `cli/tests/run/test_org_preflight.py` (`*_once_the_org_policy_moved_on`,
  `test_a_paused_run_on_the_current_policy_continues`).
- F1 (tracker text says "compose org stack"): owner decision, recorded for the coordinator;
  no code change. F2, F4-F7: Minor, returned as deferred items.
