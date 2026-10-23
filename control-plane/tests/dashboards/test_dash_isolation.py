"""Tenant-bound Grafana reader roles (M2-37, ruling T1, Review Focus 1): a tenant's reader
sees only that tenant's rows in every dashboard view, whatever it sets `app.tenant_id` to,
and can read no table at all. Every `dash_*` view in the schema is registered, so a new
view is covered by these generated cases."""

import secrets

import anyio
import asyncpg
import pytest
from pg import APP_ROLE, OWNER_ROLE, connect_admin
from sqlalchemy import select, text

from agentic_cp.__main__ import main
from agentic_cp.audit import read_records, verify
from agentic_cp.dashboards.readers import ReaderError
from agentic_cp.dashboards.views import DASH_VIEWS
from agentic_cp.tables import tenants
from agentic_cp.tenancy.context import tenant_tx

pytestmark = pytest.mark.anyio


@pytest.mark.parametrize("view", DASH_VIEWS)
async def test_reader_sees_only_its_tenant(bound, view):
    db, seeded, _, conns = bound
    for label, tenant in (("a", seeded.a), ("b", seeded.b)):
        rows = await conns[label].fetch(f"select tenant_id from {view}")
        assert rows, f"{view} is empty for {label}: the isolation check would prove nothing"
        assert {row[0] for row in rows} == {tenant}, view


async def test_reader_switching_tenant_sees_nothing(bound):
    _, seeded, _, conns = bound
    reader = conns["a"]
    await reader.execute(f"SET app.tenant_id = '{seeded.b}'")
    for view in DASH_VIEWS:
        assert await reader.fetchval(f"select count(*) from {view}") == 0, view
    await reader.execute(f"select set_config('app.tenant_id', '{seeded.b}', false)")
    assert await reader.fetchval("select count(*) from dash_calls") == 0
    await reader.execute("RESET app.tenant_id")  # back to its own tenant
    assert await reader.fetchval("select count(*) from dash_calls") > 0


@pytest.mark.parametrize(
    "relation",
    [
        "runs",
        "run_events",
        "audit_log",
        "tenants",
        "users",
        "usage_source",
        "agentic_gateway.usage_events",
    ],
)
async def test_reader_cannot_read_tables_or_link(bound, relation):
    _, _, _, conns = bound
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        await conns["a"].fetch(f"select * from {relation} limit 1")


async def test_unbound_role_sees_nothing(readers, pg_server):
    """A role given the views by hand, and a default tenant, but never bound: no rows."""
    db, seeded, made = readers
    role, password = made.name("rogue"), secrets.token_hex(12)
    admin = await connect_admin(db.admin_dsn)
    try:
        await admin.execute(f"CREATE ROLE {role} LOGIN PASSWORD '{password}'")
        await admin.execute(f"GRANT USAGE ON SCHEMA {db.schema} TO {role}")
        for view in DASH_VIEWS:
            await admin.execute(f"GRANT SELECT ON {db.schema}.{view} TO {role}")
        await admin.execute(f"ALTER ROLE {role} SET app.tenant_id = '{seeded.a}'")
        await admin.execute(f"ALTER ROLE {role} SET search_path = {db.schema}")
    finally:
        await admin.close()
    rogue = await made.connect(role, password)
    for view in DASH_VIEWS:
        assert await rogue.fetchval(f"select count(*) from {view}") == 0, view


async def test_every_dash_view_is_registered(db):
    async with db.owner.connect() as conn:
        found = await conn.execute(
            text(
                "select table_name, column_name from information_schema.columns "
                "where table_schema = :s and table_name like 'dash\\_%' and ordinal_position = 1"
            ),
            {"s": db.schema},
        )
        first = dict(found.all())
    assert set(first) == set(DASH_VIEWS)
    assert set(first.values()) == {"tenant_id"}


async def test_role_bound_to_another_tenant_is_refused(readers):
    _, seeded, made = readers
    role = made.name("shared")
    await made.bind(seeded.a, role, secrets.token_hex(12))
    with pytest.raises(ReaderError, match="already"):
        await made.bind(seeded.b, role, secrets.token_hex(12))


@pytest.mark.parametrize("role", [APP_ROLE, OWNER_ROLE, "postgres"])
async def test_service_roles_cannot_be_readers(readers, role):
    _, seeded, made = readers
    with pytest.raises(ReaderError):
        await made.bind(seeded.a, role, secrets.token_hex(12))


async def test_new_role_needs_a_password(readers):
    _, seeded, made = readers
    with pytest.raises(ReaderError, match="password"):
        await made.bind(seeded.a, made.name("nopw"), None)


async def test_rebinding_moves_the_grants(readers):
    db, seeded, made = readers
    first, second = made.name("first"), made.name("second")
    password = secrets.token_hex(12)
    await made.bind(seeded.a, first, password)
    await made.bind(seeded.a, second, password)
    with pytest.raises(asyncpg.InsufficientPrivilegeError):  # its search_path is reset too
        await (await made.connect(first, password)).fetch(f"select * from {db.schema}.dash_calls")
    assert (
        await (await made.connect(second, password)).fetchval("select count(*) from dash_calls") > 0
    )
    async with db.owner.connect() as conn:
        bound_role = (
            await conn.execute(select(tenants.c.dashboard_role).where(tenants.c.id == seeded.a))
        ).scalar_one()
    assert bound_role == second


async def test_bind_is_audited(readers):
    db, seeded, made = readers
    role = made.name("audited")
    await made.bind(seeded.a, role, secrets.token_hex(12))
    async with tenant_tx(db.owner, seeded.a) as conn:
        record = (await read_records(conn))[-1]
        assert (record.event, record.target) == ("dashboard.reader_bound", f"role:{role}")
        assert record.after == {"role": role}
        assert await verify(conn) == []


async def test_views_owned_by_a_superuser_are_refused(readers):
    """Views run with their owner's rights; a superuser owner would skip RLS."""
    db, seeded, made = readers
    admin = await connect_admin(db.admin_dsn)
    try:
        await admin.execute(f"ALTER VIEW {db.schema}.dash_scope OWNER TO postgres")
    finally:
        await admin.close()
    with pytest.raises(ReaderError, match="superuser"):
        await made.bind(seeded.a, made.name("late"), secrets.token_hex(12))


async def test_dashboard_role_command(readers, monkeypatch, capsys):
    db, seeded, made = readers
    role, password = made.name("cmd"), secrets.token_hex(12)
    monkeypatch.setenv("AGENTIC_CP_DATABASE_URL", db.app_dsn)
    monkeypatch.setenv("AGENTIC_CP_OWNER_DATABASE_URL", db.owner_dsn)
    monkeypatch.setenv("TEST_READER_PASSWORD", password)
    argv = ["db", "dashboard-role", "--tenant", "tenant-a", "--role", role]
    argv += ["--password-env", "TEST_READER_PASSWORD"]
    assert await anyio.to_thread.run_sync(main, argv) == 0
    out = capsys.readouterr().out
    assert role in out and password not in out
    reader = await made.connect(role, password)
    assert await reader.fetchval("select count(*) from dash_runs") == 3
    bad = ["db", "dashboard-role", "--tenant", "tenant-a", "--role", "Not-An-Ident"]
    assert await anyio.to_thread.run_sync(main, bad) == 3
