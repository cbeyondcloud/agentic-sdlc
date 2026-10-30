"""Evidence tables are insert-only for the running service (Review Focus 2), and the service
fails closed when the database does not enforce that (ND-5)."""

import uuid
from datetime import UTC, datetime

import asyncpg
import httpx
import pytest
from asyncpg.exceptions import InsufficientPrivilegeError
from sqlalchemy import func, select
from sqlalchemy.exc import DBAPIError

from agentic_cp.app import create_app
from agentic_cp.audit import AuditEvent, append
from agentic_cp.db_roles import INSERT_ONLY, enforcement_status, grant_runtime
from agentic_cp.settings import Settings
from agentic_cp.tables import TENANT_TABLES, metadata
from agentic_cp.tenancy.context import tenant_tx

pytestmark = pytest.mark.anyio

EVIDENCE = [t for t in INSERT_ONLY if t in TENANT_TABLES]


def test_audit_log_is_registered_as_evidence():
    assert "audit_log" in EVIDENCE


@pytest.mark.parametrize("table", EVIDENCE)
@pytest.mark.parametrize("statement", ["UPDATE", "DELETE", "TRUNCATE"])
async def test_runtime_role_cannot_update_delete_or_truncate_evidence(
    two_tenants, table, statement
):
    db, a, _ = two_tenants
    target = metadata.tables[table]
    sql = {
        "UPDATE": target.update().values(tenant_id=target.c.tenant_id),
        "DELETE": target.delete(),
    }
    with pytest.raises(DBAPIError) as caught:
        async with tenant_tx(db.app, a) as conn:
            if statement == "TRUNCATE":
                await conn.exec_driver_sql(f"TRUNCATE {table}")
            else:
                await conn.execute(sql[statement])
    assert isinstance(caught.value.orig.__cause__, InsufficientPrivilegeError)


async def test_runtime_role_can_insert_and_select_audit(two_tenants):
    db, a, _ = two_tenants
    async with tenant_tx(db.app, a) as conn:
        await append(
            conn,
            AuditEvent(event="token.issued", actor="system:operator", actor_kind="system"),
            clock=lambda: datetime.now(UTC),
        )
        table = metadata.tables["audit_log"]
        assert (await conn.execute(select(func.count()).select_from(table))).scalar_one() == 1


async def test_enforcement_status_reports_each_blocker(db):
    role = f"cp_audit_{uuid.uuid4().hex[:8]}"
    async with db.owner.begin() as conn:
        await grant_runtime(conn, role, uuid.uuid4().hex, db.schema)
    try:
        async with db.owner.connect() as conn:
            assert (await enforcement_status(conn, role=role)).enforced
        async with db.owner.begin() as conn:
            await conn.exec_driver_sql(f"GRANT UPDATE ON audit_log TO {role}")
        async with db.owner.connect() as conn:
            status = await enforcement_status(conn, role=role)
        assert status.enforced is False
        assert status.codes == {"audit.unenforced"}
        assert any("may change audit rows directly" in r for r in status.reasons)
    finally:
        admin = await asyncpg.connect(db.admin_dsn)
        try:
            await admin.execute(f"DROP OWNED BY {role}")
            await admin.execute(f"DROP ROLE {role}")
        finally:
            await admin.close()


async def _readyz(dsn: str, engine, require: bool):
    settings = Settings.from_env(
        {
            "AGENTIC_CP_DATABASE_URL": dsn,
            "AGENTIC_CP_REQUIRE_DB_ENFORCEMENT": str(require).lower(),
        }
    )
    transport = httpx.ASGITransport(app=create_app(settings, engine=engine))
    async with httpx.AsyncClient(transport=transport, base_url="http://cp.test") as client:
        return await client.get("/readyz")


async def test_readyz_fails_closed_when_audit_unenforced(db):
    reply = await _readyz(db.owner_dsn, db.owner, require=True)
    assert reply.status_code == 503
    assert reply.json()["code"] == "audit.unenforced"
    relaxed = await _readyz(db.owner_dsn, db.owner, require=False)
    assert relaxed.status_code == 200
    assert relaxed.json() == {"status": "ready", "db_enforced": False, "gateway_linked": False}
