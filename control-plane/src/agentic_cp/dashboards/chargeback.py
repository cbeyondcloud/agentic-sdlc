"""Reading the monthly chargeback (M3-01) from `dash_chargeback`, the view the Executive ROI
dashboard reads too, so the export and the dashboard agree by construction.

The caller's transaction runs as the runtime role in the caller's tenant: `dash_scope`
admits a role that may read the tenant tables, and `db_roles.grant_runtime` grants the
runtime role SELECT on this view only (`db_roles.RUNTIME_VIEWS`)."""

from __future__ import annotations

from sqlalchemy import column, select, table
from sqlalchemy.ext.asyncio import AsyncConnection

from agentic.orgapi.cost import CSV_COLUMNS, Chargeback, ChargebackRow

dash_chargeback = table("dash_chargeback", *(column(name) for name in CSV_COLUMNS))


async def read_chargeback(conn: AsyncConnection, month: str, team: str | None) -> Chargeback:
    """The chargeback of `month` (YYYY-MM), every team or just `team`, teams in order."""
    query = select(dash_chargeback).where(dash_chargeback.c.month == month)
    if team is not None:
        query = query.where(dash_chargeback.c.team == team)
    found = await conn.execute(query.order_by(dash_chargeback.c.team))
    rows = [ChargebackRow(**row._mapping) for row in found]
    return Chargeback.of(month, team, rows)
