"""Auto-trip (M2-11, Review Focus 5): a team whose measured spend rate or error rate passes
its policy threshold is paused (a team pause switch by `system:auto-trip`), its leads are
mailed and the trip is audited; hysteresis keeps it from flapping, it resets only below
the reset level (and, with auto-clear, only after the hold time); spend no step reported
is unmeasured and never trips."""

from __future__ import annotations

import tempfile
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from apiclient import member
from policy_repos import commit, make_policy_repo, url, write
from sqlalchemy import select
from test_budget_alerts import Run

from agentic_cp.alerts import autotrip
from agentic_cp.alerts.autotrip import (
    AutoTripSettings,
    Measure,
    Sample,
    decide,
    evaluate_tenant,
    measure_samples,
)
from agentic_cp.audit import read_records
from agentic_cp.killswitch.store import AUTO_TRIP_ACTOR
from agentic_cp.policy_sync import worker
from agentic_cp.tables import auto_trips, kill_switches, notifications
from agentic_cp.tenancy.context import tenant_tx

T0 = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)
HOLD = timedelta(minutes=30)


# -- the state machine, with synthetic series --


def _run(series, *, threshold="10", auto_clear=False, cleared_at=None, minutes=5):
    """Feed `series` (one value per `minutes`) through `decide`; returns the actions. A
    trip sets a switch; `cleared_at` is the index at which a person clears it."""
    state, switch, tripped_at, actions = "armed", False, None, []
    for index, raw in enumerate(series):
        now = T0 + timedelta(minutes=minutes * index)
        if cleared_at is not None and index == cleared_at:
            switch = False
        value = None if raw is None else Decimal(str(raw))
        step = decide(
            state,
            value,
            Decimal(threshold) if threshold is not None else None,
            Decimal("0.5"),
            switch_active=switch,
            tripped_at=tripped_at,
            now=now,
            hold=HOLD,
            auto_clear=auto_clear,
        )
        actions.append(step.action)
        if step.action == "trip":
            switch, tripped_at = True, now
        if step.action == "clear":
            switch = False
        state = step.state
    return actions


def test_rising_series_trips_once():
    actions = _run([1, 4, 8, 11, 15, 20, 30])
    assert actions.count("trip") == 1 and actions[3] == "trip"


def test_oscillating_series_trips_once():
    actions = _run([9, 11, 9, 11, 9, 11, 9, 11], auto_clear=True)
    assert actions.count("trip") == 1
    assert "clear" not in actions  # never below the reset level (5)


def test_resets_only_below_reset_level():
    # a person clears at index 2; the value stays above 5 until index 5
    actions = _run([12, 12, 9, 8, 6, 4, 12], cleared_at=2)
    assert actions[:3] == ["trip", None, "disarm"]
    assert actions[3:5] == [None, None]  # disarmed: no re-trip while above the reset level
    assert actions[5] == "rearm"
    assert actions[6] == "trip"  # armed again, a new crossing trips again


def test_auto_clear_waits_for_hold():
    # below the reset level from index 1, but the hold is 30 minutes (6 steps of 5)
    actions = _run([12, 2, 2, 2, 2, 2, 2, 2], auto_clear=True)
    assert actions[0] == "trip"
    assert actions[1:6] == [None] * 5
    assert actions[6] == "clear"
    assert actions[7] is None


def test_human_clear_disarms_until_calm():
    actions = _run([12, 15, 15, 15, None], cleared_at=1)
    assert actions == ["trip", "disarm", None, None, "rearm"]


def test_unmeasured_spend_never_trips():
    assert _run([None] * 10) == [None] * 10
    assert _run([50, 50], threshold=None) == [None, None]


def test_human_clear_when_already_calm_rearms_at_once():
    assert _run([12, 1], cleared_at=1) == ["trip", "rearm"]


# -- measuring --


def _sample(minutes_ago, *, usd=None, outcome="completed", team="payments", now=T0):
    return Sample(team=team, at=now - timedelta(minutes=minutes_ago), outcome=outcome, usd=usd)


def test_measure_reports_null_without_costs():
    found = measure_samples([_sample(5), _sample(10)], since=T0 - timedelta(hours=1), **_m())
    assert found["payments"].spend_per_hour is None  # unmeasured, never 0
    assert found["payments"].error_rate is None  # fewer than min_steps


def _m(window=60, min_steps=4):
    return {"window": timedelta(minutes=window), "min_steps": min_steps}


def test_measure_rates():
    samples = [
        _sample(5, usd="2.50"),
        _sample(10, usd="2.50", outcome="error"),
        _sample(15, outcome="error"),
        _sample(20, outcome="skipped"),  # never ran: not a step in the rate
        _sample(25),
        _sample(90, usd="100"),  # happened before the window (a late upload)
        _sample(5, usd="9", team="search"),
    ]
    found = measure_samples(samples, since=T0 - timedelta(minutes=30), **_m(window=30))
    assert found["payments"].spend_per_hour == Decimal("10.00")  # 5 USD in 30 minutes
    assert found["payments"].error_rate == Decimal("0.5")  # 2 of 4 steps that ran
    assert found["payments"].steps == 4
    assert found["search"].spend_per_hour == Decimal("18")
    assert found["search"].error_rate is None


def test_settings_give_each_team_its_thresholds():
    stored = {
        "spend_usd_per_hour": "50",
        "error_rate": None,
        "window_minutes": 30,
        "min_steps": 5,
        "reset_ratio": "0.4",
        "hold_minutes": 10,
        "auto_clear": True,
        "teams": {"payments": {"spend_usd_per_hour": "20", "error_rate": "0.2"}},
    }
    payments = AutoTripSettings.for_team(stored, "payments")
    assert (payments.spend, payments.errors) == (Decimal(20), Decimal("0.2"))
    assert payments.window == timedelta(minutes=30) and payments.hold == timedelta(minutes=10)
    assert payments.reset_ratio == Decimal("0.4") and payments.auto_clear
    other = AutoTripSettings.for_team(stored, "search")
    assert (other.spend, other.errors) == (Decimal(50), None)
    assert AutoTripSettings.for_team(None, "payments") is None
    assert AutoTripSettings.for_team({}, "payments") is None


# -- against the database --


async def _policy(api, tenant, org_extra: str) -> None:
    repo = make_policy_repo(Path(tempfile.mkdtemp(prefix="cp-autotrip-")) / "policy")
    org = repo / "policy" / "org.yaml"
    write(repo, "policy/org.yaml", org.read_text(encoding="utf-8") + org_extra)
    commit(repo, "auto trip")
    made = await api.put(
        "/v1/tenant/policy-source", tenant.admin_token, {"repo": url(repo), "branch": "main"}
    )
    assert made.status_code == 200 and made.json()["sync"]["kind"] == "activated", made.text


SPEND_POLICY = "auto_trip:\n  spend_usd_per_hour: 10\n  window_minutes: 60\n"


@pytest.fixture
def made_runs():
    made = []

    def make(run_id):
        made.append(Run(run_id))
        return made[-1]

    yield make
    for run in made:
        run.close()


async def _evaluate(api, tenant):
    async with tenant_tx(api.db.app, tenant.id) as conn:
        return await evaluate_tenant(conn, api.clock)


async def _state(api, tenant):
    async with tenant_tx(api.db.app, tenant.id) as conn:
        trips = list(await conn.execute(select(auto_trips)))
        switches = list(await conn.execute(select(kill_switches)))
        mails = list(
            await conn.execute(select(notifications).where(notifications.c.kind == "auto_trip"))
        )
        records = [
            r for r in await read_records(conn) if r.event.startswith(("autotrip.", "killswitch."))
        ]
    return trips, switches, mails, records


@pytest.mark.anyio
async def test_trip_pauses_team_mails_and_audits(api, made_runs):
    await _policy(api, api.a, SPEND_POLICY)
    await member(api, api.a, "lead@a.example.test", "team-lead", "payments")
    dev, _ = await member(api, api.a, "dev@a.example.test", "developer", "payments")
    run = made_runs("run-spend")
    run.step("6")
    run.step("6.5")
    await run.ingest(api, dev)
    assert await _evaluate(api, api.a) == ["autotrip.tripped"]
    assert await _evaluate(api, api.a) == []  # still tripped: nothing new
    trips, switches, mails, records = await _state(api, api.a)
    [switch] = switches
    assert (switch.scope_kind, switch.scope_value, switch.team) == ("team", "payments", "payments")
    assert switch.mode == "pause" and switch.created_by == AUTO_TRIP_ACTOR
    assert switch.reason.startswith("auto-trip: spend rate 12.50 USD per hour")
    [trip] = trips
    assert (trip.team, trip.kind, trip.state) == ("payments", "spend", "tripped")
    assert trip.switch_id == switch.id and trip.value == "12.50" and trip.threshold == "10"
    [mail] = mails
    assert mail.recipient == "lead@a.example.test" and "payments" in mail.subject
    events = [r.event for r in records]
    assert events == ["killswitch.set", "autotrip.tripped"]
    set_record, tripped = records
    assert set_record.actor == AUTO_TRIP_ACTOR and set_record.actor_kind == "system"
    assert tripped.team == "payments" and tripped.after["kind"] == "spend"
    assert tripped.after["value"] == "12.50" and tripped.after["switch"] == str(switch.id)
    # the CLI sees the pause like any other
    state = await api.get("/v1/killswitch/state?team=payments", dev)
    assert [s["id"] for s in state.json()["switches"]] == [str(switch.id)]
    # tenant b saw nothing
    assert await _state(api, api.b) == ([], [], [], [])


@pytest.mark.anyio
async def test_error_rate_trips_with_enough_steps(api, made_runs):
    await _policy(api, api.a, "auto_trip:\n  error_rate: 0.5\n  min_steps: 4\n")
    dev, _ = await member(api, api.a, "dev@a.example.test", "developer", "payments")
    run = made_runs("run-errors")
    for _ in range(3):
        run.ledger.record("step_finished", "build", outcome="error")
    await run.ingest(api, dev)
    assert await _evaluate(api, api.a) == []  # 3 steps: below min_steps, unmeasured
    run.ledger.record("step_finished", "build", outcome="error")
    await run.ingest(api, dev)
    assert await _evaluate(api, api.a) == ["autotrip.tripped"]
    trips, switches, _, _ = await _state(api, api.a)
    assert [t.kind for t in trips] == ["errors"] and trips[0].value == "1"
    assert switches[0].reason.startswith("auto-trip: error rate 100% of 4 steps")


@pytest.mark.anyio
async def test_unmeasured_team_never_trips_on_spend(api, made_runs):
    await _policy(api, api.a, "auto_trip:\n  spend_usd_per_hour: 0.01\n")
    dev, _ = await member(api, api.a, "dev@a.example.test", "developer", "payments")
    run = made_runs("run-free")
    for _ in range(5):
        run.ledger.record("step_finished", "build", outcome="completed")
    await run.ingest(api, dev)
    assert await _evaluate(api, api.a) == []
    assert (await _state(api, api.a))[1] == []


@pytest.mark.anyio
async def test_auto_clear_clears_switch_and_audits(api, made_runs):
    await _policy(api, api.a, SPEND_POLICY + "  auto_clear: true\n  hold_minutes: 30\n")
    dev, _ = await member(api, api.a, "dev@a.example.test", "developer", "payments")
    run = made_runs("run-hot")
    run.step("20")
    await run.ingest(api, dev)
    assert await _evaluate(api, api.a) == ["autotrip.tripped"]
    api.clock.advance(timedelta(minutes=20))
    assert await _evaluate(api, api.a) == []  # still within the window and the hold
    api.clock.advance(timedelta(hours=2))  # the spend left the window: unmeasured, calm
    assert await _evaluate(api, api.a) == ["autotrip.cleared"]
    trips, switches, _, records = await _state(api, api.a)
    assert trips[0].state == "armed" and trips[0].switch_id is None
    assert switches[0].cleared_by == AUTO_TRIP_ACTOR
    assert [r.event for r in records] == [
        "killswitch.set",
        "autotrip.tripped",
        "killswitch.cleared",
        "autotrip.cleared",
    ]
    verified = await api.get("/v1/audit/verify", api.a.admin_token)
    assert verified.json()["problems"] == []


@pytest.mark.anyio
async def test_rearm_after_human_clear(api, made_runs):
    await _policy(api, api.a, SPEND_POLICY)
    dev, _ = await member(api, api.a, "dev@a.example.test", "developer", "payments")
    run = made_runs("run-rearm")
    run.step("20")
    await run.ingest(api, dev)
    assert await _evaluate(api, api.a) == ["autotrip.tripped"]
    switch = (await _state(api, api.a))[1][0]
    cleared = await api.delete(f"/v1/killswitch/{switch.id}?reason=handled", api.a.admin_token)
    assert cleared.status_code == 200, cleared.text
    assert await _evaluate(api, api.a) == ["autotrip.disarmed"]  # still hot: no re-trip
    assert await _evaluate(api, api.a) == []
    api.clock.advance(timedelta(hours=2))
    assert await _evaluate(api, api.a) == ["autotrip.rearmed"]
    trips = (await _state(api, api.a))[0]
    assert trips[0].state == "armed"


@pytest.mark.anyio
async def test_no_policy_or_no_thresholds_does_nothing(api, made_runs):
    dev, _ = await member(api, api.a, "dev@a.example.test", "developer", "payments")
    run = made_runs("run-nopolicy")
    run.step("500")
    await run.ingest(api, dev)
    assert await _evaluate(api, api.a) == []  # no active policy
    await _policy(api, api.a, "")
    assert await _evaluate(api, api.a) == []  # auto_trip off


def test_job_is_registered_with_the_worker():
    assert autotrip.auto_trip_job in worker.EXTRA_JOBS
    assert worker.EXTRA_JOBS.index(autotrip.auto_trip_job) < worker.EXTRA_JOBS.index(
        worker.deliver_job
    )  # its mail goes out in the same pass


def test_measure_type_is_exported():
    assert Measure(spend_per_hour=None, error_rate=None, steps=0).value("spend") is None
