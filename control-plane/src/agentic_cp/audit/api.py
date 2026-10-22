"""Reading the caller's tenant's audit chain (`audit.read.org`, or `audit.read.team` for one
team's records) and verifying it (`audit.read.org`)."""

from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, Query, Request

from agentic.orgapi import API_PREFIX, AuditEntry, AuditVerification
from agentic_cp.audit.chain import read_records, verify
from agentic_cp.auth.deps import Caller
from agentic_cp.errors import PROBLEM_RESPONSES
from agentic_cp.rbac.authz import Org, Team, require
from agentic_cp.tenancy.context import tenant_tx

router = APIRouter(prefix=f"{API_PREFIX}/audit", tags=["audit"], responses=PROBLEM_RESPONSES)


@router.get("")
async def list_audit(
    request: Request,
    ctx: Caller,
    after_seq: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=1000),
    team: str | None = Query(default=None, max_length=63),
) -> list[AuditEntry]:
    """Records after `after_seq`, oldest first. With `team`, only that team's records."""
    state = request.app.state
    async with tenant_tx(state.engine, ctx.tenant_id) as conn:
        if team is None:
            await require(ctx, "audit.read.org", Org(), conn=conn, cache=state.enforcers)
            records = await read_records(conn, after_seq=after_seq, limit=limit)
        else:
            await require(ctx, "audit.read.team", Team(team), conn=conn, cache=state.enforcers)
            records = await read_records(conn, after_seq=after_seq, team=team, limit=limit)
    return [AuditEntry(**asdict(record)) for record in records]


@router.get("/verify")
async def verify_audit(request: Request, ctx: Caller) -> AuditVerification:
    """Recompute every hash and link of the tenant's chain; [] means intact."""
    state = request.app.state
    async with tenant_tx(state.engine, ctx.tenant_id) as conn:
        await require(ctx, "audit.read.org", Org(), conn=conn, cache=state.enforcers)
        return AuditVerification(problems=await verify(conn))
