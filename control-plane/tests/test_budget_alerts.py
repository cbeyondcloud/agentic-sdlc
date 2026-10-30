"""Budget threshold alerts (M2-16, Review Focus 5) and gate-waiting mail (M2-07): ingest adds
each step's cost to its run; crossing 80 % or 100 % of the run's budget, or of its team's
monthly budget, alerts once - never again on replay - by mail and through every alert sink;
a run waiting at a gate mails its starter and the people holding the gate's role."""

import tempfile
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from apiclient import Api, member
from policy_repos import commit, make_policy_repo, url, write
from sqlalchemy import select, update

from agentic_cp.alerts.budget import crossed
from agentic_cp.audit import read_records
from agentic_cp.tables import notifications, runs
from agentic_cp.tenancy.context import tenant_tx
from sdlc_commons import ledger as ledger_module
from sdlc_commons.gates import ZyrofloGates
from sdlc_commons.ledger import RunIdentity, RunLedger

pytestmark = pytest.mark.anyio


def test_crossed_names_each_threshold_passed():
    assert crossed(Decimal(0), Decimal("7.99"), Decimal(10)) == []
    assert crossed(Decimal(0), Decimal(8), Decimal(10)) == [80]
    assert crossed(Decimal(8), Decimal("9.5"), Decimal(10)) == []
    assert crossed(Decimal("9.5"), Decimal(12), Decimal(10)) == [100]
    assert crossed(Decimal(1), Decimal(10), Decimal(10)) == [80, 100]
    assert crossed(Decimal(1), Decimal(10), Decimal(0)) == []


class Run:
    """A real run ledger whose events are ingested batch by batch."""

    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        self.ledger = RunLedger(Path(tempfile.mkdtemp(prefix="cp-alerts-")), run_id)
        self.ledger.open(
            identity=RunIdentity(team="payments", policy_version="git:" + "c" * 40),
            kind="feature",
            project="demo",
            owner="payments",
            criticality="low",
        )
        self.sent = 0

    def step(self, usd: str, name: str = "build") -> None:
        self.ledger.record("step_finished", name, outcome="completed", cost={"usd": usd})

    def gate(self, gate: str, role: str | None = None) -> None:
        ZyrofloGates(self.ledger).waiting(gate, gate, role=role)

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


@pytest.fixture
def made_runs():
    made = []

    def make(run_id):
        made.append(Run(run_id))
        return made[-1]

    yield make
    for run in made:
        run.close()


async def _set(api, tenant, run_id, **values):
    async with tenant_tx(api.db.owner, tenant.id) as conn:
        await conn.execute(update(runs).where(runs.c.run_id == run_id).values(**values))


async def _mails(api, tenant, kind="budget.threshold"):
    async with tenant_tx(api.db.app, tenant.id) as conn:
        found = select(notifications).where(notifications.c.kind == kind)
        return list(await conn.execute(found.order_by(notifications.c.created_at)))


async def _crossings(api, tenant):
    async with tenant_tx(api.db.app, tenant.id) as conn:
        return [r for r in await read_records(conn) if r.event == "budget.threshold_crossed"]


async def test_a_run_alerts_once_at_80_and_once_at_100(api, made_runs):
    dev, _ = await member(api, api.a, "dev@a.example.test", "developer", "payments")
    run = made_runs("run-budget")
    await run.ingest(api, dev)
    await _set(api, api.a, run.run_id, budget_usd=Decimal(10))
    run.step("8.50")
    await run.ingest(api, dev)
    [mail] = await _mails(api, api.a)
    assert mail.recipient == "dev@a.example.test" and "80 %" in mail.subject
    # DF-577: a run's budget pauses the run; the gateway refuses nothing for it
    assert "the run pauses" in mail.body_text and "gateway refuses" not in mail.body_text
    assert "--extra-budget" in mail.body_text and "the run pauses" in mail.body_html
    await run.ingest(api, dev, replay=True)  # a replay counts nothing again
    run.step("0.50")
    await run.ingest(api, dev)  # 9.00: no new threshold
    assert len(await _mails(api, api.a)) == 1
    run.step("2")
    await run.ingest(api, dev)
    mails = await _mails(api, api.a)
    assert sorted("100 %" in m.subject for m in mails) == [False, True]  # one each
    async with tenant_tx(api.db.app, api.a.id) as conn:
        spent = (await conn.execute(select(runs.c.spent_usd))).scalar_one()
    assert spent == Decimal("11.00")
    records = await _crossings(api, api.a)
    assert [(r.after["scope"], r.after["threshold"]) for r in records] == [
        ("run", 80),
        ("run", 100),
    ]


async def test_one_batch_crossing_both_thresholds_alerts_both(api, made_runs):
    dev, _ = await member(api, api.a, "dev@a.example.test", "developer", "payments")
    run = made_runs("run-jump")
    await run.ingest(api, dev)
    await _set(api, api.a, run.run_id, budget_usd=Decimal(10))
    run.step("12")
    await run.ingest(api, dev)
    assert len(await _mails(api, api.a)) == 2


async def test_a_run_without_a_budget_never_alerts(api, made_runs):
    dev, _ = await member(api, api.a, "dev@a.example.test", "developer", "payments")
    run = made_runs("run-free")
    run.step("500")
    await run.ingest(api, dev)
    assert await _mails(api, api.a) == [] and await _crossings(api, api.a) == []


async def _team_budget(api, tenant, usd: str) -> None:
    repo = make_policy_repo(Path(tempfile.mkdtemp(prefix="cp-alerts-")) / "policy")
    write(repo, "policy/teams/payments.yaml", f"monthly_budget_usd: {usd}\n")
    commit(repo, "team budget")
    made = await api.put(
        "/v1/tenant/policy-source", tenant.admin_token, {"repo": url(repo), "branch": "main"}
    )
    assert made.status_code == 200 and made.json()["sync"]["kind"] == "activated", made.text


async def test_team_spend_this_month_alerts_the_team_leads(api, made_runs):
    await _team_budget(api, api.a, "100")
    await member(api, api.a, "lead@a.example.test", "team-lead", "payments")
    dev, _ = await member(api, api.a, "dev@a.example.test", "developer", "payments")
    old = made_runs("run-old")
    await old.ingest(api, dev)
    await _set(api, api.a, old.run_id, opened_at=api.clock.now - timedelta(days=40))
    old.step("70")
    await old.ingest(api, dev)  # opened last month: not this month's spend
    first = made_runs("run-a")
    first.step("50")
    await first.ingest(api, dev)
    assert await _mails(api, api.a) == []
    second = made_runs("run-b")
    second.step("35")
    await second.ingest(api, dev)
    [mail] = await _mails(api, api.a)
    assert mail.recipient == "lead@a.example.test"
    assert "Team payments" in mail.subject and "80 %" in mail.subject
    assert "gateway refuses" in mail.body_text and "the run pauses" not in mail.body_text
    [record] = await _crossings(api, api.a)
    assert record.after["scope"] == "team" and record.after["period"] == api.clock.now.strftime(
        "%Y-%m"
    )


class Recording:
    """An alert sink that keeps what it is given (the webhook seam)."""

    def __init__(self) -> None:
        self.alerts = []

    async def deliver(self, conn, alert, *, clock) -> None:
        self.alerts.append(alert)


async def test_every_sink_receives_each_alert(api, made_runs):
    sink = Recording()
    hooked = Api(api.db, api.clock, api.a, api.b, alert_webhook=sink)
    try:
        dev, _ = await member(hooked, api.a, "dev@a.example.test", "developer", "payments")
        run = made_runs("run-sinks")
        await run.ingest(hooked, dev)
        await _set(api, api.a, run.run_id, budget_usd=Decimal(10))
        run.step("10")
        await run.ingest(hooked, dev)
        await run.ingest(hooked, dev, replay=True)
    finally:
        await hooked.http.aclose()
    assert [(a.scope, a.threshold, a.run_id) for a in sink.alerts] == [
        ("run", 80, "run-sinks"),
        ("run", 100, "run-sinks"),
    ]
    assert len(await _mails(api, api.a)) == 2


async def test_another_tenants_spend_never_alerts_this_one(api, made_runs):
    dev_b, _ = await member(api, api.b, "dev@b.example.test", "developer", "payments")
    run = made_runs("run-b-only")
    await run.ingest(api, dev_b)
    await _set(api, api.b, run.run_id, budget_usd=Decimal(1))
    run.step("5")
    await run.ingest(api, dev_b)
    assert await _mails(api, api.a) == [] and len(await _mails(api, api.b)) == 2


async def test_a_gate_wait_mails_the_approver_role_holders_of_the_runs_team(api, made_runs):
    """Review F5: the person who must act is the approver - the gate's role holders in the
    run's team are mailed with the starter, once per wait; another team's lead is not."""
    await member(api, api.a, "lead@a.example.test", "team-lead", "payments")
    await member(api, api.a, "lead2@a.example.test", "team-lead", "payments")
    await member(api, api.a, "other-lead@a.example.test", "team-lead", "search")
    dev, _ = await member(api, api.a, "dev@a.example.test", "developer", "payments")
    run = made_runs("run-gate-role")
    run.gate("design-approval", role="team-lead")
    await run.ingest(api, dev)
    await run.ingest(api, dev, replay=True)
    mails = await _mails(api, api.a, "gate.waiting")
    assert sorted(m.recipient for m in mails) == [
        "dev@a.example.test",
        "lead2@a.example.test",
        "lead@a.example.test",
    ]
    assert all("team-lead" in m.body_text for m in mails)


async def test_a_gate_wait_mails_the_runs_starter_once(api, made_runs):
    """A wait without an approver role (a batch or invalid-output pause) tells the starter."""
    await member(api, api.a, "lead@a.example.test", "team-lead", "payments")
    dev, _ = await member(api, api.a, "dev@a.example.test", "developer", "payments")
    run = made_runs("run-gate")
    run.gate("design-approval")
    await run.ingest(api, dev)
    await run.ingest(api, dev, replay=True)
    run.gate("design-approval")  # waiting again later: a new wait, a new mail
    await run.ingest(api, dev)
    mails = await _mails(api, api.a, "gate.waiting")
    assert [m.recipient for m in mails] == ["dev@a.example.test", "dev@a.example.test"]
    assert "design-approval" in mails[0].subject
