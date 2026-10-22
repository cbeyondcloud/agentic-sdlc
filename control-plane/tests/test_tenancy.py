"""The tenant reaches Postgres per transaction, from a context variable, and nowhere else
(Review Focus 6): no tenant means no rows and no writes; the setting never outlives its
transaction; concurrent tasks never see each other's tenant."""

import asyncio

import pytest
from asyncpg.exceptions import InsufficientPrivilegeError
from isolation import probe_row, probe_table
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError

from agentic_cp.db import engine_for
from agentic_cp.tenancy.context import TenancyError, tenant_ctx, tenant_tx

pytestmark = pytest.mark.anyio


async def _count(conn) -> int:
    return (await conn.execute(select(func.count()).select_from(probe_table))).scalar_one()


async def test_missing_tenant_context_returns_no_rows(probe):
    db, a, b = probe
    async with db.app.begin() as conn:
        assert await _count(conn) == 0
    async with tenant_tx(db.app, a) as conn:
        assert await _count(conn) == 2


async def test_missing_tenant_context_refuses_inserts(probe):
    db, a, _ = probe
    with pytest.raises(DBAPIError) as caught:
        async with db.app.begin() as conn:
            await conn.execute(probe_table.insert().values(await probe_row(conn, a)))
    assert isinstance(caught.value.orig.__cause__, InsufficientPrivilegeError)
    assert "row-level security" in str(caught.value)


async def test_tenant_context_is_transaction_local(probe):
    db, a, _ = probe
    engine = engine_for(db.app_dsn, db.schema, pool_size=1)
    try:
        async with tenant_tx(engine, a) as conn:
            assert await _count(conn) == 2
        async with engine.begin() as conn:
            setting = await conn.execute(select(func.current_setting("app.tenant_id", True)))
            assert setting.scalar_one() in ("", None)
            assert await _count(conn) == 0
    finally:
        await engine.dispose()


async def test_tenant_context_does_not_leak_between_concurrent_tasks(probe):
    db, a, b = probe

    async def worker(tenant):
        seen = set()
        for _ in range(50):
            async with tenant_tx(db.app, tenant) as conn:
                rows = await conn.execute(select(probe_table.c.tenant_id))
                seen |= {row[0] for row in rows}
                await asyncio.sleep(0)
        return seen

    seen_a, seen_b = await asyncio.gather(worker(a), worker(b))
    assert seen_a == {a}
    assert seen_b == {b}
    assert tenant_ctx.get() is None


async def test_tenant_tx_refuses_a_mismatched_setting(probe):
    db, a, b = probe
    engine = engine_for(db.app_dsn, db.schema, pool_size=1)
    try:
        with pytest.raises(TenancyError):
            async with tenant_tx(engine, a) as conn:
                await conn.execute(select(func.set_config("app.tenant_id", str(b), False)))
        # the connection that carried a session-level setting never serves again
        async with engine.begin() as conn:
            assert await _count(conn) == 0
    finally:
        await engine.dispose()


async def test_tenant_tx_resets_the_context_variable(probe):
    db, a, _ = probe
    async with tenant_tx(db.app, a):
        assert tenant_ctx.get() == a
    assert tenant_ctx.get() is None


async def test_owner_role_is_bound_by_forced_rls_too(probe):
    db, a, _ = probe
    async with db.owner.begin() as conn:
        assert await _count(conn) == 0
    async with tenant_tx(db.owner, a) as conn:
        rows = (await conn.execute(text(f"select tenant_id from {probe_table.name}"))).all()
        assert {row[0] for row in rows} == {a}
