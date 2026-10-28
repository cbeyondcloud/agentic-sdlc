# M2 Routing Generation and Personal Sync Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:**
- **M2-09.** When a tenant's policy version is accepted, the control plane generates that tenant's
  part of TokenLean's routing overlay and applies it through TokenLean's `/admin/routing` API
  (ST-10, TokenLean PR #57). The part covers tier -> model routing per tenant and team, team
  overrides, the classifications' allowed tiers and models, team monthly budgets and team rate
  limits. The same policy always gives byte-identical bytes, pinned by a fixture. Every apply
  sends `expected_version`. It runs as an outbox job with retry and backoff. It records which
  gateway version applied which policy version, is audited, and is verified by reading it back.
- **M2-20 (rest).** Personal skills and notes (`~/.agentic/skills`, `~/.agentic/memory`, M2-45)
  are backed up to the control plane. Each file change becomes a chained event in a local
  journal, and the existing sink machinery delivers the journal: `forward` plus a durable
  cursor are the offline queue and give idempotent, in-order replay. The publish scan (M2-49)
  checks every file before it leaves the laptop. Each user sees only their own backups, and
  `agentic skills restore` brings them back.
- **Deferred items** targeting M2-09 and M2-20 are closed where they are small. Every other one
  is retargeted with a reason.

**Architecture:**

- **Generation** (`control-plane/src/agentic_cp/routing/generate.py`, pure). `routing_plan(policy)
  -> RoutingPlan(tenant, per_team, warnings)`:
  - `tenant` is the subtree for `tenants.<gateway_tenant>`. It holds `groups.G6_routing.rules`,
    a deterministic list of G6 rules, and `teams.<team>.monthly_spend_cap_usd`.
  - `per_team` holds the team request limits for `rate_limit.per_team`.
  - It is computed when a version is accepted (`policy_sync._build`) and stored in
    `policy_versions.settings["routing"]`. An older stored version is derived from its bundle.
  - `canonical(plan)` is `json.dumps(sort_keys=True, separators=(",", ":"), ensure_ascii=False)`.
    The fixture test pins these bytes.
- **Rules.** Policy tiers have free names (`balanced`, `deep-reasoning`). TokenLean's
  `G6_routing.tiers` are its own complexity levels (`simple|medium|complex`). So the mapping is
  expressed as G6 `rules` that match the request headers the CLI already sends (spec §5.3: "the
  request still carries `X-Agentic-Tier`, and TokenLean routing rules remain the enforcement
  point"). Each rule pins `action.model` with `allow_escalation: true`, so the policy's model is
  served rather than the requested one. Priorities, highest first:
  - 400 class + team + tier;
  - 350 class + team fallback;
  - 300 class + tier;
  - 250 class fallback;
  - 200 team + tier (team override);
  - 100 tier.

  Class rules are generated only for a *restrictive* class: one that leaves out a tier, or that
  sets `allowed_models`. Their fallback pins the class's first allowed model, so a request of
  that class that names a disallowed tier, or no tier, still gets an allowed model. Team rules
  match `x_agentic_team`. They sit in the tenant list because the operator's
  `tenants.<id>.groups` overlay replaces a team overlay's `rules` list wholesale (TokenLean's
  merge order). Rule ids are `agentic.<kind>.<names>`, or a 16-hex digest when that does not fit
  `^[A-Za-z0-9._-]{1,64}$`.
- **Gateway port** (`routing/gateway.py`). `GatewayRouting.read() -> RoutingState(version,
  fragment)`, `read_version(v)` and `write(fragment, expected_version, comment) -> RoutingState`.
  - `TokenLeanRouting` uses httpx2 and the deployment's `gateway_admin_url` and
    `gateway_admin_key` (a SecretStr, never in a repr, log line or error).
  - A 409 raises `RoutingConflict`. A 422 raises `RoutingRefused(errors)`. Everything else
    raises `GatewayUnavailable`.
  - `NoGatewayRouting` is used when no gateway admin is configured.
  - Tests use `httpx2.MockTransport` and a `FakeRouting` that keeps a versioned in-memory
    overlay, with TokenLean's allowed paths and its `expected_version` rule.
- **Outbox and job** (`routing/apply.py`):
  - `_activate` (the policy activation transaction) supersedes the tenant's pending
    `routing_applies` rows and queues one for the new version (`routing.queued` audited).
  - `routing_job`, a worker `EXTRA_JOBS` entry, leases the newest due row in a short
    transaction. It then calls the gateway with no transaction open (review F2 of M2-17: no
    tenant lock is held across a network call). It reads, replaces only `tenants.<gw>` (and,
    when enabled, its own `rate_limit.per_team` entries), and writes with `expected_version`. A
    409 means it reads again and retries, up to 3 times. It then reads back and compares. A
    second short transaction records the outcome and audits it:
    - `routing.applied`, with the policy version, gateway version, previous gateway version and
      the subtree's sha256;
    - `routing.refused` on a 422 (terminal);
    - `routing.apply_failed` after the last attempt.
  - Backoff is 1, 2, 4 ... 60 minutes. `POST /v1/routing/apply` queues the active version
    again. `GET /v1/routing` lists the apply records.
- **Personal backup, client side** (`cli/src/agentic_cli/personal_sync.py`):
  - The journal is `~/.agentic/sync/personal/journal.jsonl`. It is append-only and chained with
    `sdlc_commons.ledger.entry_hash` (Zyroflo's formula), and a `filelock.FileLock` guards it.
  - Beside it are `state.json` (path -> sha256 journaled), `device.json` (a random device id)
    and cursors per control plane, tenant and user.
  - `record_changes(home, guard)` walks `skills/` and `memory/` (text files only, junk skipped).
    For each changed or removed file it appends `personal.saved` (with content),
    `personal.removed`, or `personal.held` (a publish-scan hit: no content, findings counts
    only).
  - `PersonalSink` is an `EventSink`. `agentic.telemetry.sinks.forward` delivers the journal in
    order from the cursor.
  - `restore(home, files, force)` writes the latest copy of each file under the right folder.
    It never overwrites a different local file without `--force`.
- **Personal backup, control plane** (`agentic_cp/personal/`):
  - Table `personal_events`, insert-only, under forced RLS, keyed by (tenant, user, device,
    seq).
  - `POST /v1/personal/devices/{device}/events` has the same semantics as run ingest:
    duplicates are no-ops, a gap or a fork gives 409 with the head, and a broken chain gives
    422.
  - `GET /v1/personal/files` returns the caller's latest copy of each path across devices.
  - Both need `skills.personal.edit` at some scope (never a service principal). Each accepted
    event appends `personal.saved` / `.removed` / `.held` to the tenant's audit chain, with its
    path and sha256 and never its content. Rows are filtered by the caller's user id, so no
    other user, admins included, reads the content.
- **CLI commands.**
  - `agentic skills backup` records changes and sends the journal.
  - `agentic skills restore [--force]`.
  - `agentic sync` also delivers the personal journal when a control plane is configured.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy Core and Alembic, httpx2 (already a
dependency), filelock (already a core dependency), pytest with Postgres in Docker. No new
dependencies.

**Spec:**
- `docs/superpowers/specs/2026-10-16-agentic-sdlc-design.md` §5.3 (tiers; routing rules are the
  enforcement point), §12 (policy flow: "generates TokenLean routing rules"), §14.2 (identity
  headers), §24 (personal skills synced for backup and audit) and §13 (ST-10).
- TokenLean `docs/config-reference.md` at origin/main c743e01 ("/admin/routing", "teams",
  "G6_routing" rules, "rate_limit"), read with `git show` only.

## OSS reuse decisions

| Need | Reused | Not reused, why |
|---|---|---|
| HTTP client for `/admin/routing` | httpx2 (as `keys/gateway.py`) | a TokenLean SDK: there is none, and vendor SDKs stay out of the engine |
| Optimistic concurrency, versioning, audit at the gateway | TokenLean's own `/admin/routing` (expected_version, versions, history, rollback) | a control-plane copy of the overlay history: TokenLean already keeps it |
| Outbox, retry and backoff | the repo's notify outbox pattern and `notify.delivery.backoff` | a task queue (Celery, arq): one more service for a 1000-person org's handful of jobs |
| Chain hash for the personal journal | `sdlc_commons.ledger.entry_hash` (Zyroflo's formula) | a new hash scheme: one verifier for every chain |
| Offline queue and idempotent replay | `agentic.telemetry.sinks.forward` and its durable cursor (M2-20) | a second queue |
| Cross-process lock on the journal | `filelock` 4.2.1 (core dependency, Unlicense) | hand-rolled `msvcrt`/`fcntl` |
| Deep merge of the overlay | none needed: the control plane replaces whole subtrees it owns | `deepmerge` (MIT): merging would keep stale rules |

## Global Constraints

- **Ownership of the global overlay.** The fragment is one document for every tenant. A
  tenant's apply replaces only `tenants.<gateway_tenant>`. It also replaces the
  `rate_limit.per_team.<team>` entries that this tenant's last applied record says it wrote,
  and only when team rate limits are enabled. Global `groups`, global `teams` and other tenants
  are never changed. Every PUT carries `expected_version`.
- **Determinism.** Rule order, keys and numbers depend only on the policy:
  - teams, tiers and classes are sorted;
  - a class's allowed tiers keep the order the policy gives them;
  - money is written as a JSON number (an integer when whole);
  - the canonical serialisation uses sorted keys and fixed separators.
- **Gateway model names.** A tier model `route/name` reaches TokenLean as `name`. The same
  helper (`agentic.policy.gateway_model`) serves the client and the generator, so the two never
  disagree (DF-394 convention).
- **No live gateway.** Tests use `httpx2.MockTransport` and `FakeRouting` only (owner decision
  73). The admin key is a SecretStr. A test checks that it is never in a log record, an error,
  a repr or an audit record.
- **Tenant isolation.** `routing_applies` and `personal_events` are under forced RLS, with
  isolation factories. Every new route has an `EndpointCase`, and another tenant's objects give
  404. Personal rows are also filtered by user.
- **Windows and macOS.** Journal paths use POSIX relative names (`skills/<name>/SKILL.md`,
  `memory/<note>.md`). Content is hashed as stored, with no line-ending changes, so a restore
  gives the same bytes. The journal is written with `newline="\n"` and fsynced, and the lock is
  a `FileLock`.
- **Formatting and CI:**
  - never add `# fmt: skip` or `# fmt: off`; line length 100;
  - check pytest's exit code directly;
  - `uv run python scripts/check.py --all` must be green;
  - TRACKER.md, DEFERRED.md and the DECISIONS files are updated by the coordinator from this
    track's output.

## Rulings (for owner review)

| Problem | Alternatives | Decision | Why |
|---|---|---|---|
| Policy tier names do not match TokenLean's `simple/medium/complex` tiers | map tiers onto the three levels; add a `gateway_tier` field; use G6 rules | G6 `rules` matching `x_agentic_tier` with `action.model` | Exact for any number of tiers. The CLI already sends the header (spec §5.3). |
| Team rules under `tenants.<id>.teams.<t>.config` are replaced by the operator's tenant rules | team overlays; tenant-level rules matching `x_agentic_team` | tenant-level rules | TokenLean applies the operator's `tenants.<id>.groups` after a team overlay, and a `rules` list replaces wholesale |
| `rate_limit.per_team` is keyed by team name across tenants | always write; never write; write behind a deployment switch, never overwriting another writer's entry | the switch `AGENTIC_CP_GATEWAY_TEAM_RATE_LIMITS` (default off). When it is off, the limits are reported as not applied. | Tenant A's `payments` limit would otherwise apply to tenant B's `payments` team. Needs decision, plus a TokenLean item for a tenant-scoped `per_team`. |
| The gateway cannot tell a request's repo classification | skip class rules; add an 11th identity header | the CLI sends `X-Agentic-Classification` (optional, like tier) | A restricted repo's requests are pinned to an allowed (local) model at the gateway too |
| Personal content through `/v1/runs` | pseudo-runs; a new endpoint pair | `/v1/personal/...` | Runs are team-visible, take concurrency slots and verify Zyroflo's run-event schema. Personal content must be owner-only. |
| A publish-scan hit in a personal file | mask as for runs; hold back | hold back (`personal.held`, findings counts only), under either action | A masked copy is not a restorable backup, and a secret must not leave the laptop |
| Several laptops per user | one chain per user; one per (user, device) | per device; a restore takes the newest copy per path by server order | Two laptops never fork one chain |

## Review Focus

1. **Determinism and fixture pinning.** The same policy gives byte-identical canonical bytes
   across runs, dict orders and operating systems. Tests: Task 2
   `test_plan_matches_the_pinned_fixture` and `test_plan_ignores_mapping_order`.
2. **Never clobbering others.** An apply keeps other tenants' subtrees, global groups, global
   teams and another writer's `per_team` entries exactly as they were, and always sends
   `expected_version`. A 409 is retried from a fresh read. Tests: Task 4
   `test_apply_replaces_only_its_tenant`, `test_conflict_rereads_and_retries` and
   `test_per_team_never_overwrites_an_unowned_entry`.
3. **No lock across the network, and outbox semantics.** Gateway calls run with no tenant
   transaction open (an audited RBAC write goes through while the gateway hangs). The outcome is
   recorded once. A newer version supersedes a pending one. A 422 is terminal and audited.
   Backoff applies, and the last attempt fails audibly. Tests: Task 4
   `test_job_holds_no_tenant_lock_while_the_gateway_waits`, `test_invalid_fragment_is_terminal`,
   `test_backoff_then_failed`, `test_newer_activation_supersedes_pending` and
   `test_readback_mismatch_is_not_applied`.
4. **The admin key never leaks** into a log record, an exception message, a repr or the audit
   chain. Test: Task 3 `test_admin_key_never_appears`.
5. **Personal backup privacy and order.**
   - Another user in the same tenant, an org admin included, never sees a user's content, and
     another tenant's device is unknown.
   - Journal events queued offline are delivered once and in order on reconnect, and a replay
     is a no-op.
   - A file holding a secret never leaves the laptop.
   - `restore` cannot write outside `skills/` or `memory/`.

   Tests: Task 6 `test_other_users_never_see_content`; Task 7
   `test_offline_events_replay_once_in_order`, `test_secret_file_is_held_back` and
   `test_restore_refuses_escaping_paths`.

---

### Task 1: Policy and identity additions (core)

**Files:**
- Modify: `core/src/agentic/policy/model.py` (`TeamRateLimit`, `TeamPolicy.rate_limit`,
  `gateway_model`), `core/src/agentic/policy/__init__.py` (export),
  `core/src/agentic/agents/models.py` (use `gateway_model`),
  `core/src/agentic/transport/identity.py` (`classification`, `X-Agentic-Classification`),
  `cli/src/agentic_cli/runtime.py` (pass the agent's classification)
- Test: `core/tests/policy/test_team_rate_limit.py`, `core/tests/transport/test_identity.py`
  (additions)

**Interfaces:**
- `TeamRateLimit(requests_per_minute: int | None >= 1, requests_per_hour: int | None >= 1)`,
  with at least one of the two set.
- `gateway_model(model_id: str) -> str`: the part after the first `/`, or the whole id.
- `CallIdentity.classification: str | None = None`, sent as `X-Agentic-Classification`.

- [ ] Write failing tests:
  - a team `rate_limit` with neither field is refused, and with one field it is kept;
  - `gateway_model("tokenlean/openrouter/x") == "openrouter/x"` and `gateway_model("m") == "m"`;
  - identity headers include the classification only when it is set.
- [ ] Implement, run, and commit.

### Task 2: Deterministic routing generation (M2-09)

**Files:**
- Create: `control-plane/src/agentic_cp/routing/__init__.py`, `.../routing/generate.py`
- Test: `control-plane/tests/test_routing_generate.py`; fixtures
  `control-plane/tests/fixtures/routing/policy/` (a policy tree) and
  `control-plane/tests/fixtures/routing/expected.json`

**Interfaces:**
- `RoutingPlan(tenant: dict, per_team: dict[str, dict], warnings: list[str])`, with
  `to_settings()` and `from_settings()`.
- `routing_plan(policy: OrgPolicy) -> RoutingPlan`.
- `canonical(value) -> bytes`.
- `plan_digest(plan) -> str` (`sha256:` of the canonical tenant subtree and per_team).

- [ ] Failing tests:
  - pinned fixture bytes;
  - mapping order does not matter (policy dicts rebuilt reversed);
  - a restrictive class pins its fallback;
  - an unrestrictive class makes no class rules;
  - a team override wins over the org tier for that team only;
  - a team override not allowed in a class is left out of that class;
  - a long or odd name gets a digest id that fits TokenLean's pattern;
  - a whole budget is an int and a fractional one a float;
  - a class that allows no model gives a warning.
- [ ] Implement, then write the fixture once from reviewed output (checked by hand against the
  rules above), run, and commit.

### Task 3: Gateway routing port (httpx2 and fake)

**Files:**
- Create: `control-plane/src/agentic_cp/routing/gateway.py`, `control-plane/tests/fake_routing.py`
- Modify: `control-plane/src/agentic_cp/settings.py` (`gateway_team_rate_limits`,
  `routing_max_attempts`)
- Test: `control-plane/tests/test_routing_gateway.py`

**Interfaces:**
- `RoutingState(version: int, fragment: dict)`.
- Errors: `RoutingConflict(current_version)`, `RoutingRefused(errors: list[str])` and
  `GatewayUnavailable` (reused from `keys.gateway`).
- `GatewayRouting` (Protocol): `configured`, `read()`, `read_version(v)`,
  `write(fragment, *, expected_version, comment)`.
- `TokenLeanRouting(admin_url, admin_key, transport=None)`, `NoGatewayRouting`,
  `gateway_routing_for(settings)`.

- [ ] Failing tests with `httpx2.MockTransport`:
  - GET and PUT shapes, with the bearer header carrying the key;
  - 409 gives `RoutingConflict`;
  - 422 gives `RoutingRefused` with `path: message` items;
  - 403 gives `GatewayUnavailable`, naming the unbound admin key;
  - 5xx, a network error and non-JSON give `GatewayUnavailable`;
  - the key is not in the error text, the repr or the log records (`caplog`).
- [ ] Implement, run, and commit.

### Task 4: Outbox, worker job, read-back and audit (M2-09)

**Files:**
- Create: `control-plane/src/agentic_cp/routing/apply.py`,
  `control-plane/src/agentic_cp/migrations/versions/m2_09_routing.py`
- Modify: `control-plane/src/agentic_cp/tables.py` (`routing_applies`, `TENANT_TABLES`),
  `policy_sync/service.py` (store the plan; queue on activation), `policy_sync/worker.py`
  (`EXTRA_JOBS`), `app.py` (`state.gateway_routing`),
  `control-plane/tests/isolation.py` (factory)
- Test: `control-plane/tests/test_routing_apply.py`

**Interfaces:**
- `routing_applies` columns:
  - `tenant_id`, `id`, `policy_version` (FK to `policy_versions`);
  - `status` (pending | applied | invalid | failed | superseded | skipped);
  - `attempts`, `next_attempt_at`, `last_error`, `problems text[]`;
  - `plan_sha256`, `gateway_version`, `previous_gateway_version`, `per_team text[]`;
  - `created_at`, `applied_at`, `requested_by`.
- `queue_apply(conn, version, *, actor, actor_kind, clock) -> UUID`.
- `apply_due(engine, tenant_id, gateway, settings, clock) -> ApplyReport | None`.
- `routing_job(engine, git, tenant_id, settings, clock)`.

- [ ] Failing DB tests (FakeRouting):
  - an activation queues one row;
  - the job applies it, records the gateway version and audits `routing.applied`;
  - other tenants' subtrees and global keys are untouched;
  - a 409 from a concurrent writer is re-read and succeeds;
  - a 422 is `invalid` and audited, with no retry;
  - an unreachable gateway backs off (1, 2, 4 min) and is `failed` and audited after
    `routing_max_attempts`;
  - a newer activation supersedes the pending row;
  - a read-back mismatch is not marked applied;
  - no gateway tenant gives `skipped`, audited;
  - with `gateway_team_rate_limits` on, per_team entries are written and removed when the team
    goes, and an entry it did not write is left alone with a warning;
  - the job holds no tenant lock while the gateway waits;
  - the admin key is not in the audit chain;
  - migration and metadata match, and the isolation factory exists.
- [ ] Implement, run, and commit.

### Task 5: Routing routes (M2-09)

**Files:**
- Create: `control-plane/src/agentic_cp/routing/api.py`
- Modify: `app.py` (router), `core/src/agentic/orgapi/models.py` and `__init__.py`
  (`RoutingApplyInfo`), `control-plane/tests/isolation.py` (`EndpointCase`s)
- Test: `control-plane/tests/test_routing_api.py`

**Interfaces:**
- `GET /v1/routing?limit=` gives `list[RoutingApplyInfo]`, newest first. It needs
  `policy.edit.org` or `audit.read.org`.
- `POST /v1/routing/apply` queues the active version and gives 202 with a `RoutingApplyInfo`.
  It needs `policy.edit.org`. With no active policy it gives 409 `routing.no_policy`.

- [ ] Failing tests: the permissions, a list with no rows of other tenants (`EndpointCase`
  must_not_list), and a queue that is audited.
- [ ] Implement, run, and commit.

### Task 6: Personal backup on the control plane (M2-20)

**Files:**
- Create: `control-plane/src/agentic_cp/personal/__init__.py`, `.../personal/store.py`,
  `.../personal/api.py`
- Modify: `tables.py` (`personal_events`, `TENANT_TABLES`), `db_roles.py` (`INSERT_ONLY`),
  the migration (same revision as Task 4), `app.py`, `core/src/agentic/orgapi/models.py`
  (`PersonalIngestRequest`, `PersonalFile`), `control-plane/tests/isolation.py`
- Test: `control-plane/tests/test_personal_backup.py`

**Interfaces:**
- Entries are `{seq, prev, hash, at, event, kind, path, sha256, content?, findings?}`, where:
  - `event` is one of `personal.saved | personal.removed | personal.held`;
  - `kind` is `skill | memory`;
  - `path` is `skills/<name>/<rel>.md|.txt` or `memory/<rel>.md|.txt`;
  - `content` is present only for `saved`, with sha256 equal to `sha256:` of its UTF-8 bytes;
  - each entry is at most 256 KiB.
- `POST /v1/personal/devices/{device}/events` gives an `IngestResult`.
- `GET /v1/personal/files` gives `list[PersonalFile]` (latest per path; a removed path is
  included as `deleted: true`).

- [ ] Failing tests:
  - accepted, then replayed as duplicates;
  - a gap or fork gives 409 with the head;
  - a bad hash gives 422;
  - content that mismatches its sha256 gives 422;
  - an escaping path gives 422;
  - a service account gives 403;
  - another user in the tenant (org admin included) gets an empty list;
  - an audit record per event without content;
  - two devices, where the newest copy wins;
  - an isolation factory and `EndpointCase`s.
- [ ] Implement, run, and commit.

### Task 7: Personal backup on the laptop, and commands (M2-20)

**Files:**
- Create: `cli/src/agentic_cli/personal_sync.py`
- Modify: `cli/src/agentic_cli/control_plane.py` (`ingest_personal`, `personal_files`),
  `cli/src/agentic_cli/commands/skills.py` (`backup`, `restore`),
  `cli/src/agentic_cli/commands/sync.py` (personal journal, and DF-497)
- Test: `cli/tests/test_personal_sync.py`, `cli/tests/test_sync_command.py` (additions)

**Interfaces:**
- `record_changes(home, guard, clock) -> Recorded(saved, removed, held, skipped)`.
- `PersonalSink(client)` (an `EventSink`).
- `deliver(home, client, scope) -> Delivered(sent, queued)`.
- `restore(home, files, *, force) -> Restored(written, unchanged, kept, refused)`.

- [ ] Failing tests:
  - a change is journaled once, and an unchanged file is not;
  - a removal is journaled;
  - a secret file is journaled as held, with no content;
  - offline, events are queued; on reconnect they are sent once and in order, and a second
    sync sends nothing;
  - two processes share the lock;
  - restore writes missing files, keeps a different local file without `--force`, and refuses
    `../` and absolute paths;
  - `agentic sync` for a missing run id exits 1 (DF-497).
- [ ] Implement, run, and commit.

### Task 8: Sink warnings (DF-499, DF-524, DF-535)

**Files:** `core/src/agentic/telemetry/sinks.py`; test
`core/tests/telemetry/test_sinks_backoff.py`

- [ ] Failing tests:
  - while offline, repeated failures log one warning per sink until the error changes or
    delivery recovers;
  - the background thread waits a backoff (doubling, at most 60 s) before re-reading
    `events.jsonl` after a failure, and `close()` still makes one final attempt;
  - a `PublishBlocked` is reported as "held back; a retry will not send it", never "will
    retry".
- [ ] Implement, run, and commit.

### Task 9: Full check

- [ ] `uv run python scripts/check.py --all` (background, wait for it to finish). Fix and re-run
  until green. Re-record the acceptance cassettes only if the drift test asks.
