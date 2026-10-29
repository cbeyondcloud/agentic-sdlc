"""`GET /v1/cost/chargeback` (M3-01): the monthly chargeback per team as JSON or CSV. Its
totals equal the SQL view's and the gateway's bill; the org needs `dashboard.view.org`, one
team `dashboard.view.team` there (or the org permission); every export is audited."""

import csv
import io
from decimal import Decimal

import pytest
from apiclient import member
from roi_seed import CALLS as ROI_CALLS
from roi_seed import GW_A, GW_B, OCT, SEP, a_calls, cost, month_of, seed_roi
from sqlalchemy import text, update

from agentic.orgapi.cost import CSV_COLUMNS, Chargeback
from agentic_cp.audit import read_records, verify
from agentic_cp.dashboards.link import link_usage
from agentic_cp.tables import tenants
from agentic_cp.tenancy.context import tenant_tx

pytestmark = pytest.mark.anyio


@pytest.fixture
async def cost_api(api, make_gateway):
    """The app with the executive cost seed in tenants a and b and its gateway linked."""
    async with api.db.owner.begin() as conn:
        for tenant, gateway in ((api.a.id, GW_A), (api.b.id, GW_B)):
            await conn.execute(
                update(tenants).where(tenants.c.id == tenant).values(gateway_tenant=gateway)
            )
    await seed_roi(api.db.owner, api.a.id, api.b.id)
    gateway = await make_gateway(calls=ROI_CALLS)
    async with api.db.owner.begin() as conn:
        await link_usage(conn, api.db.schema, gateway.db)
    return api


async def _view(api, month: str) -> list[dict]:
    async with tenant_tx(api.db.owner, api.a.id) as conn:
        found = await conn.execute(
            text("select * from dash_chargeback where month = :m order by team"), {"m": month}
        )
        return [dict(row._mapping) for row in found]


async def test_csv_totals_match_the_sql_view_and_the_gateway(cost_api):
    api = cost_api
    for month in (SEP, OCT):
        reply = await api.get(f"/v1/cost/chargeback?month={month}&format=csv", api.a.admin_token)
        assert reply.status_code == 200, reply.text
        assert reply.headers["content-type"].startswith("text/csv")
        assert f"chargeback-tenant-a-org-{month}.csv" in reply.headers["content-disposition"]
        rows = list(csv.DictReader(io.StringIO(reply.text)))
        assert tuple(rows[0]) == CSV_COLUMNS
        view = await _view(api, month)
        assert [r["team"] for r in rows] == [v["team"] for v in view]
        for column in ("cost_usd", "unattributed_usd", "attributed_usd"):
            from_csv = sum(Decimal(r[column]) for r in rows if r[column])
            from_sql = sum(v[column] for v in view if v[column] is not None)
            assert from_csv == from_sql, (month, column)
        gateway = cost(c for c in a_calls() if month_of(c) == month)
        assert sum(Decimal(r["cost_usd"]) for r in rows if r["cost_usd"]) == gateway


async def test_json_report_carries_rows_and_totals(cost_api):
    api = cost_api
    reply = await api.get(f"/v1/cost/chargeback?month={OCT}", api.a.admin_token)
    assert reply.status_code == 200, reply.text
    report = Chargeback.model_validate(reply.json())
    assert report.month == OCT and report.team is None
    assert report.totals.cost_usd == cost(c for c in a_calls() if month_of(c) == OCT)
    assert "platform" not in {row.team for row in report.rows}  # tenant b's team


async def test_default_month_is_the_previous_calendar_month(cost_api):
    api = cost_api
    reply = await api.get("/v1/cost/chargeback", api.a.admin_token)
    assert reply.status_code == 200, reply.text
    now = api.clock()
    year, month = (now.year, now.month - 1) if now.month > 1 else (now.year - 1, 12)
    assert reply.json()["month"] == f"{year:04d}-{month:02d}"


async def test_one_team_and_rbac(cost_api):
    api = cost_api
    developer, _ = await member(api, api.a, "dev@a.example.test", "developer", "payments")
    lead, _ = await member(api, api.a, "lead@a.example.test", "team-lead", "payments")
    executive, _ = await member(api, api.a, "exec@a.example.test", "exec-viewer")
    org = f"/v1/cost/chargeback?month={OCT}"
    assert (await api.get(org, developer)).status_code == 403
    assert (await api.get(org, lead)).status_code == 403
    assert (await api.get(org, executive)).status_code == 200
    own = await api.get(f"{org}&team=payments", lead)
    assert own.status_code == 200, own.text
    assert {row["team"] for row in own.json()["rows"]} == {"payments"}
    assert (await api.get(f"{org}&team=search", lead)).status_code == 403
    assert (await api.get(f"{org}&team=search", executive)).status_code == 200


async def test_every_export_is_audited(cost_api):
    api = cost_api
    await api.get(f"/v1/cost/chargeback?month={OCT}&format=csv&team=payments", api.a.admin_token)
    async with tenant_tx(api.db.owner, api.a.id) as conn:
        records = await read_records(conn)
        assert await verify(conn) == []
    last = records[-1]
    assert last.event == "cost.chargeback_exported"
    assert last.team == "payments"
    assert last.after["month"] == OCT and last.after["format"] == "csv"


@pytest.mark.parametrize(
    "query", ["month=2026-13", "month=oct", "format=xml", "team=Not%20A%20Team"]
)
async def test_bad_parameters_are_refused(cost_api, query):
    reply = await cost_api.get(f"/v1/cost/chargeback?{query}", cost_api.a.admin_token)
    assert reply.status_code == 422, reply.text
