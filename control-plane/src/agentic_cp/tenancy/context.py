"""How the tenant reaches Postgres: per transaction, from a context variable.

A `begin` event on every engine runs `set_config('app.tenant_id', <tenant>, true)` - the
bindable form of `SET LOCAL` - so the setting lives exactly as long as the transaction and
a pooled connection never carries it into the next one. With no tenant in the context the
event sets the empty string, so the RLS predicate is NULL: reads return no rows and writes
are refused, even if something left a session-level value on the connection."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from contextvars import ContextVar
from uuid import UUID

from sqlalchemy import event, func, select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

#: The tenant of the current task (a request, a worker iteration, an operator command).
tenant_ctx: ContextVar[UUID | None] = ContextVar("agentic_cp_tenant", default=None)

SETTING = "app.tenant_id"


class TenancyError(Exception):
    """The transaction's tenant is missing or is not the one the code asked for."""


def install_tenant_guc(engine: AsyncEngine) -> None:
    """Make every transaction on `engine` start in the context's tenant (or in none)."""

    @event.listens_for(engine.sync_engine, "begin")
    def _set_tenant(conn) -> None:
        tenant = tenant_ctx.get()
        value = str(tenant) if tenant is not None else ""
        conn.execute(select(func.set_config(SETTING, value, True)))


async def current_tenant_id(conn: AsyncConnection) -> UUID | None:
    """The tenant the current transaction runs in, as Postgres sees it."""
    value = (await conn.execute(select(func.current_setting(SETTING, True)))).scalar_one()
    return UUID(value) if value else None


async def require_tenant_id(conn: AsyncConnection) -> UUID:
    """The current transaction's tenant; `TenancyError` when there is none."""
    tenant = await current_tenant_id(conn)
    if tenant is None:
        raise TenancyError("no tenant is set for this transaction")
    return tenant


@asynccontextmanager
async def tenant_tx(engine: AsyncEngine, tenant_id: UUID) -> AsyncIterator[AsyncConnection]:
    """A transaction in `tenant_id`, committed on success. The setting is read back at the
    start and before commit; a mismatch (say, a session-level `set_config` inside) raises
    `TenancyError`, rolls back, and the connection is discarded rather than pooled."""
    token = tenant_ctx.set(tenant_id)
    try:
        async with engine.connect() as conn:
            try:
                async with conn.begin():
                    await _expect(conn, tenant_id)
                    yield conn
                    await _expect(conn, tenant_id)
            except TenancyError:
                await conn.invalidate()
                raise
    finally:
        tenant_ctx.reset(token)


async def _expect(conn: AsyncConnection, tenant_id: UUID) -> None:
    found = await current_tenant_id(conn)
    if found != tenant_id:
        raise TenancyError("the transaction's tenant changed underneath it")
