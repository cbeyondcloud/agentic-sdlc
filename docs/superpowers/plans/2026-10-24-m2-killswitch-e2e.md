# M2-54 Kill-Switch Propagation E2E Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prove, end to end and repeatably, that a kill switch flipped in the real control plane stops a running pipeline of the real CLI within 30 s and within one turn, with no process left behind: for a tenant-wide (global) switch, a team switch and a pipeline switch, five consecutive runs each, every timing reported and the worst case of the five named. Prove too that a pause switch holds a run at the end of the turn in progress and that `agentic resume` continues it without repeating a model call, and that switch state the CLI cannot confirm past its stale window pauses the run. Close the open deferred items that target M2-54 (DF-504, DF-506, DF-516, DF-518).

**Rows:** M2-54 (SP20). Deferred items pulled in: DF-504, DF-506, DF-516, DF-518 (test half; the server-side team enforcement half stays with M2-30).

**Architecture:**
- **No engine change.** The mechanism exists (M2-19, M2-10, M2-22): the CLI port caches `GET /v1/killswitch/state` (TTL 10 s, stale 60 s), the engine checks before each step, before each model request (`turn_guard`) and every 5 s from a watchdog while a step runs; a kill cancels the step inside its `process_scope`. Worst case propagation by design is TTL + poll = 15 s (ruling R1 of the SP15 2a plan). This row measures it.
- **E2E harness** (`control-plane/tests/e2e/`, next to the existing CLI-against-control-plane contract tests, because it needs the Postgres fixtures):
  - the real control plane: `create_app(settings_for(db))` under uvicorn on 127.0.0.1 in a thread with its own engine and loop (the `LiveServer` pattern of `tests/contract`), wrapped by a tiny ASGI recorder that timestamps every `GET /v1/killswitch/state` it serves ("the CLI learned of the flip");
  - tenant a bootstrapped; a developer of team `payments` (real token, `developer` role assigned through the API) runs the CLI; the org admin flips switches through the CLI's own `ControlPlaneClient` over HTTP;
  - the policy repo is a real git repo synced by the control plane (`PUT /v1/tenant/policy-source`, `file://`), fetched by the CLI's own policy source (`resolve_policy` + `commit`, what `agentic init` does in org mode), so registration's D33 pipeline check compares the CLI's hash of the bundle with the control plane's hash of the commit (DF-518 a); the pipeline file is committed with CRLF line endings and a non-ASCII character so a line-ending or encoding difference would show;
  - the real CLI run path in this process (`agentic_cli.main.main(["--json", "run", ...])` on the main thread, so its signal handling is real) with a scripted model (`runtime.network_transport` replaced by an `httpx2.MockTransport` that answers per agent and timestamps each request);
  - a slow turn: the model's first answer calls `run_shell` with `python spawner.py`, a script committed in the target repo that starts a grandchild, writes both pids and waits for a release file (bounded lifetime, so nothing outlives a failed test); the run is mid-step, blocked in that tool call, when the switch flips;
  - a flipper thread waits (deadline, polling) for the pid file, flips the switch and records the time; the main thread's `main()` returns when the run is closed; then the pids are polled until gone (deadline, never a fixed sleep as an assertion);
  - timings (flip -> CLI aware, flip -> run killed, flip -> process tree gone, model calls after the flip) are collected per run, printed in a terminal summary section and included in every assertion message.
- **DF-506 (server):** `GET /v1/killswitch/state` sends a strong `ETag` (sha256 over the tenant, the caller, the query and the answer's switches and TTL; never `checked_at`) and `Cache-Control: private, no-cache`; a request whose `If-None-Match` matches gets `304` with no body. **DF-506 (CLI):** the port sends its last `ETag` and treats a `304` as a fresh confirmation of the switches it holds.
- **DF-516 (CLI):** the port honours `KillState.ttl_seconds`: it caches for `min(ttl_seconds, CACHE_TTL_S)` (at least 1 s), so an operator can shorten propagation through `AGENTIC_CP_KILL_TTL_SECONDS` but never lengthen it past the 30 s target. `checked_at` stays informational: the CLI ages answers by its own monotonic clock (a laptop's wall clock may be skewed).

**Tech Stack:** Python 3.12, pytest, uvicorn/FastAPI/asyncpg (control plane, as pinned), httpx2 (CLI). No new dependency.

**Spec:** `docs/superpowers/specs/2026-10-16-agentic-sdlc-design.md` §8.6 (kill switch: checks before every step and turn, short cache, children terminated, run marked `killed` with who/why, offline -> pause). SP15 2a plan rulings R1 (TTL 10 s, stale 60 s, poll 5 s -> 15 s worst case), R2 (stale pauses), R4 (pause at the next turn boundary). Master plan row M2-54: "E2E timing test passes 5/5 runs".

## OSS reuse decisions

| Need | Reused | Not reused, why |
|---|---|---|
| Serving the real control plane in a test | uvicorn (already pinned, BSD-3) `Server` in a thread, the `LiveServer` pattern of `control-plane/tests/contract` | — |
| Repeating a test N times | a loop inside one test (each run's timing reported) | pytest-repeat and pytest-rerunfailures are MPL-2.0 (not on the allow-list); a rerun plugin would also hide flakiness, the opposite of "5/5" |
| Polling with a deadline | a 10-line helper | tenacity (Apache-2.0) / polling2 (MIT): a new dependency for one loop |
| Is a pid alive | `core/tests/_process_support.alive` (loaded by path, one implementation) | psutil (BSD-3) is a native extension: not installed on this Windows host (owner rule), and not needed |
| Conditional GET | HTTP's own `ETag` / `If-None-Match` / `304` (RFC 9110), hand-written in the route | starlette has no conditional-GET middleware for dynamic JSON; a generic middleware would hash after serialising every route |

Zyroflo has nothing for switch propagation or process timing.

## Rulings (recorded for the owner)

- R1 **Placement:** the E2E lives in `control-plane/tests/e2e/` (it needs the Postgres fixtures and drives the CLI in process, as `tests/contract` already does). It runs in every `check.py --all` stage; on a host without Postgres it skips with the fixture's reason, and the Linux stage sets `AGENTIC_CP_REQUIRE_PG=1`, so there it can never silently skip.
- R2 **"Within one turn"** is measured as model calls started after the flip: at most 1 (the kill cancels the turn in progress, so it is expected to be 0). For a pause, the turn in progress at the flip finishes and no further model call is made. A pause lands at the first turn boundary after the CLI learns of it, and the CLI learns within TTL + poll (the watchdog refreshes the cache while a turn runs); the E2E's slow turn ends once the control plane has served the CLI a state poll after the flip, so the test asserts both bounds without a fixed sleep. A turn shorter than the propagation window can be followed by more turns before the pause lands; that is the price of the 10 s cache (spec §8.6 "lightweight poll with short cache") and is stated, not hidden.
- R3 **Timing reference points:** the flip time is taken before the admin's `POST /v1/killswitch` is sent (conservative: it lengthens every measured interval); "run marked killed" is when the CLI's `run` command returns with the run saved as `killed` (an upper bound); "process tree gone" is when neither the child nor the grandchild pid is alive. The 30 s bound is asserted on the worst of these; the timing of each run is reported.
- R4 **Stale window without waiting 60 s:** the stale E2E stops the real control plane (connections refused) and advances only the kill-switch port's monotonic clock (a test seam: the port already takes `clock`), so the 60 s window is crossed without a minute of wall time. Everything else is real.
- R5 **DF-516:** the CLI caches for `min(server ttl_seconds, 10 s)`, at least 1 s: the server may shorten propagation, never lengthen it beyond the 30 s target.
- R6 **DF-506:** the `ETag` includes the tenant and the caller, so equal answers to two callers never share a tag; a `304` counts as a confirmation (it resets the stale clock) because the server answered.
- R7 **DF-504** was done by M2-60 (0a74ce9: the claude-code, codex and ACP runners await `turn_guard`); this row only confirms it and returns the item as Done.

## Global Constraints

- core never imports `sdlc_commons`, `agentic_cli` or Zyroflo; server libraries stay in the control plane (import-linter). Tests may import across packages.
- Provider-agnostic: the scripted model, scopes and messages name no vendor.
- No new table, route or migration (DF-506 changes the behaviour of an existing route; its `EndpointCase` stays green, the OpenAPI snapshot is regenerated). The Alembic head is unchanged (`m2_37_usage_views`).
- Windows and macOS laptops, Linux CI: pid checks through `_process_support.alive` (Windows and POSIX); the spawner is plain Python started through the platform shell; files written with explicit bytes; no POSIX-only calls in the harness.
- Timing robustness (DF-688 lesson): every wait is a poll with a deadline; no fixed sleep is an assertion; the process-gone deadline is generous (60 s) while the asserted bound is the measured 30 s; each assertion message carries every timing.
- No live model, provider, TokenLean or Jira call: the model is scripted, the control plane is local.
- Processes are stopped only by pid (the spawner exits on its own when released; the harness never kills by image name).
- Never `# fmt: skip` / `# fmt: off`; line length 100; check pytest's exit code directly; `uv run python scripts/check.py --all` green. TRACKER.md, DEFERRED.md and DECISIONS files are the coordinator's.

## Review Focus

1. **The 30 s / one-turn claim is measured, not assumed.** Each of the 15 kill runs (tenant, team, pipeline x 5) asserts flip -> killed <= 30 s, flip -> process tree gone <= 30 s and model calls after the flip <= 1, reports each run's timings and the worst case; the run is genuinely mid-step (blocked in a tool call with a live child and grandchild) when the switch flips. Test: Task 4 `test_a_kill_switch_stops_a_running_pipeline_5_of_5[tenant|team|pipeline]`.
2. **No orphan processes.** The child and grandchild pids the step started are gone after every kill, on Windows (job object) and Linux (group/session), polled with a deadline. Test: Task 4 (same test).
3. **Pause holds within one turn and resumes without loss.** The turn in progress finishes, no model call follows the flip, the run is paused `hold`; after the switch is cleared `agentic resume` sends exactly one more model request (the one after the tool result) and completes. Test: Task 5 `test_a_pause_switch_holds_within_one_turn_and_resume_repeats_no_model_call`.
4. **Unconfirmed state pauses, never continues.** With the control plane unreachable past the stale window the run pauses `kill-switch-unconfirmed` at the next turn; signed out, a resumed run stays held. Tests: Task 6 `test_unconfirmable_switch_state_pauses_a_running_pipeline`; Task 3 `test_signed_out_resume_stays_held_unconfirmed`.
5. **ETag/304 never serves another caller's or another tenant's state, and a 304 keeps the CLI confirmed.** Tests: Task 1 `test_state_etag_and_304`, `test_state_etag_differs_per_caller_and_tenant`; Task 2 `test_a_304_confirms_the_cached_switches`.
6. **The server's TTL can shorten the CLI cache, never lengthen it.** Test: Task 2 `test_the_server_ttl_shortens_the_cache`, `test_the_server_ttl_never_lengthens_the_cache`.

---

### Task 1: Conditional GET on the kill-switch state (DF-506 server, DF-518 c)

**Files:**
- Modify: `control-plane/src/agentic_cp/killswitch/api.py`, `control-plane/openapi.json`
- Test: `control-plane/tests/test_killswitch_api.py`

**Interfaces:**
- `GET /v1/killswitch/state` -> 200 `KillState` with headers `ETag: "<sha256 hex>"`, `Cache-Control: private, no-cache`; with a matching `If-None-Match` -> 304, same headers, empty body.
- Docstring states that `team` is client-supplied and only narrows what the caller sees (enforcement for a client that omits it is M2-30's TokenLean deny rule).

- [ ] Write failing tests: ETag present and stable across two calls; matching `If-None-Match` gives 304 with no body; a new switch changes the tag (200 again); the tag differs for another caller and another tenant with the same answer; omitting `team` hides team-qualified switches but not tenant-wide ones (pins DF-518 c).
- [ ] Implement; regenerate `openapi.json`; tests pass; commit.

### Task 2: The CLI port honours the server TTL and the ETag (DF-516, DF-506 client)

**Files:**
- Modify: `cli/src/agentic_cli/control_plane.py` (`kill_state_reply`), `cli/src/agentic_cli/killswitch.py`, `cli/tests/cp_fakes.py` (the fake honours `If-None-Match`, serves a configurable TTL)
- Test: `cli/tests/test_killswitch_port.py`

**Interfaces:**
- `ControlPlaneClient.kill_state_reply(*, team, repo, pipeline, etag=None) -> KillStateReply(state: KillState | None, etag: str | None)` (`state` None on 304); `kill_state(...)` unchanged.
- `ControlPlaneKillSwitch`: `_Entry.etag`, `_Entry.ttl_s`; `MIN_TTL_S = 1.0`.

- [ ] Write failing tests: server TTL 3 s refetches after 3 s; server TTL 30 s still refetches after 10 s; the second poll sends `If-None-Match` and a 304 keeps the switches and resets the stale clock; a 304 without a cached answer is treated as unconfirmed (never "run").
- [ ] Implement; pass; commit.

### Task 3: Signed out, a resumed org run stays held (DF-518 b)

**Files:**
- Test: `cli/tests/run/test_org_preflight.py`

- [ ] Write the test: a run held by a pause switch; the switch cleared; the token removed; `agentic resume` exits 4 with pending `kill-switch-unconfirmed` naming the sign-in problem; no model call. (A test gap: expected to pass on the existing code; if it fails, fix the CLI.)
- [ ] Commit.

### Task 4: E2E harness and kill propagation 5/5 per scope (M2-54, DF-518 a)

**Files:**
- Create: `control-plane/tests/e2e/conftest.py` (fixtures, terminal summary), `control-plane/tests/e2e/_killswitch_e2e.py` (live server with request log, scripted model, spawner, org workspace, flipper, polling), `control-plane/tests/e2e/test_killswitch_propagation.py`

**Interfaces:**
- `Timing(scope, run, flip_to_aware_s, flip_to_killed_s, flip_to_gone_s, calls_after_flip)`.
- `KILL_BOUND_S = 30.0`, `RUNS = 5`.

- [ ] Write the test first against the harness interfaces; see it fail (missing harness), build the harness until a single run works; then prove the test can fail: run once with the engine poll stretched past the bound (local, not committed) and see the bound assertion fail with the timings in the message.
- [ ] `test_a_kill_switch_stops_a_running_pipeline_5_of_5[tenant|team|pipeline]`: five consecutive runs each; per run: registration passed every org check (D33 agreement), exit 5, run `killed` with the admin as `ended_by` and the reason, the bounds above, pids gone; the switch is cleared before the next run.
- [ ] Commit.

### Task 5: E2E pause holds within one turn; resume repeats no model call (M2-54, M2-10)

**Files:**
- Modify: `control-plane/tests/e2e/test_killswitch_propagation.py`

- [ ] Test: pause switch (team) flipped while the run is in its slow turn; the turn is released once the control plane served a state poll after the flip; exit 4, pending `hold`; zero model calls after the flip; flip -> aware and flip -> held <= 30 s; the tool's processes gone. Clear the switch; `agentic resume` exits 0; exactly one more model request, which carries the first call's tool result.
- [ ] Commit.

### Task 6: E2E stale switch state pauses (M2-54, master Review Focus 3)

**Files:**
- Modify: `control-plane/tests/e2e/test_killswitch_propagation.py`

- [ ] Test: run mid-step; stop the control plane; advance the port's clock past the stale window; release the turn; exit 4, pending `kill-switch-unconfirmed` naming the unreachable control plane; no model call after the outage; processes gone.
- [ ] Commit.

### Task 7: Full check

- [ ] `uv run python scripts/check.py --all` green on Windows and Linux; record the worst case of each scope's five runs from both stages.
