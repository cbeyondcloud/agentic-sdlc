"""Run health (M3-07, Review Focus 1-3): a CLI driving a run sends heartbeats
(`POST /v1/runs/{run_id}/heartbeat`); the worker marks an open run that sent neither a
heartbeat nor an event for `run_health.stale_minutes` stale - its concurrency slot is free
at once, its starter is mailed and `run.stale` is audited. A run waiting for a person or a
pause switch is never stale. A fresh beat revives a stale run (`run.revived`); an old,
queued beat or a replayed batch never does."""

import tempfile
from datetime import timedelta
from pathlib import Path

import pytest
from apiclient import member
from policy_repos import commit, make_policy_repo, url, write
from sqlalchemy import select

from agentic.orgapi import CLI_VERSION_HEADER
from agentic.policy import load_policy
from agentic_cp import policy_sync
from agentic_cp.audit import read_records
from agentic_cp.ingest import health
from agentic_cp.ingest.health import waits, zombie_job
from agentic_cp.policy_sync import worker
from agentic_cp.policy_sync.service import run_settings
from agentic_cp.tables import notifications, runs
from agentic_cp.tenancy.context import tenant_tx
from sdlc_commons import ledger as ledger_module
from sdlc_commons.gates import ZyrofloGates
from sdlc_commons.ledger import RunIdentity, RunLedger

pytestmark = pytest.mark.anyio

PIPELINE = (
    "name: deliver\nversion: 1.0.0\nregime: team\n"
    "steps:\n  - gate: review\n    approver_role: developer\n"
)
NEW_CLI = {CLI_VERSION_HEADER: "9.9.9"}


# ── helpers ──────────────────────────────────────────────────────────────────


class Run:
    """A real run ledger whose events are ingested batch by batch."""

    def __init__(self, run_id: str, version: str = "git:" + "c" * 40) -> None:
        self.run_id = run_id
        self.ledger = RunLedger(Path(tempfile.mkdtemp(prefix="cp-health-")), run_id)
        self.ledger.open(
            identity=RunIdentity(team="payments", policy_version=version),
            kind="feature",
            project="demo",
            owner="payments",
            criticality="low",
        )
        self.sent = 0

    def step(self, outcome: str = "completed", name: str = "build") -> None:
        self.ledger.record("step_finished", name, outcome=outcome)

    def gate(self, gate: str = "review") -> None:
        ZyrofloGates(self.ledger).waiting(gate, gate)

    def closed(self, status: str = "completed") -> None:
        self.ledger.record("run_closed", "", status=status)

    async def ingest(self, api, token, *, replay: bool = False):
        entries = self.ledger.events()
        batch = entries if replay else entries[self.sent :]
        reply = await api.post(f"/v1/runs/{self.run_id}/events", token, {"entries": batch})
        assert reply.status_code == 200, reply.text
        self.sent = len(entries)
        return reply.json()

    def close(self) -> None:
        self.ledger.release()
        ledger_module._IDENTITIES.pop(self.run_id, None)


@pytest.fixture(autouse=True)
def fresh_identities(monkeypatch):
    """Run identities are process-wide: another module's `r-1` must not leak in."""
    monkeypatch.setattr(ledger_module, "_IDENTITIES", {})


@pytest.fixture
def made_runs():
    made = []

    def make(run_id, **kwargs):
        made.append(Run(run_id, **kwargs))
        return made[-1]

    yield make
    for run in made:
        run.close()


async def beat(api, token, run_id, *, state="running", age=timedelta(0), status=200):
    """A heartbeat sent now that was taken `age` ago (a queued beat when age > 0)."""
    sent = api.clock.now
    body = {"beat_at": (sent - age).isoformat(), "sent_at": sent.isoformat(), "state": state}
    reply = await api.post(f"/v1/runs/{run_id}/heartbeat", token, body)
    assert reply.status_code == status, reply.text
    return reply.json()


async def summary(api, token, run_id):
    reply = await api.get(f"/v1/runs/{run_id}", token)
    assert reply.status_code == 200, reply.text
    return reply.json()


async def sweep(api, tenant=None):
    return await zombie_job(api.db.app, None, (tenant or api.a).id, api.settings, api.clock)


async def audited(api, event, tenant=None):
    async with tenant_tx(api.db.app, (tenant or api.a).id) as conn:
        return [r for r in await read_records(conn) if r.event == event]


async def mails(api, tenant=None):
    async with tenant_tx(api.db.app, (tenant or api.a).id) as conn:
        found = select(notifications).where(notifications.c.kind == "run.stale")
        return list(await conn.execute(found.order_by(notifications.c.created_at)))


async def status_of(api, run_id, tenant=None):
    async with tenant_tx(api.db.owner, (tenant or api.a).id) as conn:
        found = select(runs.c.status).where(runs.c.run_id == run_id)
        return (await conn.execute(found)).scalar_one()


def _policy_repo(path, org_yaml=""):
    repo = make_policy_repo(path)
    write(repo, "pipelines/deliver.yaml", PIPELINE)
    write(repo, "policy/limits.yaml", "concurrency: { per_user: 2, per_team: 5 }\n")
    write(repo, "policy/org.yaml", "allow_repo_pipelines: false\n" + org_yaml)
    commit(repo, "pipelines, limits and run health")
    return repo


async def _sync(api, repo) -> str:
    made = await api.put(
        "/v1/tenant/policy-source", api.a.admin_token, {"repo": url(repo), "branch": "main"}
    )
    assert made.status_code == 200, made.text
    sync = made.json()["sync"]
    assert sync["kind"] in ("activated", "unchanged"), sync
    return sync["version"]


async def register(api, version, run_id):
    import hashlib

    body = {
        "run_id": run_id,
        "team": "payments",
        "repo": "github.com/acme/app",
        "pipeline": "deliver",
        "pipeline_file": "pipelines/deliver.yaml",
        "pipeline_sha256": "sha256:" + hashlib.sha256(PIPELINE.encode()).hexdigest(),
        "policy_version": version,
        "agents": [],
        "models": [],
        "backends": ["native"],
    }
    reply = await api.post("/v1/runs", api.a.admin_token, body, headers=NEW_CLI)
    assert reply.status_code == 200, reply.text
    return reply.json()


@pytest.fixture
async def version(api, tmp_path):
    return await _sync(api, _policy_repo(tmp_path / "policy"))


# ── settings ─────────────────────────────────────────────────────────────────


def test_run_settings_carry_run_health(tmp_path):
    repo = _policy_repo(tmp_path / "policy", "run_health:\n  stale_minutes: 10\n")
    stored = run_settings(load_policy(repo / "policy"), repo)
    assert stored["run_health"] == {"heartbeat_seconds": 60, "stale_minutes": 10}
    assert "run_health" in policy_sync.service.RUN_SETTINGS_KEYS


def test_stale_window_defaults_without_stored_settings():
    assert health.window_from(None) == timedelta(minutes=30)
    assert health.window_from({"run_health": {"stale_minutes": 12}}) == timedelta(minutes=12)


def test_a_batch_waits_when_it_ends_in_a_wait():
    gate = {"event": "gate_waiting"}
    paused = {"event": "step_finished", "outcome": "paused"}
    done = {"event": "step_finished", "outcome": "completed"}
    assert waits([done, gate]) and waits([paused])
    assert not waits([gate, {"event": "gate_decided"}]) and not waits([done])


# ── the heartbeat route (Review Focus 3) ─────────────────────────────────────


async def test_a_beat_of_the_starter_counts(api, made_runs):
    dev, _ = await member(api, api.a, "dev@a.example.test", "developer", "payments")
    run = made_runs("hb-1")
    await run.ingest(api, dev)
    api.clock.advance(timedelta(minutes=5))
    assert await beat(api, dev, "hb-1") == {"status": "open", "counted": True, "revived": False}
    found = await summary(api, dev, "hb-1")
    assert found["last_heartbeat_at"] is not None and found["waiting"] is False


async def test_a_beat_for_someone_elses_run_needs_run_cancel_any(api, made_runs):
    dev, _ = await member(api, api.a, "dev@a.example.test", "developer", "payments")
    other, _ = await member(api, api.a, "other@a.example.test", "developer", "payments")
    run = made_runs("hb-2")
    await run.ingest(api, dev)
    refused = await beat(api, other, "hb-2", status=403)
    assert refused["code"] == "rbac.forbidden"
    await beat(api, api.a.admin_token, "hb-2")  # the org admin holds run.cancel.any


async def test_a_beat_for_an_unknown_run_is_404(api):
    await beat(api, api.a.admin_token, "no-such-run", status=404)


async def test_a_bad_beat_is_refused(api, made_runs):
    run = made_runs("hb-bad")
    await run.ingest(api, api.a.admin_token)
    now = api.clock.now.isoformat()
    for body in (
        {"beat_at": now, "sent_at": now, "state": "dead"},
        {"beat_at": "2026-10-29T10:00:00", "sent_at": now},  # no time zone
        {"beat_at": now, "sent_at": now, "tenant": "b"},
    ):
        reply = await api.post("/v1/runs/hb-bad/heartbeat", api.a.admin_token, body)
        assert reply.status_code == 422, reply.text


async def test_waiting_beats_set_waiting_and_running_beats_clear_it(api, made_runs):
    run = made_runs("hb-3")
    token = api.a.admin_token
    await run.ingest(api, token)
    await beat(api, token, "hb-3", state="waiting")
    assert (await summary(api, token, "hb-3"))["waiting"] is True
    await beat(api, token, "hb-3")
    assert (await summary(api, token, "hb-3"))["waiting"] is False


async def test_a_batch_ending_in_a_wait_sets_waiting(api, made_runs):
    token = api.a.admin_token
    gated, paused = made_runs("hb-gate"), made_runs("hb-paused")
    gated.step()
    gated.gate()
    paused.step(outcome="paused")
    for run in (gated, paused):
        await run.ingest(api, token)
        assert (await summary(api, token, run.run_id))["waiting"] is True


async def test_a_late_batch_does_not_clear_waiting(api, made_runs):
    """Events are delivered in the background, maybe after the CLI's last (waiting) beat;
    a pause switch held before a step records no event. Only a running beat clears it."""
    token = api.a.admin_token
    run = made_runs("hb-late")
    await run.ingest(api, token)
    await beat(api, token, "hb-late", state="waiting")
    run.step()
    await run.ingest(api, token)
    assert (await summary(api, token, "hb-late"))["waiting"] is True


# ── zombie detection (Review Focus 1 and 2) ──────────────────────────────────


async def test_a_run_without_a_heartbeat_is_marked_stale_and_its_slot_freed(api, version):
    for run_id in ("z-1", "z-2"):
        made = await register(api, version, run_id)
        assert made["registered"], [c for c in made["checks"] if not c["passed"]]
    assert not (await register(api, version, "z-3"))["registered"]  # per_user 2
    api.clock.advance(timedelta(minutes=29))
    assert await sweep(api) == []
    api.clock.advance(timedelta(minutes=2))
    assert sorted(await sweep(api)) == ["z-1", "z-2"]
    assert [await status_of(api, r) for r in ("z-1", "z-2")] == ["stale", "stale"]
    assert (await register(api, version, "z-3"))["registered"]  # the slot is free
    records = await audited(api, "run.stale")
    assert sorted(r.run_id for r in records) == ["z-1", "z-2"]
    assert records[0].actor_kind == "system" and records[0].after["stale_minutes"] == 30
    sent = await mails(api)
    assert sorted(m.recipient for m in sent) == ["admin@a.example.test"] * 2
    assert "no heartbeat for 30 minutes" in sent[0].subject
    assert await sweep(api) == []  # once per stale episode
    assert len(await mails(api)) == 2


async def test_a_beating_run_is_not_stale(api, version):
    await register(api, version, "alive")
    for _ in range(4):
        api.clock.advance(timedelta(minutes=20))
        await beat(api, api.a.admin_token, "alive")
        assert await sweep(api) == []
    assert await status_of(api, "alive") == "open"


async def test_a_waiting_run_is_not_stale(api, version, made_runs):
    """Paused is not dead: by the CLI's last beat, or by a wait event (a gate, a paused
    step) when that beat never arrived."""
    token = api.a.admin_token
    await register(api, version, "w-beat")
    await beat(api, token, "w-beat", state="waiting")
    gated = made_runs("w-gate", version=version)
    gated.gate()
    await gated.ingest(api, token)
    api.clock.advance(timedelta(hours=6))
    assert await sweep(api) == []
    assert await status_of(api, "w-beat") == await status_of(api, "w-gate") == "open"
    await beat(api, token, "w-beat")  # resumed: running again, so it can go stale
    api.clock.advance(timedelta(minutes=31))
    assert await sweep(api) == ["w-beat"]


async def test_the_window_comes_from_the_policy(api, tmp_path):
    version = await _sync(api, _policy_repo(tmp_path / "p", "run_health:\n  stale_minutes: 10\n"))
    await register(api, version, "short")
    api.clock.advance(timedelta(minutes=11))
    assert await sweep(api) == ["short"]
    [record] = await audited(api, "run.stale")
    assert record.after["stale_minutes"] == 10


async def test_events_count_as_a_sign_of_life(api, made_runs):
    """A run reported by a CLI that sends no heartbeat (an older release) is judged by its
    events."""
    token = api.a.admin_token
    run = made_runs("old-cli")
    await run.ingest(api, token)
    api.clock.advance(timedelta(minutes=20))
    run.step()
    await run.ingest(api, token)
    api.clock.advance(timedelta(minutes=20))
    assert await sweep(api) == []
    api.clock.advance(timedelta(minutes=11))
    assert await sweep(api) == ["old-cli"]


async def test_tenants_are_swept_separately(api, made_runs):
    run = made_runs("b-run")
    await run.ingest(api, api.b.admin_token)
    api.clock.advance(timedelta(hours=1))
    assert await sweep(api) == []
    assert await status_of(api, "b-run", api.b) == "open"
    assert await sweep(api, api.b) == ["b-run"]


def test_the_sweep_runs_in_the_worker_before_the_mail_goes_out():
    assert zombie_job in worker.EXTRA_JOBS
    assert worker.EXTRA_JOBS.index(zombie_job) < worker.EXTRA_JOBS.index(worker.deliver_job)


# ── a stale run that reports again (Review Focus 3) ──────────────────────────


async def _stale(api, made_runs, run_id):
    run = made_runs(run_id)
    await run.ingest(api, api.a.admin_token)
    api.clock.advance(timedelta(minutes=31))
    assert await sweep(api) == [run_id]
    return run


async def test_an_old_queued_beat_does_not_revive_a_stale_run(api, made_runs):
    await _stale(api, made_runs, "q-1")
    queued = await beat(api, api.a.admin_token, "q-1", age=timedelta(minutes=31))
    assert queued == {"status": "stale", "counted": False, "revived": False}
    assert await status_of(api, "q-1") == "stale"
    assert await audited(api, "run.revived") == []


async def test_an_old_queued_beat_does_not_extend_an_open_run(api, made_runs):
    run = made_runs("q-2")
    await run.ingest(api, api.a.admin_token)
    api.clock.advance(timedelta(minutes=29))
    await beat(api, api.a.admin_token, "q-2", age=timedelta(minutes=29))  # taken at the start
    api.clock.advance(timedelta(minutes=2))
    assert await sweep(api) == ["q-2"]


async def test_a_fresh_beat_revives_a_stale_run(api, made_runs):
    await _stale(api, made_runs, "r-1")
    revived = await beat(api, api.a.admin_token, "r-1")
    assert revived == {"status": "open", "counted": True, "revived": True}
    [record] = await audited(api, "run.revived")
    assert record.run_id == "r-1" and record.actor == "admin@a.example.test"
    assert record.after["state"] == "running"
    api.clock.advance(timedelta(minutes=31))  # a second episode mails again
    assert await sweep(api) == ["r-1"]
    assert len(await mails(api)) == 2


async def test_events_of_a_stale_run_do_not_revive_it_but_closing_does(api, made_runs):
    run = await _stale(api, made_runs, "e-1")
    await run.ingest(api, api.a.admin_token, replay=True)
    run.step()
    await run.ingest(api, api.a.admin_token)  # `agentic sync` of a run that died
    assert await status_of(api, "e-1") == "stale"
    run.closed("stopped")
    await run.ingest(api, api.a.admin_token)
    assert await status_of(api, "e-1") == "stopped"
    assert await beat(api, api.a.admin_token, "e-1") == {
        "status": "stopped",
        "counted": False,
        "revived": False,
    }
