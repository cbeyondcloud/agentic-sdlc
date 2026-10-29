# M3 Run health Implementation Plan (M3-07, DF-697 remainder)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A run whose CLI process died stops holding a concurrency slot, and someone is told. While the CLI drives a run it sends a heartbeat: to a small file in the run folder (always) and, in org mode with a control plane, to `POST /v1/runs/{run_id}/heartbeat` from a background thread that never blocks the engine and shrugs off an unreachable control plane. The control plane's worker marks an open run **stale** when it has had no heartbeat (and no event) for `org.yaml: run_health.stale_minutes` (default 30): the run stops counting against the M2-08 caps, the starter is mailed through the notifications outbox, and `run.stale` is on the tenant's audit chain. A run waiting for a person or a pause switch is not a zombie: the CLI's last beat says `waiting`, and a `gate_waiting` or paused `step_finished` event says so too. A stale run whose CLI comes back with a fresh beat is revived (`run.revived`); an old, queued beat never revives one. Locally, `agentic logs --follow` stops on a run whose heartbeat went stale (DF-278) and plain `logs` says how much of an unfinished last line it held back (DF-282); `run.json` reads are retried while a writer swaps the file on Windows (DF-280).

DF-697 remainder (owner decision 105, 2026-10-28): a standalone or `--policy` run paused on one policy version and resumed after its owner edited the policy **continues**, and its run ledger records the change (the old and new version on the first event after the resume, whose `step` names where); an org policy (control plane or git) is still refused, as M3-21 made it.

**Architecture:**
- **Policy (`agentic.policy.model`).** `OrgPolicy.run_health: RunHealthPolicy` (`org.yaml: run_health`): `heartbeat_seconds` (60, 15..600) and `stale_minutes` (30, 5..1440), with `stale_minutes x 60 >= 3 x heartbeat_seconds` (one lost beat never makes a zombie). The control plane stores it in each accepted version's run settings (`RUN_SETTINGS_KEYS` gains `run_health`; older stored versions are re-read from their bundle, as for `learning`).
- **API models (`agentic.orgapi`).** `HeartbeatRequest` (`beat_at`, `sent_at`, `state` = `running` | `waiting`) and `HeartbeatResult` (`status`, `counted`, `revived`). `RunSummary` gains `last_heartbeat_at` and `waiting`.
- **Control plane.**
  - Migration `m3_07_run_health` (after `m3_11_learning`): `runs` gains `last_heartbeat_at`, `waiting` (bool, default false) and `stale_at`. No new table (the `runs` RLS and isolation factory cover the columns).
  - `agentic_cp/ingest/health.py`: `record_heartbeat` (the route's work), `waits(entries)` (a batch ending in a wait), `sweep` (one tenant's zombie pass) and `zombie_job` (`policy_sync.worker.EXTRA_JOBS`, before the outbox so the mail leaves in the same pass).
  - Liveness is the server's clock: a beat counts as `now - (sent_at - beat_at)` (both client times, so the client's clock skew cancels out); a beat older than the window is recorded as not counted and never revives.
  - `ingest_batch`: a batch whose last new event is a wait sets `waiting`; events never clear it (a running beat does), so a late batch cannot make a waiting run look dead. A stale run's events are stored as evidence; a `run_closed` closes it; other events leave it stale (a replay by `agentic sync` never resurrects it).
  - The zombie test: `status = 'open' AND NOT waiting AND greatest(opened_at, last_event_at, last_heartbeat_at) < now - stale_minutes`. `stale` is not `open`, so `_concurrency` stops counting it at once (DF-509's orphaned slot included). Review F2: a run with events whose CLI never beat (`last_heartbeat_at IS NULL AND last_event_at IS NOT NULL`, a release before M3-07) is judged by the deployment's `run_slot_hours` instead - a long agent step sends nothing, and a stale run's events never revive it - so during the rollout an old CLI's live run is not marked stale; it goes stale when it holds no slot anyway.
  - Mail kind `run.stale` (template `run_stale.txt/.html`) to the starter when an active person; `run.stale` / `run.revived` audit events.
- **CLI.**
  - `agentic_cli/heartbeat.py`: `Heartbeat` (a context manager around one engine call: a daemon thread beats at once and every `heartbeat_seconds`; each beat rewrites `<run>/heartbeat.json` atomically and, with a poster, posts the beat; a failure is logged once until it changes and never raised), `finish(status)` (one `waiting` beat when the call ends at a gate or pause) and `local_state(run_dir)` (`alive` / `stale` / None for no or unreadable file).
  - `ControlPlaneClient.heartbeat(...)`; the heartbeat thread gets its own client (`client_for`), never the sink's.
  - `Runtime.heartbeat(run_id)` builds it; `run`, `resume` and the gate verbs wrap `drive` in it.
  - `logs --follow`: a `running` run whose heartbeat is stale is read to the end and the follow exits 1 with what to do; plain `logs` warns about held-back bytes (`LogTail.held`).
  - `RunStore.load` retries a `PermissionError` briefly (DF-280).
- **DF-697.** `sdlc_commons.ledger.FIELDS` gains `policy_from` and `policy_to` (the policy version pattern). `Runtime.check_same_policy` returns `(old, new)` for a standalone or `--policy` run whose policy moved since the version in force (the last `policy_to` in its ledger, else the version it started on), queues the stamp on the run's `StampingRecorder` (the next event the run records carries both ids), writes `run.policy_changed` to the audit log and the command warns. Org caches keep refusing.

**Tech Stack:** Python 3.12, stdlib `threading` (as `ForwardingRecorder`), Pydantic v2, FastAPI + SQLAlchemy Core + Alembic (existing). No new dependency.

**Spec:** §1.3 / §12.1 (the run record names the policy every step used), §8.2 (registration), §12.7-12.8 (caps), "No runaway process" (M3-07 heartbeat + zombie detection). Rows M3-07, DF-697 (remainder; owner decision 105). Deferred items pulled in: DF-278, DF-280, DF-282, DF-468, DF-469, DF-475, DF-476, DF-478, DF-508, DF-509, DF-704, DF-706, DF-707, DF-697, DF-748.

## OSS reuse decisions

| Need | Reused | Not reused, why |
|---|---|---|
| Periodic zombie sweep | the control plane's existing leader-elected worker loop (`policy_sync.worker.EXTRA_JOBS`) | APScheduler (MIT) / Celery beat (BSD): a second scheduler next to one that already runs per-tenant jobs under the tenant context |
| Client heartbeat thread | stdlib `threading.Event` loop, the pattern of `ForwardingRecorder` | a library adds nothing over a 40-line loop |
| Mail and audit | the notifications outbox (M2-07) and the per-tenant audit chain | - |
| Heartbeat in the hash chain | - | Zyroflo's event types are fixed (no heartbeat type, and Zyroflo is a pinned library); 60 entries an hour per run would bloat the evidence, and a chained beat replayed by `agentic sync` could not be told from a live one |

## Global Constraints

- **Never blocks, never fails a run.** The heartbeat thread is a daemon; its HTTP timeout is short (5 s); the engine never waits on it; a failing control plane is reported once (`agentic.heartbeat` log) and retried at the next beat.
- **Heartbeats are not queued durably.** A beat is a liveness signal: the thread always sends the newest one. The protocol still carries `beat_at` and `sent_at`, so a delayed delivery is judged by its true age.
- **Clocks.** Staleness is decided by the server's clock (`state.clock`, a fake in tests); the client's clock only measures a beat's own delay. Local staleness compares the beat file's `at` with the same machine's clock.
- **Paused is not dead.** `waiting` comes from the CLI's last beat or a wait event; a waiting run keeps its slot until `run_slot_hours` (M2-08 ruling R8 unchanged).
- **Isolation.** The new route gets an `EndpointCase`; another tenant's run is 404 like a missing one.
- **Cross-platform.** `heartbeat.json` is written with a temporary file and `os.replace` (retried briefly on Windows); readers treat an unreadable file as unknown. No new process handling.
- **Formatting, CI and records:** never add `# fmt: skip` / `# fmt: off`; line length 100; check pytest's exit code directly; `uv run python scripts/check.py --all` green on Windows and Linux; TRACKER.md, DEFERRED.md and DECISIONS are updated by the coordinator from this track's output.

## Review Focus

1. **Zombie detection frees the slot (row M3-07).** With a fake clock: a registered run with no heartbeat for more than `stale_minutes` is marked `stale`, a new run then registers at the cap, `run.stale` is audited and the starter's mail is queued once; a run that beat within the window is untouched. Tests: Task 3 `test_a_run_without_a_heartbeat_is_marked_stale_and_its_slot_freed`, `test_a_beating_run_is_not_stale`, `test_the_window_comes_from_the_policy`.
2. **Paused runs are not zombies.** A run whose last beat said `waiting`, or whose last event is `gate_waiting` or a paused `step_finished`, is never marked stale; a running beat clears it. Tests: Task 3 `test_a_waiting_run_is_not_stale` (both sources), Task 2 `test_a_late_batch_does_not_clear_waiting`.
3. **Queued beats and replays never resurrect.** A beat whose `sent_at - beat_at` puts it before the window does not move liveness or revive a stale run; a replayed non-closing batch leaves a stale run stale; a fresh beat revives it (`run.revived`); `run_closed` closes it. Tests: Task 2 `test_an_old_queued_beat_does_not_revive_a_stale_run`, `test_a_fresh_beat_revives_a_stale_run`, `test_events_of_a_stale_run_do_not_revive_it`.
4. **The CLI never blocks on the control plane.** An unreachable control plane: the run completes, the beat file is written, the failure is logged once. A run ending at a gate sends one `waiting` beat. Tests: Task 5.
5. **DF-697.** A standalone run resumed after a policy edit continues and its ledger carries `policy_from` / `policy_to` once; resuming again without an edit adds nothing; the org-mode refusals stay (existing tests). Task 7.

---

## Task 1: Policy and API models

**Files:** `core/src/agentic/policy/model.py`, `core/src/agentic/policy/load.py`, `core/src/agentic/orgapi/models.py`, `core/src/agentic/orgapi/__init__.py`, `control-plane/src/agentic_cp/policy_sync/service.py`; tests `core/tests/policy/test_run_health_policy.py`, `control-plane/tests/test_policy_sync.py` (settings carry `run_health`).

**Interfaces:**
```python
class RunHealthPolicy(_Strict):
    heartbeat_seconds: int = Field(default=60, ge=15, le=600)
    stale_minutes: int = Field(default=30, ge=5, le=1440)

class HeartbeatRequest(RequestModel):
    beat_at: AwareDatetime
    sent_at: AwareDatetime
    state: Literal["running", "waiting"] = "running"

class HeartbeatResult(BaseModel):
    status: str
    counted: bool
    revived: bool = False
```

- [ ] Test: defaults; `org.yaml: run_health` loads; a window under three beats is refused naming both values.
- [ ] Test: `run_settings` carries `run_health`.
- [ ] Implement; run; commit.

## Task 2: Heartbeat route and ingest waiting

**Files:** `control-plane/src/agentic_cp/migrations/versions/m3_07_run_health.py`, `tables.py`, `ingest/health.py` (new), `ingest/api.py`, `ingest/store.py`, `openapi.json`; tests `control-plane/tests/test_run_health.py`, `control-plane/tests/isolation.py` (EndpointCase).

- [ ] Tests: a beat of the starter counts and moves `last_heartbeat_at`; a stranger without `run.cancel.any` is 403; an unknown run is 404; another tenant's run is 404 (EndpointCase); `state: waiting` sets `waiting`, `running` clears it; a batch ending in `gate_waiting` sets `waiting`, a later batch without one leaves it; an old queued beat is not counted and never revives; a fresh beat revives a stale run with `run.revived`; events of a stale run leave it stale; `run_closed` closes it.
- [ ] Implement; regenerate openapi; run; commit.

## Task 3: Zombie sweep

**Files:** `ingest/health.py`, `policy_sync/worker.py`, `notify/templates.py`, `notify/templates/run_stale.{txt,html}`; test `control-plane/tests/test_run_health.py`.

- [ ] Tests (fake clock): Review Focus 1 and 2; the mail is queued once per stale episode; a service account's run is audited without mail; `zombie_job` is in `EXTRA_JOBS` before `deliver_job`.
- [ ] Implement; run; commit.

## Task 4: CLI client and heartbeat module

**Files:** `cli/src/agentic_cli/control_plane.py` (`heartbeat`, DF-706), `cli/src/agentic_cli/heartbeat.py` (new); tests `cli/tests/test_heartbeat.py`, `cli/tests/test_control_plane_client.py`.

- [ ] Tests: a beat writes `heartbeat.json` and posts `beat_at`/`sent_at`/`state`; a raising poster is logged once and never raised; `finish("awaiting_gate")` posts one `waiting` beat, `finish("completed")` none; the thread beats at once and stops on exit; `local_state` alive / stale / None; `kill_state` without a body raises `ControlPlaneUnavailable` (DF-706).
- [ ] Implement; run; commit.

## Task 5: Wiring into run, resume and gate verbs; local readers

**Files:** `cli/src/agentic_cli/runtime.py`, `commands/runs.py`, `commands/gates.py`, `core/src/agentic/telemetry/logs.py` (`LogTail.held`), `core/src/agentic/engine/state.py` (DF-280); tests `cli/tests/run/test_run_heartbeat.py`, `cli/tests/run/test_logs_command.py`, `core/tests/engine/test_state.py`.

- [ ] Tests: a run leaves a fresh `heartbeat.json`; in org mode with an unreachable control plane the run still completes; `logs --follow` on a `running` run with a stale beat file prints the last line and exits 1; plain `logs` warns about held-back bytes; `RunStore.load` retries a `PermissionError`.
- [ ] Implement; run; commit.

## Task 6: Contract test against the real control plane

**Files:** `control-plane/tests/contract/test_cli_against_control_plane.py`.

- [ ] Test: the CLI's `ControlPlaneClient.heartbeat` against the live app: counted, then the sweep with the clock moved marks a run with no beats stale.
- [ ] Run; commit.

## Task 7: DF-697 - a standalone run resumes and records the policy change

**Files:** `commons/src/sdlc_commons/ledger.py` (FIELDS), `cli/src/agentic_cli/skill_activity.py` (`StampingRecorder.stamp_next`), `cli/src/agentic_cli/runtime.py` (`check_same_policy`), `commands/runs.py`, `commands/gates.py`; tests `cli/tests/run/test_policy_change.py`, `commons/tests/test_ledger_adapter.py`.

- [ ] Tests: approve after a policy edit continues; the first event after the decision carries `ext.agentic.policy_from` = start version and `policy_to` = new version, its `step` set; a second resume without an edit stamps nothing; the warning names both versions; `verify_entries` accepts the stamped chain.
- [ ] Implement; run; commit.

## Task 8: DF-707 and the full check

**Files:** `control-plane/src/agentic_cp/settings.py`, `core/src/agentic/orgapi/models.py` (`KillState.ttl_seconds` description), `control-plane/openapi.json`.

- [ ] Reword; regenerate openapi; `uv run python scripts/check.py --all` until green; record the result.
