"""`GET /v1/cost/chargeback`: the caller's tenant's monthly chargeback per team (M3-01), as
JSON (`Chargeback`) or CSV (`agentic.orgapi.cost.chargeback_csv`, the file the CLI writes).

The whole org needs `dashboard.view.org` (the exec-viewer and auditor roles); one team needs
`dashboard.view.team` in that team, or the org permission. `month` defaults to the previous
calendar month (UTC, by the server's clock). Every export is on the tenant's audit chain
(`cost.chargeback_exported`)."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Query, Request
from fastapi.responses import Response

from agentic.orgapi import API_PREFIX
from agentic.orgapi.cost import MONTH_PATTERN, Chargeback, chargeback_csv, previous_month
from agentic.orgapi.models import TEAM_SLUG
from agentic_cp.audit.chain import AuditEvent, append
from agentic_cp.auth.deps import Caller
from agentic_cp.dashboards.chargeback import read_chargeback
from agentic_cp.errors import PROBLEM_RESPONSES
from agentic_cp.rbac.authz import Org, Team, authorize, require
from agentic_cp.tenancy.context import tenant_tx

router = APIRouter(prefix=f"{API_PREFIX}/cost", tags=["cost"], responses=PROBLEM_RESPONSES)

CSV_MEDIA_TYPE = "text/csv; charset=utf-8"
_RESPONSES = {
    200: {
        "description": "The chargeback: JSON, or CSV with `format=csv`",
        "content": {"text/csv": {}},
    }
}


@router.get("/chargeback", response_model=Chargeback, responses=_RESPONSES)
async def chargeback(
    request: Request,
    ctx: Caller,
    month: str | None = Query(default=None, pattern=MONTH_PATTERN),
    # a team slug only: it is written into the download name and the audit record
    team: str | None = Query(default=None, pattern=TEAM_SLUG),
    fmt: Literal["json", "csv"] = Query(default="json", alias="format"),
):
    """One row per team for `month` (YYYY-MM): users, runs, step executions, calls, tokens,
    and cost split into attributed and unattributed. Unmeasured values are null."""
    state = request.app.state
    month = month or previous_month(state.clock().date())
    async with tenant_tx(state.engine, ctx.tenant_id) as conn:
        org = await authorize(ctx, "dashboard.view.org", Org(), conn=conn, cache=state.enforcers)
        if team is None or not org.allowed:
            scope = Org() if team is None else Team(team)
            permission = "dashboard.view.org" if team is None else "dashboard.view.team"
            await require(ctx, permission, scope, conn=conn, cache=state.enforcers)
        report = await read_chargeback(conn, month, team)
        await append(
            conn,
            AuditEvent(
                event="cost.chargeback_exported",
                actor=ctx.email,
                actor_kind=ctx.actor_kind,
                target="chargeback",
                team=team,
                after={
                    "month": month,
                    "format": fmt,
                    "teams": len(report.rows),
                    "cost_usd": None
                    if report.totals.cost_usd is None
                    else str(report.totals.cost_usd),
                },
            ),
            clock=state.clock,
        )
    if fmt == "json":
        return report
    name = f"chargeback-{ctx.tenant_slug}-{team or 'org'}-{month}.csv"
    return Response(
        chargeback_csv(report),
        media_type=CSV_MEDIA_TYPE,
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )
