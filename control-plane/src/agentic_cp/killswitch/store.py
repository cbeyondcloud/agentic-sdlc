"""Reading kill switches in the transaction's tenant (forced RLS keeps other tenants out)."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from agentic.killswitch import Switch
from agentic.orgapi import KillSwitchInfo
from agentic_cp.tables import kill_switches


def info(row) -> KillSwitchInfo:
    return KillSwitchInfo(
        id=row.id,
        scope_kind=row.scope_kind,
        scope_value=row.scope_value,
        team=row.team,
        mode=row.mode,
        reason=row.reason,
        created_by=row.created_by,
        created_at=row.created_at,
        cleared_by=row.cleared_by,
        cleared_at=row.cleared_at,
        active=row.cleared_at is None,
    )


def as_switch(row) -> Switch:
    return Switch(
        id=str(row.id),
        scope_kind=row.scope_kind,
        scope_value=row.scope_value,
        mode=row.mode,
        reason=row.reason,
        by=row.created_by,
        team=row.team,
    )


async def rows(conn: AsyncConnection, *, include_cleared: bool = False) -> list:
    """The tenant's switches, oldest first (active only, unless `include_cleared`)."""
    query = select(kill_switches).order_by(kill_switches.c.created_at, kill_switches.c.id)
    if not include_cleared:
        query = query.where(kill_switches.c.cleared_at.is_(None))
    return list(await conn.execute(query))


async def active_switches(conn: AsyncConnection) -> list[Switch]:
    return [as_switch(row) for row in await rows(conn)]
