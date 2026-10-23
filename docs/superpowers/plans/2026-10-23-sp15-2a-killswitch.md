# SP15 Slice 2a: Kill Switch, Pause and Org Preflight Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** An org admin (any scope) or a team lead (own team only) flips a kill switch or a pause switch in the control plane, scoped to the whole tenant, a team, a user, a repo, a pipeline, an agent (name or name@version), a model or a backend. Every change is on the tenant's audit chain. The engine asks a `KillSwitch` port before every step and before every agent turn: a kill stops the run at once (children and processes cleaned up, run marked `killed` with who and why); a pause finishes the current turn, checkpoints and holds, and `agentic resume <run>` continues without loss once the pause is cleared; switch state the CLI cannot confirm for longer than its expiry pauses the run, never continues it. The control plane serves a minimum CLI version (an older CLI is refused with an upgrade message) and enforces per-user and per-team concurrency caps when a run registers. Before an org run starts, preflight runs the org checks: RBAC, kill switch, CLI version, concurrency, team budget remaining (null while unmeasured), repo classification and pipeline integrity (D33: the pipeline must byte-match the approved version by hash).

**Rows:** M2-06, M2-19, M2-10, M2-25, M2-08, M2-21 (SP15 slice 2a). M2-54 (propagation test) is designed for, not built.

**Architecture:**
- **Core `agentic.killswitch`** (new package; imports neither `sdlc_commons`, `agentic_cli` nor Zyroflo): the scope kinds and modes, `RunScope` (team, user, repo, pipeline, agent, model, backend), `Switch`, the pure matcher `matches(switch, scope)` (shared by the control plane and the CLI, so both answer alike), `Verdict` (`run` / `pause` / `kill`, with `stale` for unconfirmed state), `decide(switches, scope)` (kill beats pause beats run), the `KillSwitch` protocol (`check(scope) -> Verdict`), `NoKillSwitch` (standalone), `RunKilled`, and `scope_problem(kind, value)` (one validator for server and CLI).
- **Engine (M2-19, M2-10).** `Engine(killswitch=..., kill_poll_s=..., repo=...)`. Before every step: the verdict for the run scope plus the step's agent, model and backend. During an agent step: a watchdog task polls every `kill_poll_s` and cancels the step on `kill` (inside the step's `process_scope`, so every process tree it started is killed); the step's runner gets a `turn_guard` (`StepContext.turn_guard`) that the native runner awaits before each model request. `kill` raises `RunKilled`; `pause` (or stale) makes the native runner save its conversation like a tool-approval pause and return `held`; the engine records `Pending(kind="pause", id="hold" | "kill-switch-unconfirmed", state_path, state_digest)`. Resume re-checks the switch, then continues the saved conversation. A port that raises is treated as stale (fail closed). `RunStatus` gains `killed`.
- **Control plane (M2-06, M2-08, M2-21 server half).** Table `kill_switches` (tenant-owned, forced RLS, updatable: clearing sets `cleared_by/at`; the audit chain is the evidence). Routes: `POST /v1/killswitch`, `DELETE /v1/killswitch/{id}`, `GET /v1/killswitch`, `GET /v1/killswitch/state`. `POST /v1/runs` registers a run after the org checks (`dry_run` checks only): CLI version (426 `cli.too_old`), RBAC `run.trigger`, kill switch, policy version accepted (ND-7), pipeline integrity (D33), concurrency (advisory lock per tenant), team budget (null). `policy_versions.settings` (new JSONB) keeps what the sync extracted: `min_cli_version`, `concurrency`, `allow_repo_pipelines`, `pipelines` (policy-repo path -> sha256 of the file bytes).
- **CLI (M2-19 port, M2-21 client half, M2-25).** `agentic_cli.killswitch.ControlPlaneKillSwitch` caches `GET /v1/killswitch/state` (TTL 10 s, stale after 60 s, thread-safe); `Runtime.engine` wires it in org mode with a control plane, `NoKillSwitch` otherwise. `Runtime.org_preflight` adds the server's checks to the preflight report (a dry run asks with `dry_run`). Commands: `agentic killswitch on|off|list`, `agentic pause <scope>`, `agentic resume --scope <scope>` (run resume unchanged). The cached policy's `fetched.json` records the approved pipeline hashes, so a locally edited pipeline is refused naming the approved hash even offline.

**Tech Stack:** Python 3.12, FastAPI/SQLAlchemy/asyncpg/alembic (control plane, as pinned), pycasbin through `agentic_cp.rbac.authz`, httpx2 (CLI), Typer. New direct dependency: `packaging==26.3` (Apache-2.0 OR BSD-2-Clause, pure Python, released 2026-08-04, already in the lock transitively) in the control plane only, for PEP 440 version comparison.

**Spec:** `docs/superpowers/specs/2026-10-16-agentic-sdlc-design.md` §8.2 (preflight), §8.6 (kill switch), §12.7 (minimum CLI version), §12.8 (concurrency), §6 / D33 (approved, hash-matched pipelines), §14.5 (audit). Master plan Review Focus 3. SP15 parent plan, "Slice 2" table rows M2-06, M2-08, M2-10 and "What slice 1 gives the consuming rows".

## OSS reuse decisions

| Need | Reused | Not reused, why |
|---|---|---|
| Kill-switch store and API | our control plane: tenancy with forced RLS, per-tenant audit chain, pycasbin `require` | Unleash (Apache-2.0) and OpenFeature (Apache-2.0) are feature-flag platforms: a second server and database with no tenant RLS or link to our audit chain; Flipt v1 is GPL-3.0 (not on the allow-list). The SP15 plan already chose a table plus the audit chain. |
| Client cache of switch state | a 40-line cache in the CLI | cachetools (MIT) `TTLCache` evicts on expiry; we need "stale beyond expiry -> pause" (fail closed) and the last good state, which it does not model |
| Version comparison | `packaging.version.Version` (Apache-2.0 OR BSD-2-Clause) | hand-written comparison gets pre-releases and local versions wrong |
| Process cleanup on kill | `agentic.backends.processes.process_scope` (M2-22) | — |
| Holding and resuming a conversation | pydantic-ai's own resume from a message history ending in a request (`UserPromptNode`) and our tool-approval pause file format | — |

Zyroflo (via `sdlc_commons`) has no switch or pause concept; the run ledger's `run_closed` status carries `killed` unchanged (no new event type, per M1-07).

## Rulings (recorded for the owner)

- R1 Cache TTL 10 s, stale after 60 s, engine watchdog poll 5 s: worst case kill propagation is TTL + poll = 15 s (target 30 s, M2-54); a pause or stale state takes effect at the next turn boundary (target: at most 1 turn).
- R2 Stale switch state pauses (`kill-switch-unconfirmed`), never kills: a network outage must not destroy work; `agentic resume` continues once the state is confirmed again.
- R3 A switch carries an optional `team` qualifier: a team-qualified switch of any kind applies only to runs of that team. A team lead may only create or clear switches qualified with a team they hold `killswitch.team` in; unqualified (tenant-wide) switches need `killswitch.global`.
- R4 Graceful pause holds at the next turn boundary for the native runner (the only backend the CLI wires today); runners without a turn hook hold at the next step boundary. A kill reaches every backend through the watchdog.
- R5 Child agents ignore a pause (their parent holds at its next turn) but obey a kill.
- R6 In org mode with a control plane, a run does not start when the org checks cannot run (control plane unreachable or not signed in): without confirmed switch state it would pause at once anyway. Org mode without a control plane keeps running, with a preflight warning that org checks were skipped.
- R7 The minimum CLI version is the higher of the deployment floor (`AGENTIC_CP_MIN_CLI_VERSION`) and the active policy's `org.yaml: min_cli_version`; it is enforced at run registration (a run never starts on an old CLI) and never on ingest (evidence already recorded is always accepted). A missing or unparsable version header is too old when a minimum is set.
- R8 A concurrency slot is a registered run whose status is still `open` (no `run_closed` ingested) and whose last activity is within `run_slot_hours` (24 h default); stale slots are M3-07's job.
- R9 `agentic pause/resume`: `agentic pause <scope>` sets a pause switch; `agentic resume --scope <scope>` clears the active pause switches of exactly that scope; `agentic resume <run>` is unchanged (exactly one of the two).

## Global Constraints

- core never imports `sdlc_commons`, `agentic_cli` or Zyroflo; the CLI may import core and commons; server libraries stay in the control plane (import-linter).
- Provider-agnostic: model and backend scope values are opaque strings; no vendor names in code, flags or messages.
- Every new tenant-owned table is listed in `TENANT_TABLES`, created with `enable_tenant_rls`, gets an isolation row factory and an `EndpointCase` for every object route (the completeness guards stay green). Another tenant's switch answers exactly like a missing one (404).
- Every switch change appends `killswitch.set` / `killswitch.cleared` to the tenant's audit chain in the same transaction.
- CLI: Typer modules under `agentic_cli/commands/` with `register(app)`, one line in `COMMANDS`; failures as `CliError(message, ExitCode.X, hint=...)`; results through `ctx.obj` so `--json` works; refusal exit 2.
- Windows and macOS laptops, Linux CI: no POSIX-only calls; files written with `newline="\n"`; paths in hash maps are POSIX relative paths.
- Never `# fmt: skip` / `# fmt: off`; line length 100; check pytest's exit code directly; `uv run python scripts/check.py --all` green. TRACKER.md, DEFERRED.md and DECISIONS files are the coordinator's.

## Review Focus

1. **A team lead cannot reach beyond their team.** Creating or clearing a tenant-wide switch, or one qualified with another team, is 403; another tenant's switch id is 404 exactly like a missing one; every set and clear is on the audit chain with who, scope and reason. Tests: Task 3 `test_team_lead_limited_to_own_team`, `test_every_change_is_audited`, isolation cases.
2. **Scope matching is the same on both sides and fails safe.** Each kind matches only its own dimension; `agent:x` matches every version of x and `agent:x@1.0.0` only that one; a team-qualified switch never stops another team's run; kill beats pause. Tests: Task 1 `test_scope_matching_per_kind`, `test_team_qualifier_limits_any_kind`, `test_kill_beats_pause`.
3. **Unconfirmed switch state pauses, never continues (master Review Focus 3).** With the control plane unreachable past the stale threshold the run pauses `kill-switch-unconfirmed` before the next step and before the next turn; a port that raises pauses too. Tests: Task 5 `test_stale_state_pauses_before_next_step`, `test_port_error_pauses`; Task 6 `test_stale_cache_gives_a_stale_verdict`.
4. **Pause resumes without loss; kill does not resume.** A held native step resumes its saved conversation (no repeated model call, turns already used still count); a killed run is terminal, its process trees are gone and `resume` refuses it. Tests: Task 5 `test_paused_run_resumes_without_loss`, `test_killed_run_does_not_resume`, `test_kill_mid_step_cancels_and_cleans_processes`.
5. **Org preflight refuses before any spend.** Old CLI (426 with upgrade message), concurrency at cap under a race (two registrations at cap-1 give one slot), a locally edited pipeline (refused with the approved hash), an unknown policy version, a kill switch, a missing `run.trigger`; team budget is `null`, never 0. Tests: Task 4 and Task 8.

---

### Task 1: Core kill-switch model and port (M2-06, M2-19)

**Files:**
- Create: `core/src/agentic/killswitch/__init__.py`
- Test: `core/tests/killswitch/test_killswitch.py`

**Interfaces:**
- `SCOPE_KINDS = ("tenant", "team", "user", "repo", "pipeline", "agent", "model", "backend")`, `ScopeKind`, `MODES = ("kill", "pause")`, `Mode`.
- `@dataclass(frozen=True) RunScope(team, user, repo, pipeline, agent, model, backend)` (all `str | None`), `with_step(agent=, model=, backend=)`.
- `@dataclass(frozen=True) Switch(id, scope_kind, scope_value, mode, reason, by, team=None)`.
- `matches(switch, scope) -> bool`; `run_level(switch, scope) -> bool` (agent/model/backend kinds always pass: the server's state filter).
- `@dataclass(frozen=True) Verdict(action, reason="", by="", switch="", stale=False)`; `.pause_id` (`"hold"` or `"kill-switch-unconfirmed"`); `.describe()`; `RUN`; `unconfirmed(reason) -> Verdict`.
- `decide(switches, scope) -> Verdict`.
- `class KillSwitch(Protocol): def check(self, scope: RunScope) -> Verdict`; `class NoKillSwitch`.
- `class RunKilled(Exception)` carrying `verdict`.
- `scope_problem(kind, value) -> str | None`, `parse_scope(text) -> tuple[ScopeKind, str]` (`global` = tenant).

- [ ] Write failing tests: matching per kind; agent name vs name@version; team qualifier; tenant matches all; decide precedence; scope validation (bad team slug, empty value, whitespace in model, `global` only without value); NoKillSwitch.
- [ ] Implement; tests pass; commit.

### Task 2: Wire models and the control-plane table (M2-06, M2-08)

**Files:**
- Modify: `core/src/agentic/orgapi/models.py`, `core/src/agentic/orgapi/__init__.py`
- Modify: `control-plane/src/agentic_cp/tables.py`, `control-plane/tests/isolation.py`
- Create: `control-plane/src/agentic_cp/migrations/versions/0009_killswitch.py`
- Test: existing guards (`test_migrations_match_metadata`, `test_every_tenant_table_has_an_isolation_factory`, `test_tenant_isolation`)

**Interfaces:**
- `KillSwitchRequest(scope_kind, scope_value="", team=None, mode="kill", reason)`, `KillSwitchInfo(id, scope_kind, scope_value, team, mode, reason, created_by, created_at, cleared_by, cleared_at, active)`, `KillState(switches, checked_at, ttl_seconds)`.
- `RunRegistration(run_id, team, repo, pipeline, pipeline_file, pipeline_sha256, policy_version, agents, models, backends, dry_run)`, `OrgCheck(check, passed, detail, value)`, `RunPreflight(registered, checks, team_budget_remaining_usd=None, min_cli_version=None)`.
- `kill_switches` table; `policy_versions.settings JSONB NOT NULL DEFAULT '{}'`.

- [ ] Add the isolation factory first; see the completeness guard fail; add table + migration; guards pass; commit.

### Task 3: Kill-switch API (M2-06)

**Files:**
- Create: `control-plane/src/agentic_cp/killswitch/__init__.py`, `control-plane/src/agentic_cp/killswitch/api.py`
- Modify: `control-plane/src/agentic_cp/app.py`, `control-plane/src/agentic_cp/settings.py` (`kill_ttl_seconds = 10`)
- Test: `control-plane/tests/test_killswitch_api.py`; isolation cases in `tests/isolation.py`

**Interfaces:**
- `POST /v1/killswitch` -> 201 `KillSwitchInfo` (`killswitch.global` without `team`; `killswitch.team` in `team`); scope value validated with `scope_problem`; `team` scope sets `team = scope_value`.
- `DELETE /v1/killswitch/{id}` -> 200 `KillSwitchInfo` (same permission as creating it; 404 unknown or other tenant; 409 `killswitch.cleared`).
- `GET /v1/killswitch?include_cleared=` -> switches the caller may clear.
- `GET /v1/killswitch/state?team=&repo=&pipeline=` -> `KillState`: active switches at run level for the caller (user scope = the caller's email).
- Audit `killswitch.set` / `killswitch.cleared` (target `killswitch:<id>`, team, reason, after = scope and mode).

- [ ] Tests first: admin sets a tenant switch; team lead sets own-team switch; team lead refused tenant-wide / other team / clearing another team's switch; developer refused; state filters by team/user/repo/pipeline and returns agent/model/backend switches; state in tenant b never shows a's switch; every change audited; cleared switch absent from state; double clear 409.
- [ ] Implement; pass; commit.

### Task 4: Minimum CLI version, run registration and concurrency (M2-08, M2-21 server half)

**Files:**
- Create: `control-plane/src/agentic_cp/runs/__init__.py`, `control-plane/src/agentic_cp/runs/register.py`
- Modify: `control-plane/src/agentic_cp/ingest/api.py` (mount `POST /v1/runs`), `control-plane/src/agentic_cp/policy_sync/service.py` (store `settings`), `control-plane/src/agentic_cp/settings.py` (`min_cli_version`, `run_slot_hours`), `control-plane/pyproject.toml` (`packaging`)
- Modify: `core/src/agentic/policy/model.py`, `core/src/agentic/policy/load.py` (`min_cli_version` in org.yaml; `pipeline_digests(root)`)
- Test: `control-plane/tests/test_run_registration.py`, `core/tests/policy/test_pipeline_digests.py`

**Interfaces:**
- `cli_version_problem(sent: str | None, minimum: str | None) -> str | None`.
- `POST /v1/runs` body `RunRegistration` -> 200 `RunPreflight`; 426 `cli.too_old` (hint names the minimum and how to upgrade) before anything else.
- Checks, in order: `rbac` (`run.trigger` in the team / repo), `killswitch` (run scope x agents, models, backends), `policy` (version accepted in this tenant), `pipeline` (D33), `concurrency` (`per_user`, `per_team` of that version's `limits.yaml`), `budget` (passed, value `None`). Registered only when all pass and not `dry_run`; serialised by a per-tenant advisory lock.

- [ ] Tests first: old CLI 426 with upgrade message (deployment floor; policy minimum higher wins); each check refused with its own test; locally edited pipeline refused naming the approved hash; repo pipeline accepted only when the policy allows repo pipelines; cap enforced; two concurrent registrations at cap-1 give one slot; tenants counted separately; a closed run frees its slot; dry run registers nothing; budget is null; registered run then ingests normally.
- [ ] Implement; pass; commit.

### Task 5: Engine kill-switch checks, hold and kill (M2-19, M2-10)

**Files:**
- Modify: `core/src/agentic/engine/engine.py`, `core/src/agentic/engine/state.py` (`killed`), `core/src/agentic/agents/runner.py` (`turn_guard`, hold, resume without decisions), `core/src/agentic/backends/native.py`, `core/src/agentic/engine/factory.py` (children inherit the guard)
- Test: `core/tests/engine/test_engine_killswitch.py`, `core/tests/agents/test_runner_hold.py`

**Interfaces:**
- `Engine(..., killswitch: KillSwitch | None = None, kill_poll_s: float = 5.0, repo: str | None = None)`.
- `StepContext.turn_guard: Callable[[], Awaitable[Verdict]] | None = None`.
- `PydanticAIRunner(..., turn_guard=None)`; `AgentOutcome.held: Verdict | None`.
- Pending ids `hold`, `kill-switch-unconfirmed`; run status `killed` with `ended_by` / `end_reason`; ledger `run_closed status=killed`, `step_finished outcome=killed`.

- [ ] Tests first: kill before a step marks the run killed with who/why; pause before a step holds and resume continues once cleared (still paused: stays paused); stale and raising port pause; kill mid-step via watchdog cancels and kills the step's process tree; native runner holds before its next model request and resumes the saved conversation without repeating the call; kill at a turn raises; killed run refuses resume and stop; standalone (no port) unchanged.
- [ ] Implement; pass; commit.

### Task 6: CLI kill-switch port and wiring (M2-19)

**Files:**
- Create: `cli/src/agentic_cli/killswitch.py`
- Modify: `cli/src/agentic_cli/control_plane.py` (client calls), `cli/src/agentic_cli/runtime.py`, `cli/src/agentic_cli/driving.py`, `cli/src/agentic_cli/commands/runs.py` (killed shown, exit 5), `cli/tests/cp_fakes.py`
- Test: `cli/tests/test_killswitch_port.py`

**Interfaces:**
- `CACHE_TTL_S = 10.0`, `STALE_AFTER_S = 60.0`, `POLL_S = 5.0`.
- `ControlPlaneKillSwitch(client, *, user, team, repo, clock=time.monotonic)`: `check(scope) -> Verdict`.
- `Runtime.killswitch() -> KillSwitch`.

- [ ] Tests first: fresh state cached for TTL (one request); refetch after TTL; unreachable within stale threshold uses last good state; beyond it a stale verdict; never fetched + unreachable is stale; user dimension is the signed-in email; standalone gets `NoKillSwitch`.
- [ ] Implement; pass; commit.

### Task 7: Admin commands (M2-25)

**Files:**
- Create: `cli/src/agentic_cli/commands/killswitch.py`
- Modify: `cli/src/agentic_cli/commands/__init__.py`, `cli/src/agentic_cli/commands/runs.py` (`resume --scope`)
- Test: `cli/tests/test_killswitch_commands.py`

**Interfaces:**
- `agentic killswitch on <scope> --reason R [--team T]`, `agentic killswitch off <id>`, `agentic killswitch list [--all]`, `agentic pause <scope> --reason R [--team T]`, `agentic resume --scope <scope> [--team T]`.

- [ ] Tests first: each command's JSON result; refusal per role exits 2 with the server's message (developer, team lead outside their team); bad scope exits 64; not in org mode exits 2; `resume` with both a run and `--scope` is a usage error.
- [ ] Implement; pass; commit.

### Task 8: Org preflight in the CLI (M2-21 client half)

**Files:**
- Create: `cli/src/agentic_cli/org_preflight.py`
- Modify: `cli/src/agentic_cli/runtime.py`, `cli/src/agentic_cli/commands/runs.py`, `cli/src/agentic_cli/policy_source.py` (`pipelines` in `fetched.json`; edited pipelines named with their approved hash), `core/src/agentic/engine/preflight.py` (one pipeline-level `classification` problem)
- Test: `cli/tests/test_org_preflight.py`

- [ ] Tests first: one test per org check mapped into the report (rbac, killswitch, cli version, concurrency, budget null shown as "not measured", classification, pipeline integrity); dry run sends `dry_run`; control plane unreachable refuses the run (R6); org mode without control plane warns; locally edited cached pipeline refused with the approved hash.
- [ ] Implement; pass; commit.

### Task 9: Deferred items pulled in

- DF-409 / DF-402: the gate approval ledger entry records `required_role` and `role_held` (from `OrgAuthorizer.role_held`).
- DF-488: ingest recomputes `policy_known` while it is false.
- DF-493: ingest refuses entries holding floats that do not survive jsonb (exponent form, -0.0).
- DF-494: the NUL check walks values, so the six characters `\u0000` in a string pass.
- DF-498: `login --for control-plane` checks the URL before the token prompt; a 401 at login says the token was not accepted.
- DF-353: committed `.agentic/local/init.json` is ignored (honoured only when untracked); UNC policy records are refused.
- DF-182 / DF-340 (minimum CLI version part): closed by Task 4.
- Retargeted with reasons in the track output: DF-55, DF-63, DF-361.

Each with a test seen failing first.

### Task 10: Full check

- [ ] `uv run python scripts/check.py --all` green on Windows and Linux; fix and re-run.
