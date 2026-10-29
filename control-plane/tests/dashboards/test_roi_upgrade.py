"""Upgrade safety of the dashboard views (M3-01, DF-631): a migration that recreates the
contract views leaves them as empty placeholders, and `migrate` points them back at the
gateway tables already imported; `/readyz` says whether the gateway is linked; a reader
bound before `m3_01_roi` reads the new views after the upgrade without re-binding."""

import secrets

import anyio
import httpx
import pytest
from dash_seed import GW_A, calls_of
from sqlalchemy import text

from agentic_cp.__main__ import main
from agentic_cp.app import create_app
from agentic_cp.dashboards.link import relink_contracts
from agentic_cp.dashboards.views import CONTRACTS, M2_37_VIEWS, ROI_VIEWS, placeholder_sql
from agentic_cp.db import downgrade_async, migrate_async
from agentic_cp.settings import Settings
from agentic_cp.tenancy.context import tenant_tx

pytestmark = pytest.mark.anyio


async def _count(engine, tenant, view: str) -> int:
    async with tenant_tx(engine, tenant) as conn:
        return (await conn.execute(text(f"select count(*) from {view}"))).scalar_one()


async def _reset_contracts(db) -> None:
    """What a view migration does to the contract views: empty placeholders again."""
    async with db.owner.begin() as conn:
        for name, (columns, _) in CONTRACTS.items():
            await conn.execute(text(f"CREATE OR REPLACE VIEW {name} AS {placeholder_sql(columns)}"))


async def _readyz(db) -> dict:
    settings = Settings.from_env(
        {"AGENTIC_CP_DATABASE_URL": db.app_dsn, "AGENTIC_CP_REQUIRE_DB_ENFORCEMENT": "false"}
    )
    transport = httpx.ASGITransport(app=create_app(settings, engine=db.app))
    async with httpx.AsyncClient(transport=transport, base_url="http://cp.test") as client:
        reply = await client.get("/readyz")
    assert reply.status_code == 200, reply.text
    return reply.json()


async def test_relink_restores_reset_contract_views(linked_db):
    db, seeded, _ = linked_db
    await _reset_contracts(db)
    assert await _count(db.owner, seeded.a, "dash_calls") == 0
    async with db.owner.begin() as conn:
        relinked = await relink_contracts(conn, db.schema)
    assert relinked == ("usage_source", "gateway_audit_source")
    assert await _count(db.owner, seeded.a, "dash_calls") == len(calls_of(GW_A))
    async with db.owner.begin() as conn:
        assert await relink_contracts(conn, db.schema) == ()  # already pointing there


async def test_relink_without_a_link_changes_nothing(dash_db):
    db, _ = dash_db
    async with db.owner.begin() as conn:
        assert await relink_contracts(conn, db.schema) == ()


async def test_migrate_relinks_reset_contract_views(linked_db, monkeypatch, capsys):
    db, seeded, _ = linked_db
    await _reset_contracts(db)
    monkeypatch.setenv("AGENTIC_CP_DATABASE_URL", db.app_dsn)
    monkeypatch.setenv("AGENTIC_CP_OWNER_DATABASE_URL", db.owner_dsn)
    assert await anyio.to_thread.run_sync(main, ["migrate"]) == 0
    assert "usage_source" in capsys.readouterr().out
    assert await _count(db.owner, seeded.a, "dash_calls") == len(calls_of(GW_A))


async def test_readyz_reports_whether_the_gateway_is_linked(dash_db, make_gateway):
    db, _ = dash_db
    assert (await _readyz(db))["gateway_linked"] is False
    from agentic_cp.dashboards.link import link_usage

    gateway = await make_gateway()
    async with db.owner.begin() as conn:
        await link_usage(conn, db.schema, gateway.db)
    assert (await _readyz(db))["gateway_linked"] is True


async def test_bound_reader_reads_new_views_after_upgrade(readers, monkeypatch):
    db, seeded, made = readers
    await downgrade_async(db.owner_dsn, db.schema, "m3_07_run_health")
    role, password = made.name("old"), secrets.token_hex(12)
    # bound by a release before M3-01: it granted the M2-37 views only
    monkeypatch.setattr("agentic_cp.dashboards.readers.DASH_VIEWS", M2_37_VIEWS)
    await made.bind(seeded.a, role, password)
    await migrate_async(db.owner_dsn, db.schema)
    reader = await made.connect(role, password)
    for view in ROI_VIEWS:
        assert await reader.fetch(f"select * from {view}"), view
