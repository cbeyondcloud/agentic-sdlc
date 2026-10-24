"""Cost reconciles exactly on the seeded database (M2-37, Review Focus 2 and 3): per run,
step, agent and team, the gateway cost sums to the tenant's gateway total; calls without a
run tag are the `(unattributed)` line; unmeasured values are NULL, never 0."""

from collections import defaultdict
from decimal import Decimal

import pytest
from dash_seed import (
    GW_A,
    GW_B,
    STEPS_A,
    UNATTRIBUTED,
    calls_of,
    total,
)
from sqlalchemy import text

from agentic_cp.tenancy.context import tenant_tx

pytestmark = pytest.mark.anyio

COST_VIEWS = {
    "dash_run_cost": ("run_key",),
    "dash_step_cost": ("run_key", "step_key"),
    "dash_agent_cost": ("agent_key",),
    "dash_team_cost": ("team",),
}


async def _rows(engine, tenant, sql: str) -> list[dict]:
    async with tenant_tx(engine, tenant) as conn:
        return [dict(row._mapping) for row in await conn.execute(text(sql))]


def _expected(calls, key) -> dict[tuple, Decimal]:
    sums: dict[tuple, Decimal] = defaultdict(Decimal)
    for call in calls:
        sums[key(call)] += Decimal(call.cost)
    return dict(sums)


def _tag(call, name: str) -> str:
    return (call.tags or {}).get(name) or UNATTRIBUTED


KEYS = {
    "dash_run_cost": lambda c: (_tag(c, "run_id"),),
    "dash_step_cost": lambda c: (_tag(c, "run_id"), _tag(c, "step")),
    "dash_agent_cost": lambda c: (_tag(c, "agent"),),
    "dash_team_cost": lambda c: (c.team or UNATTRIBUTED,),
}


@pytest.mark.parametrize("view", list(COST_VIEWS))
async def test_cost_reconciles_per_dimension(linked_db, view):
    db, seeded, _ = linked_db
    for tenant, gateway in ((seeded.a, GW_A), (seeded.b, GW_B)):
        rows = await _rows(db.owner, tenant, f"select * from {view}")
        measured = [r["gateway_usd"] for r in rows if r["gateway_usd"] is not None]
        assert sum(measured, Decimal(0)) == total(calls_of(gateway)), view
        columns = COST_VIEWS[view]
        got = {tuple(r[c] for c in columns): r["gateway_usd"] for r in rows if r["gateway_usd"]}
        assert got == _expected(calls_of(gateway), KEYS[view]), view


async def test_unattributed_line_holds_untagged_calls(linked_db):
    db, seeded, _ = linked_db
    untagged = [c for c in calls_of(GW_A) if c.tags is None]
    rows = await _rows(
        db.owner,
        seeded.a,
        f"select * from dash_run_cost where run_key = '{UNATTRIBUTED}'",
    )
    assert len(rows) == 1
    line = rows[0]
    assert line["gateway_usd"] == total(untagged)
    assert line["calls"] == len(untagged)
    assert (line["registered"], line["run_id"]) == (False, None)
    tagged_elsewhere = await _rows(
        db.owner, seeded.a, "select registered from dash_run_cost where run_key = 'local-only-1'"
    )
    assert tagged_elsewhere == [{"registered": False}]  # tagged, but no registered run


async def test_unmeasured_values_are_null(linked_db, dash_db):
    db, seeded, _ = linked_db
    idle = await _rows(
        db.owner,
        seeded.a,
        "select calls, gateway_usd, reported_usd, input_tokens from dash_run_cost "
        "where run_key = 'a-r3'",
    )
    assert idle == [
        {"calls": None, "gateway_usd": None, "reported_usd": None, "input_tokens": None}
    ]
    review = await _rows(
        db.owner,
        seeded.a,
        "select reported_usd, gateway_usd from dash_step_cost "
        "where run_key = 'a-r1' and step_key = 'review'",
    )
    assert review == [{"reported_usd": None, "gateway_usd": Decimal("0.070000")}]
    untagged = await _rows(
        db.owner,
        seeded.a,
        f"select input_tokens from dash_run_cost where run_key = '{UNATTRIBUTED}'",
    )
    assert untagged == [{"input_tokens": 1000}]  # one call reported none: summed as unknown


async def test_unlinked_gateway_reads_no_rows_not_zeros(dash_db):
    db, seeded = dash_db
    rows = await _rows(db.owner, seeded.a, "select run_key, calls, gateway_usd from dash_run_cost")
    assert {r["run_key"] for r in rows} == {"a-r1", "a-r2", "a-r3"}
    assert all(r["calls"] is None and r["gateway_usd"] is None for r in rows)


async def test_reported_and_gateway_cost_side_by_side(linked_db):
    """DF-438: the cost the engine reported next to the gateway's, per run and step."""
    db, seeded, _ = linked_db
    rows = await _rows(
        db.owner,
        seeded.a,
        "select run_key, reported_usd, gateway_usd, delta_usd from dash_run_cost "
        "where run_key in ('a-r1', 'a-r2') order by run_key",
    )
    reported = defaultdict(Decimal)
    for step in STEPS_A:
        if step.cost is not None:
            reported[step.run] += Decimal(step.cost)
    gateway = _expected(calls_of(GW_A), KEYS["dash_run_cost"])
    for row in rows:
        run = row["run_key"]
        assert row["reported_usd"] == reported[run]
        assert row["gateway_usd"] == gateway[(run,)]
        assert row["delta_usd"] == gateway[(run,)] - reported[run]
    develop = await _rows(
        db.owner,
        seeded.a,
        "select executions, reported_usd, gateway_usd, agents from dash_step_cost "
        "where run_key = 'a-r1' and step_key = 'develop'",
    )
    assert develop == [
        {
            "executions": 2,
            "reported_usd": Decimal("0.50"),
            "gateway_usd": Decimal("0.510000"),
            "agents": "developer@1.2.0",
        }
    ]


async def test_team_is_the_authenticated_team_not_the_tag(linked_db):
    db, seeded, _ = linked_db
    rows = await _rows(
        db.owner,
        seeded.a,
        "select team, tag_team, cost_usd from dash_calls where tag_team = 'search' "
        "and run_key = 'a-r1'",
    )
    assert rows == [{"team": "payments", "tag_team": "search", "cost_usd": Decimal("0.160000")}]
    no_team = await _rows(
        db.owner, seeded.a, f"select gateway_usd from dash_team_cost where team = '{UNATTRIBUTED}'"
    )
    assert no_team == [{"gateway_usd": Decimal("0.250000")}]


async def test_other_gateway_tenants_never_counted(linked_db):
    db, seeded, _ = linked_db
    rows = await _rows(db.owner, seeded.a, "select request_id from dash_calls")
    assert {r["request_id"] for r in rows} == {c.request_id for c in calls_of(GW_A)}


async def test_a_shared_gateway_tenant_shows_no_gateway_rows(linked_db):
    """Two tenants mapped to one gateway tenant (a misconfiguration) cannot tell whose calls
    are whose: neither sees any, rather than each seeing the other's."""
    db, seeded, _ = linked_db
    async with db.owner.begin() as conn:
        await conn.execute(
            text("update tenants set gateway_tenant = :gw where id = :id"),
            {"gw": GW_A, "id": seeded.b},
        )
    for tenant in (seeded.a, seeded.b):
        assert await _rows(db.owner, tenant, "select request_id from dash_calls") == []
        assert await _rows(db.owner, tenant, "select action from dash_gateway_safety") == []


async def test_agent_quality_counts_executions_gates_and_cost(linked_db):
    db, seeded, _ = linked_db
    rows = await _rows(
        db.owner,
        seeded.a,
        "select agent, executions, completed, failed, limited, repeats, approvals, rejections, "
        "rejection_rate, gateway_usd, usd_per_approval from dash_agent_quality order by agent",
    )
    by_agent = {r["agent"]: r for r in rows}
    developer = by_agent["developer@1.2.0"]
    assert (developer["executions"], developer["completed"], developer["failed"]) == (3, 1, 1)
    assert (developer["limited"], developer["repeats"]) == (1, 1)
    review = by_agent["code-review@1.0.0"]
    assert (review["approvals"], review["rejections"]) == (1, 1)
    assert review["rejection_rate"] == Decimal("0.5000")
    assert review["usd_per_approval"] == Decimal("0.070000")
    assert by_agent["design@1.0.0"]["usd_per_approval"] is None  # no approval: unmeasured
