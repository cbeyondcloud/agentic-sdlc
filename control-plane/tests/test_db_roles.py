"""The runtime role and the database's own guarantees (Review Focus 6): the role owns
nothing and cannot bypass RLS; credential lookups go only through registered, pinned
definer functions; `enforcement_status` names every gap and `/readyz` fails closed."""

import logging
import uuid

import asyncpg
import httpx
import pytest
from isolation import PROBE
from pg import APP_ROLE
from sqlalchemy import func, select, text, update
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError

from agentic_cp.app import create_app, serve_preflight
from agentic_cp.db_roles import DEFINER_FUNCTIONS, enforcement_status, grant_runtime
from agentic_cp.settings import Settings
from agentic_cp.tables import TENANT_TABLES, tenants
from agentic_cp.tenancy.context import tenant_tx
from agentic_cp.tenancy.tenants import bump_rbac_revision, current_tenant

pytestmark = pytest.mark.anyio


async def _admin(db) -> asyncpg.Connection:
    return await asyncpg.connect(db.admin_dsn)


async def test_runtime_role_is_not_owner_and_cannot_bypass_rls(db):
    admin = await _admin(db)
    try:
        role = await admin.fetchrow(
            "select oid, rolsuper, rolbypassrls, rolinherit, rolcreaterole "
            "from pg_roles where rolname = $1",
            APP_ROLE,
        )
        assert not role["rolsuper"]
        assert not role["rolbypassrls"]
        assert not role["rolinherit"]
        assert not role["rolcreaterole"]
        assert (
            await admin.fetchval(
                "select count(*) from pg_auth_members where member = $1", role["oid"]
            )
            == 0
        )
        owned = await admin.fetchval(
            "select count(*) from pg_class where relowner = $1", role["oid"]
        ) + await admin.fetchval("select count(*) from pg_proc where proowner = $1", role["oid"])
        assert owned == 0
    finally:
        await admin.close()


async def test_enforcement_status_is_enforced_on_a_fresh_database(db):
    async with db.app.connect() as conn:
        status = await enforcement_status(conn, schema=db.schema)
    assert status.enforced, status.reasons
    assert status.codes == set()


async def test_runtime_sees_only_its_own_tenant_row(two_tenants):
    db, a, b = two_tenants
    async with tenant_tx(db.app, a) as conn:
        rows = (await conn.execute(select(tenants.c.id))).all()
        assert [row[0] for row in rows] == [a]
        assert (await current_tenant(conn)).id == a
        assert await bump_rbac_revision(conn) == 1
    with pytest.raises(DBAPIError) as caught:
        async with tenant_tx(db.app, a) as conn:
            await conn.execute(update(tenants).values(name="renamed").where(tenants.c.id == a))
    assert "permission denied" in str(caught.value)
    async with tenant_tx(db.owner, b) as conn:
        assert (await current_tenant(conn)).rbac_revision == 0


async def test_definer_functions_are_registered_and_pinned(db):
    admin = await _admin(db)
    try:
        rows = await admin.fetch(
            "select p.proname, p.proconfig, p.proacl from pg_proc p "
            "join pg_namespace n on n.oid = p.pronamespace "
            "where n.nspname = $1 and p.prosecdef",
            db.schema,
        )
    finally:
        await admin.close()
    assert {row["proname"] for row in rows} == set(DEFINER_FUNCTIONS)
    for row in rows:
        assert f"search_path=pg_catalog, {db.schema}" in (row["proconfig"] or [])
        acl = [str(item) for item in row["proacl"] or []]
        assert acl, "a NULL acl means PUBLIC may execute"
        assert not any(item.startswith("=") for item in acl), acl  # '=X/owner' is PUBLIC
        assert any(item.startswith(f"{APP_ROLE}=X") for item in acl)


async def test_active_tenant_ids_returns_ids_only(two_tenants):
    db, a, b = two_tenants
    async with tenant_tx(db.app, a) as conn:
        ids = {row[0] for row in await conn.execute(text("select * from active_tenant_ids()"))}
        assert ids == {a, b}
        assert (await conn.execute(select(tenants.c.id).where(tenants.c.id == b))).all() == []
    async with db.owner.begin() as conn:
        await conn.execute(update(tenants).values(status="suspended").where(tenants.c.id == b))
    async with db.app.begin() as conn:
        ids = {row[0] for row in await conn.execute(text("select * from active_tenant_ids()"))}
        assert ids == {a}


async def _gap_role(db) -> str:
    """A throw-away runtime role (roles are cluster-wide: never break the shared one)."""
    role = f"cp_gap_{uuid.uuid4().hex[:8]}"
    async with db.owner.begin() as conn:
        await grant_runtime(conn, role, uuid.uuid4().hex, db.schema)
    return role


async def _drop_role(db, role: str) -> None:
    admin = await _admin(db)
    try:
        await admin.execute(f"DROP OWNED BY {role}")
        await admin.execute(f"DROP ROLE {role}")
    finally:
        await admin.close()


async def test_enforcement_status_reports_rls_gaps(probe):
    db, _, _ = probe
    tables = (*TENANT_TABLES, PROBE)
    role = await _gap_role(db)
    try:
        async with db.owner.connect() as conn:
            assert (await enforcement_status(conn, role=role, tenant_tables=tables)).enforced
        gaps = {
            "no force": f"ALTER TABLE agentic.{PROBE} NO FORCE ROW LEVEL SECURITY",
            "bypass": f"ALTER ROLE {role} BYPASSRLS",
            "definer": (
                "CREATE FUNCTION agentic.peek() RETURNS SETOF uuid LANGUAGE sql "
                "SECURITY DEFINER AS 'select id from agentic.tenants'"
            ),
        }
        undo = {
            "no force": f"ALTER TABLE agentic.{PROBE} FORCE ROW LEVEL SECURITY",
            "bypass": f"ALTER ROLE {role} NOBYPASSRLS",
            "definer": "DROP FUNCTION agentic.peek()",
        }
        expected = {"no force": PROBE, "bypass": "BYPASSRLS", "definer": "peek"}
        for gap, statement in gaps.items():
            admin = await _admin(db)
            try:
                await admin.execute(statement)
            finally:
                await admin.close()
            async with db.owner.connect() as conn:
                status = await enforcement_status(conn, role=role, tenant_tables=tables)
            assert status.enforced is False, gap
            assert "tenancy.unenforced" in status.codes
            assert any(expected[gap] in reason for reason in status.reasons), status.reasons
            admin = await _admin(db)
            try:
                await admin.execute(undo[gap])
            finally:
                await admin.close()
    finally:
        await _drop_role(db, role)


#: Grants and role attributes the catalogue checks must catch: (statement with `{role}`,
#: expected code, a fragment of the reason). Column grants are invisible to
#: `has_table_privilege`, so each is listed on its own.
PRIVILEGE_GAPS = {
    "audit column update": (
        "GRANT UPDATE (hash) ON agentic.audit_log TO {role}",
        "audit.unenforced",
        "may change audit rows",
    ),
    "tenants column update": (
        "GRANT UPDATE (status) ON agentic.tenants TO {role}",
        "tenancy.unenforced",
        "may change tenants",
    ),
    "tenants column insert": (
        "GRANT INSERT (slug, name) ON agentic.tenants TO {role}",
        "tenancy.unenforced",
        "may change tenants",
    ),
    "createrole": ("ALTER ROLE {role} CREATEROLE", "tenancy.unenforced", "CREATEROLE"),
    "createdb": ("ALTER ROLE {role} CREATEDB", "tenancy.unenforced", "CREATEDB"),
    "replication": ("ALTER ROLE {role} REPLICATION", "tenancy.unenforced", "REPLICATION"),
    "schema create": (
        "GRANT CREATE ON SCHEMA agentic TO {role}",
        "tenancy.unenforced",
        "create objects in schema",
    ),
}


@pytest.mark.parametrize("gap", sorted(PRIVILEGE_GAPS))
async def test_enforcement_status_reports_column_grants_and_role_attributes(db, gap):
    statement, code, fragment = PRIVILEGE_GAPS[gap]
    role = await _gap_role(db)
    try:
        async with db.owner.connect() as conn:
            assert (await enforcement_status(conn, role=role)).enforced
        admin = await _admin(db)
        try:
            await admin.execute(statement.format(role=role))
        finally:
            await admin.close()
        async with db.owner.connect() as conn:
            status = await enforcement_status(conn, role=role)
        assert status.enforced is False, gap
        assert code in status.codes
        assert any(fragment in reason for reason in status.reasons), status.reasons
    finally:
        await _drop_role(db, role)


async def test_enforcement_status_reports_an_unregistered_table(db):
    async with db.owner.begin() as conn:
        await conn.exec_driver_sql("CREATE TABLE stray (id int primary key)")
    async with db.app.connect() as conn:
        status = await enforcement_status(conn, schema=db.schema)
    assert not status.enforced
    assert any("stray" in reason for reason in status.reasons)


def _app(db, require: bool):
    settings = Settings.from_env(
        {
            "AGENTIC_CP_DATABASE_URL": db.app_dsn,
            "AGENTIC_CP_REQUIRE_DB_ENFORCEMENT": "true" if require else "false",
        }
    )
    return create_app(settings, engine=db.app)


async def _get(app, path):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://cp.test") as client:
        return await client.get(path)


async def test_readyz_is_ready_on_an_enforced_database(db):
    reply = await _get(_app(db, require=True), "/readyz")
    assert reply.status_code == 200
    assert reply.json() == {"status": "ready", "db_enforced": True}


async def test_readyz_fails_closed_when_tenancy_unenforced(db):
    async with db.owner.begin() as conn:
        await conn.exec_driver_sql("CREATE TABLE stray (id int primary key)")
    reply = await _get(_app(db, require=True), "/readyz")
    assert reply.status_code == 503
    assert reply.json()["code"] == "tenancy.unenforced"
    assert "stray" not in reply.text  # details go to the server log, not to callers
    relaxed = await _get(_app(db, require=False), "/readyz")
    assert relaxed.status_code == 200
    assert relaxed.json() == {"status": "ready", "db_enforced": False}


async def test_serve_refuses_to_start_on_an_unenforced_database(db):
    settings = Settings.from_env({"AGENTIC_CP_DATABASE_URL": db.app_dsn})
    assert await serve_preflight(settings, db.app) is None
    async with db.owner.begin() as conn:
        await conn.exec_driver_sql("CREATE TABLE stray (id int primary key)")
    refusal = await serve_preflight(settings, db.app)
    assert refusal and refusal.startswith("tenancy.unenforced")


async def test_readyz_reports_pending_migrations(db):
    async with db.owner.begin() as conn:
        await conn.exec_driver_sql("UPDATE alembic_version SET version_num = '0000'")
    reply = await _get(_app(db, require=True), "/readyz")
    assert reply.status_code == 503
    assert reply.json()["code"] == "db.migrations_pending"


async def test_readyz_reports_an_unreachable_database():
    settings = Settings.from_env(
        {"AGENTIC_CP_DATABASE_URL": "postgresql+asyncpg://nobody:x@127.0.0.1:9/none"}
    )
    reply = await _get(create_app(settings), "/readyz")
    assert reply.status_code == 503
    assert reply.json()["code"] == "db.unavailable"


async def test_grant_runtime_is_idempotent_and_never_logs_password(db, caplog):
    role = f"cp_idem_{uuid.uuid4().hex[:8]}"
    password = "pw-" + uuid.uuid4().hex
    caplog.set_level(logging.DEBUG)
    try:
        for _ in range(2):
            async with db.owner.begin() as conn:
                await grant_runtime(conn, role, password, db.schema)
        async with db.owner.connect() as conn:
            assert (await enforcement_status(conn, role=role)).enforced
        assert caplog.records, "grant_runtime logs what it does"
        assert all(password not in record.getMessage() for record in caplog.records)
        assert "SCRAM-SHA-256$" not in caplog.text
        admin = await _admin(db)
        try:
            stored = await admin.fetchval(
                "select rolpassword from pg_authid where rolname = $1", role
            )
        finally:
            await admin.close()
        assert stored.startswith("SCRAM-SHA-256$")
        dsn = make_url(db.app_dsn).set(username=role, password=password)
        login = await asyncpg.connect(dsn.render_as_string(hide_password=False))
        await login.close()  # the SCRAM verifier we computed accepts the password
    finally:
        await _drop_role(db, role)


async def test_runtime_role_cannot_read_the_owner_table_count_without_tenant(db):
    async with db.app.begin() as conn:
        assert (await conn.execute(select(func.count()).select_from(tenants))).scalar_one() == 0
