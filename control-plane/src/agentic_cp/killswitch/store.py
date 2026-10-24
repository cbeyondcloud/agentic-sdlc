"""Reading, setting and clearing kill switches in the transaction's tenant (forced RLS keeps
other tenants out); every change is appended to the tenant's audit chain with it."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from agentic.killswitch import Switch
from agentic.orgapi import KillSwitchInfo
from agentic_cp.audit import AuditEvent, append
from agentic_cp.tables import kill_switches
from agentic_cp.tenancy.context import require_tenant_id

#: `created_by` of the pause switches auto-trip sets (M2-11); every other switch is a
#: person's (or a service account's).
AUTO_TRIP_ACTOR = "system:auto-trip"


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


def scope_of(row_or_values) -> dict:
    """What a switch covers, as its audit record shows it."""
    get = row_or_values.get if isinstance(row_or_values, dict) else row_or_values._mapping.get
    return {key: get(key) for key in ("scope_kind", "scope_value", "team", "mode")}


async def insert_switch(
    conn: AsyncConnection,
    *,
    scope_kind: str,
    scope_value: str,
    team: str | None,
    mode: str,
    reason: str,
    actor: str,
    actor_kind: str,
    clock: Callable[[], datetime],
):
    """Set a switch in the transaction's tenant and append `killswitch.set` (the API and
    auto-trip both set switches through here); the new row."""
    values = {
        "tenant_id": await require_tenant_id(conn),
        "scope_kind": scope_kind,
        "scope_value": scope_value,
        "team": team,
        "mode": mode,
        "reason": reason,
        "created_by": actor,
        "created_at": clock(),
    }
    row = (await conn.execute(kill_switches.insert().values(values).returning(kill_switches))).one()
    await append(
        conn,
        AuditEvent(
            event="killswitch.set",
            actor=actor,
            actor_kind=actor_kind,
            target=f"killswitch:{row.id}",
            team=team,
            reason=row.reason,
            after=scope_of(row),
        ),
        clock=clock,
    )
    return row


async def clear_switch(
    conn: AsyncConnection,
    switch_id: UUID,
    *,
    reason: str,
    actor: str,
    actor_kind: str,
    clock: Callable[[], datetime],
):
    """Clear an active switch and append `killswitch.cleared`; the cleared row. The caller
    holds the row (`FOR UPDATE`) and has checked that it is still active."""
    row = (
        await conn.execute(
            update(kill_switches)
            .where(kill_switches.c.id == switch_id)
            .values(cleared_by=actor, cleared_at=clock())
            .returning(kill_switches)
        )
    ).one()
    await append(
        conn,
        AuditEvent(
            event="killswitch.cleared",
            actor=actor,
            actor_kind=actor_kind,
            target=f"killswitch:{row.id}",
            team=row.team,
            reason=reason,
            before=scope_of(row),
        ),
        clock=clock,
    )
    return row
