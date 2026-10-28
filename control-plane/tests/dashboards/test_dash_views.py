"""The dashboard views exist after `migrate`, compile before any gateway is linked, and read
the framework's run events (M2-37)."""

import re
from decimal import Decimal

import pytest
from sqlalchemy import text

from agentic_cp.dashboards.views import CONTRACT_VIEWS, DASH_VIEWS
from agentic_cp.tenancy.context import tenant_tx

pytestmark = pytest.mark.anyio


async def _rows(engine, tenant, sql: str) -> list[dict]:
    async with tenant_tx(engine, tenant) as conn:
        result = await conn.execute(text(sql))
        return [dict(row._mapping) for row in result]


async def test_views_exist_after_migrate(db):
    async with db.owner.connect() as conn:
        found = await conn.execute(
            text("select viewname from pg_views where schemaname = :s"), {"s": db.schema}
        )
        names = {row[0] for row in found}
    assert set(CONTRACT_VIEWS) | set(DASH_VIEWS) <= names


async def test_placeholder_sources_are_empty(dash_db):
    db, seeded = dash_db
    for view in (*CONTRACT_VIEWS, "dash_calls", "dash_gateway_safety"):
        assert await _rows(db.owner, seeded.a, f"select * from {view}") == [], view


async def test_scope_is_the_transaction_tenant(dash_db):
    db, seeded = dash_db
    scope = await _rows(
        db.owner, seeded.a, "select tenant_id, slug, gateway_tenant from dash_scope"
    )
    assert scope == [{"tenant_id": seeded.a, "slug": "tenant-a", "gateway_tenant": "GW-A"}]
    async with db.owner.connect() as conn:  # no tenant set: nothing
        assert (await conn.execute(text("select count(*) from dash_scope"))).scalar() == 0


async def test_step_runs_read_cost_agent_and_duration(linked_db):
    """The seeded ledger, like the engine's, records no agent: each step's agent is the
    agent tag of its first gateway call (review F1)."""
    db, seeded, _ = linked_db
    rows = await _rows(
        db.owner,
        seeded.a,
        "select run_id, step, agent, agent_source, outcome, reported_usd, duration_s "
        "from dash_step_runs order by run_id, seq",
    )
    assert [(r["run_id"], r["step"], r["agent"], r["outcome"]) for r in rows] == [
        ("a-r1", "design", "design@1.0.0", "completed"),
        ("a-r1", "develop", "developer@1.2.0", "invalid"),
        ("a-r1", "develop", "developer@1.2.0", "completed"),
        ("a-r1", "review", "code-review@1.0.0", "completed"),
        ("a-r2", "develop", "developer@1.2.0", "budget"),
    ]
    assert {r["agent_source"] for r in rows} == {"gateway"}
    assert [r["reported_usd"] for r in rows] == [
        Decimal("0.10"),
        Decimal("0.20"),
        Decimal("0.30"),
        None,  # no cost recorded: NULL, never 0
        Decimal("0.05"),
    ]
    assert all(r["duration_s"] == 300 for r in rows)


async def _plan(engine, tenant, sql: str) -> str:
    async with tenant_tx(engine, tenant) as conn:
        return "\n".join(row[0] for row in await conn.execute(text(f"explain (verbose) {sql}")))


@pytest.mark.parametrize(
    "sql",
    [
        "select sum(cost_usd) from dash_calls",
        "select count(*) from dash_gateway_safety",
        "select * from dash_step_agents",
        "select * from dash_agent_cost",
    ],
)
async def test_the_tenant_filter_reaches_the_gateway(linked_db, sql):
    """Review F2: the gateway answers with the tenant's rows only. The tenant predicate is
    in the SQL sent over postgres_fdw, so no other tenant's calls cross the wire."""
    db, seeded, _ = linked_db
    plan = await _plan(db.owner, seeded.a, sql)
    remote = [line for line in plan.splitlines() if "Remote SQL" in line]
    assert remote, plan
    for line in remote:
        assert re.search(r"\(tenant_id = \$\d+(::text)?\)", line), plan


async def test_views_without_an_agent_never_read_the_gateway(linked_db):
    """The step's agent comes from its gateway calls, but views that show no agent (runs,
    run cost, team cost) plan no extra gateway read for it."""
    db, seeded, _ = linked_db
    assert "Foreign Scan" not in await _plan(db.owner, seeded.a, "select * from dash_runs")
    plan = await _plan(db.owner, seeded.a, "select * from dash_run_cost")
    assert plan.count("Foreign Scan") == 1, plan


async def test_a_malformed_cost_is_null_not_an_error(dash_db):
    db, seeded = dash_db
    async with tenant_tx(db.owner, seeded.a) as conn:
        await conn.execute(
            text(
                "insert into run_events (tenant_id, run_id, seq, hash, prev, event, step, at, "
                "entry) values (:t, 'a-r3', 2, 'h', 'p', 'step_finished', 'develop', 'not a time', "
                '\'{"cost": {"usd": "1e400x"}, "outcome": "completed"}\'::jsonb)'
            ),
            {"t": seeded.a},
        )
    rows = await _rows(
        db.owner,
        seeded.a,
        "select reported_usd, finished_at from dash_step_runs where run_id = 'a-r3'",
    )
    assert rows == [{"reported_usd": None, "finished_at": None}]


async def test_gates_and_audit_and_repos_are_read(dash_db):
    db, seeded = dash_db
    gates = await _rows(
        db.owner,
        seeded.a,
        "select event, gate, decision, approver, waited_ms from dash_gates order by seq",
    )
    assert [(g["event"], g["decision"], g["waited_ms"]) for g in gates] == [
        ("gate_waiting", None, None),
        ("gate_decided", "reject", 4000),
        ("gate_waiting", None, None),
        ("gate_decided", "approve", None),
    ]
    audit = await _rows(db.owner, seeded.a, "select event, actor, team from dash_audit")
    assert audit == [{"event": "role.assigned", "actor": "alice@a.test", "team": "payments"}]
    repos = await _rows(db.owner, seeded.a, "select repo_key, team from dash_repos")
    assert repos == [{"repo_key": "git.example.test/a/app", "team": "payments"}]
