"""The executive cost views (M3-01, master plan "Executive cost breakdown"): total cost as
users x sessions per user x turns per session x requests per turn x tokens per request x
cost per token, for the org, each team and each user, per month; reconciled against the
gateway's actual cost; untagged calls on their own unattributed line; unmeasured is NULL."""

from decimal import Decimal

import pytest
from roi_seed import (
    OCT,
    QUEUED,
    QUEUED_CALLS,
    RUNS_A,
    SEP,
    a_calls,
    attributed,
    cost,
    month_of,
    run_of,
    unattributed,
)
from sqlalchemy import text

from agentic_cp.tenancy.context import tenant_tx

pytestmark = pytest.mark.anyio

FACTORS = (
    "users",
    "sessions_per_user",
    "turns_per_session",
    "requests_per_turn",
    "tokens_per_request",
    "cost_per_token",
)
TOLERANCE = Decimal("0.005")


async def _rows(engine, tenant, sql: str) -> list[dict]:
    async with tenant_tx(engine, tenant) as conn:
        return [dict(row._mapping) for row in await conn.execute(text(sql))]


def _close(product, actual) -> bool:
    return abs(product - actual) <= actual * TOLERANCE


def _product(row: dict):
    value = Decimal(1)
    for factor in FACTORS:
        value *= row[factor]
    return value


async def test_product_of_factors_equals_actual_cost_for_org_and_each_user(roi_db):
    db, seeded = roi_db
    org = {
        r["month"]: r for r in await _rows(db.owner, seeded.a, "select * from dash_cost_drivers")
    }
    assert set(org) == {SEP, OCT}
    for month, row in org.items():
        assert row["actual_usd"] == cost(attributed(month))
        assert _close(row["product_usd"], row["actual_usd"]), (month, row)
        assert _close(_product(row), row["actual_usd"]), month  # the shown factors multiply
    users = await _rows(db.owner, seeded.a, "select * from dash_cost_drivers_user")
    measured = [r for r in users if r["actual_usd"] is not None]
    assert {(r["month"], r["user_email"]) for r in measured} == {
        (SEP, "alice@a.test"),
        (SEP, "bob@a.test"),
        (OCT, "alice@a.test"),
        (OCT, "bob@a.test"),
    }
    for row in measured:
        mine = [c for c in attributed(row["month"]) if run_of(c).owner == row["user_email"]]
        assert row["users"] == 1
        assert row["actual_usd"] == cost(mine), row
        assert _close(row["product_usd"], row["actual_usd"]), row
        assert row["data_quality_alert"] is False, row
    teams = await _rows(db.owner, seeded.a, "select * from dash_cost_drivers_team")
    for row in teams:
        if row["actual_usd"] is not None:
            assert _close(row["product_usd"], row["actual_usd"]), row


async def test_turns_count_every_step_execution(roi_db):
    db, seeded = roi_db
    [row] = await _rows(
        db.owner, seeded.a, f"select * from dash_cost_drivers where month = '{OCT}'"
    )
    october = [run for run in RUNS_A if run.opened.strftime("%Y-%m") == OCT]
    assert row["users"] == len({run.owner for run in october}) == 3
    assert row["sessions"] == len(october) == 3
    assert row["turns"] == sum(len(run.steps) for run in october) == 5  # loops count
    assert row["requests"] == len(attributed(OCT))
    assert row["sessions_per_user"] == 1
    assert abs(row["turns_per_session"] - Decimal(5) / 3) < Decimal("1e-12")
    assert row["requests_per_turn"] == Decimal(len(attributed(OCT))) / 5


async def test_untagged_calls_appear_as_unattributed(roi_db):
    db, seeded = roi_db
    calls = await _rows(
        db.owner,
        seeded.a,
        "select request_id, month, unattributed_reason, user_email, cost_usd "
        "from dash_cost_calls where not attributed",
    )
    expected = {c.request_id: c for c in unattributed()}
    assert {r["request_id"] for r in calls} == set(expected)
    for row in calls:
        call = expected[row["request_id"]]
        reason = "no run tag" if call.tags is None else "run not registered"
        assert row["unattributed_reason"] == reason
        assert row["user_email"] is None
        assert row["month"] == month_of(call)
    facts = await _rows(
        db.owner,
        seeded.a,
        "select sum(cost_usd) as usd, sum(calls) as calls from dash_cost_facts "
        "where not attributed",
    )
    assert facts[0]["usd"] == cost(unattributed())
    assert facts[0]["calls"] == len(unattributed())
    for month in (SEP, OCT):
        [org] = await _rows(
            db.owner, seeded.a, f"select * from dash_cost_drivers where month = '{month}'"
        )
        assert org["unattributed_usd"] == cost(unattributed(month))
        assert org["total_usd"] == cost(attributed(month)) + cost(unattributed(month))
    total = sum(
        r["total_usd"] for r in await _rows(db.owner, seeded.a, "select * from dash_cost_drivers")
    )
    assert total == cost(a_calls())  # nothing hidden: the gateway's whole bill


async def test_untagged_calls_raise_the_org_alert(roi_db):
    db, seeded = roi_db
    [october] = await _rows(
        db.owner, seeded.a, f"select * from dash_cost_drivers where month = '{OCT}'"
    )
    share = cost(unattributed(OCT)) / october["total_usd"]
    assert share > TOLERANCE
    assert october["diff_pct"] > Decimal("0.5")
    assert october["data_quality_alert"] is True
    [search] = await _rows(
        db.owner,
        seeded.a,
        f"select * from dash_cost_drivers_team where month = '{SEP}' and team = 'search'",
    )
    assert search["unattributed_usd"] is None
    assert search["data_quality_alert"] is False


async def test_unmeasured_factors_are_null_not_zero(roi_db):
    db, seeded = roi_db
    [cara] = await _rows(
        db.owner, seeded.a, "select * from dash_cost_drivers_user where user_email = 'cara@a.test'"
    )
    # registered, then nothing reached the control plane or the gateway
    assert cara["users"] == 1 and cara["sessions_per_user"] == 1
    for column in (
        "turns",
        "requests_per_turn",
        "turns_per_session",
        "tokens_per_request",
        "cost_per_token",
        "product_usd",
        "actual_usd",
        "total_usd",
        "diff_pct",
    ):
        assert cara[column] is None, column
    assert cara["data_quality_alert"] is False  # nothing measured, nothing to reconcile


async def test_calls_without_events_leave_turns_unmeasured(roi_queued_db):
    """Review F2: a registered run whose calls reached the gateway but none of whose events
    reached the control plane has NULL step executions, never a measured 0."""
    db, seeded = roi_queued_db
    run = QUEUED.run_id
    facts = await _rows(
        db.owner,
        seeded.a,
        f"select executions, calls, cost_usd from dash_cost_facts where run_id = '{run}'",
    )
    assert facts == [
        {"executions": None, "calls": len(QUEUED_CALLS), "cost_usd": cost(QUEUED_CALLS)}
    ]
    [dan] = await _rows(
        db.owner,
        seeded.a,
        f"select * from dash_cost_drivers_user where user_email = '{QUEUED.owner}'",
    )
    assert dan["requests"] == len(QUEUED_CALLS)
    assert dan["actual_usd"] == cost(QUEUED_CALLS)
    for column in ("turns", "turns_per_session", "requests_per_turn", "product_usd"):
        assert dan[column] is None, column
    assert dan["data_quality_alert"] is True  # cost without turns cannot be reconciled
    [october] = await _rows(
        db.owner, seeded.a, f"select * from dash_cost_drivers where month = '{OCT}'"
    )
    assert october["turns"] == 5  # the queued run adds no turn
    assert _close(october["product_usd"], october["actual_usd"]), october


async def test_cached_share_is_null_without_cache_counts(roi_db):
    db, seeded = roi_db
    rows = {
        (r["month"], r["team"]): r
        for r in await _rows(db.owner, seeded.a, "select * from dash_cost_drivers_team")
    }
    assert rows[(SEP, "search")]["cached_share"] is None
    assert rows[(SEP, "payments")]["cached_share"] > 0
    assert rows[(SEP, "payments")]["cache_read_tokens"] == 4000


async def test_chargeback_totals_match_the_gateway(roi_db):
    db, seeded = roi_db
    rows = await _rows(db.owner, seeded.a, "select * from dash_chargeback")
    assert sum(r["cost_usd"] for r in rows if r["cost_usd"] is not None) == cost(a_calls())
    assert sum(r["unattributed_usd"] or 0 for r in rows) == cost(unattributed())
    by_key = {(r["month"], r["team"]): r for r in rows}
    payments = by_key[(OCT, "payments")]
    assert payments["users"] == 2  # alice and cara
    assert payments["runs"] == 2
    unteamed = by_key[(SEP, "(unattributed)")]
    assert unteamed["users"] == 0 and unteamed["runs"] == 0
    assert unteamed["unattributed_usd"] == Decimal("0.020000")


async def test_roi_month_measures_what_the_records_hold(roi_db):
    db, seeded = roi_db
    rows = {
        (r["month"], r["team"]): r
        for r in await _rows(db.owner, seeded.a, "select * from dash_roi_month")
    }
    september = rows[(SEP, "payments")]  # r1: design, develop x2, review; one gate, 1 h
    assert september["runs"] == 1 and september["users"] == 1
    assert september["step_executions"] == 4 and september["step_repeats"] == 1
    assert september["step_rework_rate"] == Decimal("0.25")
    assert september["gate_decisions"] == 1
    assert september["gate_wait_hours"] == 1
    assert september["gate_wait_median_minutes"] == 60
    october = rows[(OCT, "payments")]  # r3 (develop x3) and r5 (nothing arrived)
    assert october["runs"] == 2 and october["users"] == 2
    assert october["step_executions"] == 4 and october["step_repeats"] == 2
    assert october["step_rework_rate"] == Decimal("0.5")
    assert october["gate_decisions"] == 2  # one wait was not recorded: not counted as 0
    assert october["gate_wait_hours"] == Decimal("0.5")
    payments = [c for c in a_calls() if month_of(c) == OCT and c.team == "payments"]
    assert october["cost_usd"] == cost(payments)
    for column in (
        "merged_changes",
        "cost_per_accepted_change_usd",
        "request_to_merge_hours",
        "pr_merge_rate",
        "dora_lead_time_hours",
        "change_failure_rate",
        "gateway_savings_usd",
    ):
        assert october[column] is None, column  # no source yet: unmeasured, never 0
