"""`db link-usage`: the usage contract views read the gateway's tables through postgres_fdw
(M2-37, ruling L1, Review Focus 4)."""

import anyio
import pytest
from dash_seed import CALLS, GATEWAY_AUDIT, GATEWAY_DDL, GW_A, calls_of
from sqlalchemy import select, text

from agentic_cp.__main__ import build_parser, main
from agentic_cp.audit import read_records, verify
from agentic_cp.dashboards.link import LinkError, link_usage
from agentic_cp.tables import tenants
from agentic_cp.tenancy.context import tenant_tx

pytestmark = pytest.mark.anyio


async def _count(engine, tenant, view: str) -> int:
    async with tenant_tx(engine, tenant) as conn:
        return (await conn.execute(text(f"select count(*) from {view}"))).scalar_one()


async def test_link_points_contracts_at_gateway_rows(linked_db):
    db, seeded, _ = linked_db
    assert await _count(db.owner, seeded.a, "usage_source") == len(CALLS)
    assert await _count(db.owner, seeded.a, "gateway_audit_source") == len(GATEWAY_AUDIT)
    assert await _count(db.owner, seeded.a, "dash_calls") == len(calls_of(GW_A))


async def test_relink_is_idempotent(linked_db, make_gateway):
    db, seeded, gateway = linked_db
    async with db.owner.begin() as conn:
        result = await link_usage(conn, db.schema, gateway.db)
    assert result.linked == ("usage_source", "gateway_audit_source")
    assert await _count(db.owner, seeded.a, "dash_calls") == len(calls_of(GW_A))
    other = await make_gateway(calls=CALLS[:2])  # re-pointing at another gateway works too
    async with db.owner.begin() as conn:
        await link_usage(conn, db.schema, other.db)
    assert await _count(db.owner, seeded.a, "dash_calls") == 2


async def test_gateway_without_team_or_tags_is_refused(dash_db, make_gateway):
    db, seeded = dash_db
    old = GATEWAY_DDL[0].replace(" team TEXT NOT NULL DEFAULT '', tags JSONB,", "")
    gateway = await make_gateway(ddl=(old, GATEWAY_DDL[1]), calls=())
    with pytest.raises(LinkError) as raised:
        async with db.owner.begin() as conn:
            await link_usage(conn, db.schema, gateway.db)
    message = str(raised.value)
    assert "team" in message and "tags" in message
    assert "ST-1" in message and "ST-2" in message
    assert gateway.db.password.get_secret_value() not in message
    assert await _count(db.owner, seeded.a, "usage_source") == 0  # rolled back


async def test_missing_audit_table_keeps_its_placeholder(dash_db, make_gateway):
    db, seeded = dash_db
    gateway = await make_gateway(ddl=GATEWAY_DDL[:1], calls=CALLS[:3])
    # fill_gateway also writes audit rows; a gateway without the table cannot hold them
    async with db.owner.begin() as conn:
        result = await link_usage(conn, db.schema, gateway.db)
    assert result.linked == ("usage_source",)
    assert await _count(db.owner, seeded.a, "usage_source") == 3
    assert await _count(db.owner, seeded.a, "gateway_audit_source") == 0


async def test_unreachable_gateway_is_a_link_error_without_the_password(dash_db, make_gateway):
    db, _ = dash_db
    gateway = await make_gateway()
    wrong = gateway.db.with_password("not-the-password")
    with pytest.raises(LinkError) as raised:
        async with db.owner.begin() as conn:
            await link_usage(conn, db.schema, wrong)
    assert "not-the-password" not in str(raised.value)


def test_command_takes_the_password_from_a_variable_only():
    parser = build_parser()
    args = parser.parse_args(["db", "link-usage", "--host", "h", "--dbname", "d", "--user", "u"])
    assert args.password_env == "GATEWAY_DB_PASSWORD"
    with pytest.raises(SystemExit):
        parser.parse_args(["db", "link-usage", "--host", "h", "--dbname", "d", "--password", "x"])


@pytest.fixture
def operator_env(linked_db, monkeypatch):
    db, seeded, gateway = linked_db
    monkeypatch.setenv("AGENTIC_CP_DATABASE_URL", db.app_dsn)
    monkeypatch.setenv("AGENTIC_CP_OWNER_DATABASE_URL", db.owner_dsn)
    return db, seeded, gateway


async def _run(*argv: str) -> int:
    return await anyio.to_thread.run_sync(main, list(argv))


async def test_link_command_is_audited_in_every_tenant(operator_env, monkeypatch, capsys):
    db, seeded, gateway = operator_env
    argv = (
        "db",
        "link-usage",
        "--host",
        gateway.db.host,
        "--port",
        str(gateway.db.port),
        "--dbname",
        gateway.db.dbname,
        "--user",
        gateway.db.user,
        "--password-env",
        "TEST_GATEWAY_PASSWORD",
    )
    monkeypatch.delenv("TEST_GATEWAY_PASSWORD", raising=False)
    assert await _run(*argv) == 3
    assert "TEST_GATEWAY_PASSWORD" in capsys.readouterr().err
    monkeypatch.setenv("TEST_GATEWAY_PASSWORD", gateway.db.password.get_secret_value())
    assert await _run(*argv) == 0
    out = capsys.readouterr().out
    assert "usage_source" in out and gateway.db.password.get_secret_value() not in out
    for tenant in (seeded.a, seeded.b):
        async with tenant_tx(db.owner, tenant) as conn:
            records = await read_records(conn)
            assert records[-1].event == "gateway.usage_linked"
            assert records[-1].after["dbname"] == gateway.db.dbname
            assert "password" not in records[-1].after
            assert await verify(conn) == []
    async with db.owner.begin() as conn:
        slugs = (await conn.execute(select(tenants.c.slug))).scalars().all()
    assert sorted(slugs) == ["tenant-a", "tenant-b"]
