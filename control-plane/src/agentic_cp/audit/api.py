"""Reading the caller's tenant's audit chain (`audit.read.org`, or `audit.read.team` for one
team's records), verifying it (`audit.read.org`) and exporting a range of it with its chain
proof (`audit.export`, M2-13)."""

from __future__ import annotations

from dataclasses import asdict
from typing import Literal

from fastapi import APIRouter, Query, Request
from fastapi.responses import StreamingResponse

from agentic.orgapi import API_PREFIX, AuditEntry, AuditVerification
from agentic_cp.audit.chain import AuditEvent, append, format_at, read_records, verify
from agentic_cp.audit.export import MEDIA_TYPES, export_range, parse_bound, stream_export
from agentic_cp.auth.deps import Caller
from agentic_cp.errors import PROBLEM_RESPONSES, ApiProblem
from agentic_cp.rbac.authz import Org, Team, require
from agentic_cp.tenancy.context import tenant_tx

router = APIRouter(prefix=f"{API_PREFIX}/audit", tags=["audit"], responses=PROBLEM_RESPONSES)

_EXPORT_RESPONSES = {
    200: {
        "description": "The export: JSONL (header, entries and links, proof) or CSV, streamed",
        "content": {MEDIA_TYPES["jsonl"]: {}, MEDIA_TYPES["csv"]: {}},
    }
}


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


@router.get("/export", response_class=StreamingResponse, responses=_EXPORT_RESPONSES)
async def export_audit(
    request: Request,
    ctx: Caller,
    fmt: Literal["jsonl", "csv"] = Query(default="jsonl", alias="format"),
    team: str | None = Query(default=None, max_length=63),
    start: str | None = Query(default=None, alias="from", max_length=40),
    end: str | None = Query(default=None, alias="to", max_length=40),
) -> StreamingResponse:
    """The records whose time is in [`from`, `to`) with the chain proof an auditor verifies
    without this database (`agentic.auditproof`). `from`/`to` are ISO dates (a `to` date
    includes that day) or date-times with a zone. With `team`, other teams' records in the
    range are link rows only. The export itself is audited (`audit.exported`)."""
    try:
        start_at, end_at = parse_bound(start, end=False), parse_bound(end, end=True)
    except ValueError as error:
        raise ApiProblem(
            422,
            "audit.invalid_range",
            f"from/to must be an ISO date or a date-time with a zone ({error})",
        ) from None
    if start_at is not None and end_at is not None and start_at >= end_at:
        raise ApiProblem(422, "audit.invalid_range", "`from` must be before `to`")
    state = request.app.state
    async with tenant_tx(state.engine, ctx.tenant_id) as conn:
        scope = Team(team) if team is not None else Org()
        await require(ctx, "audit.export", scope, conn=conn, cache=state.enforcers)
        span = await export_range(conn, start_at, end_at)
        await append(
            conn,
            AuditEvent(
                event="audit.exported",
                actor=ctx.email,
                actor_kind=ctx.actor_kind,
                target="audit",
                team=team,
                after={
                    "format": fmt,
                    "from": start_at,
                    "to": end_at,
                    "first_seq": span.first_seq,
                    "last_seq": span.last_seq,
                },
            ),
            clock=state.clock,
        )
    header = {
        "exported_by": ctx.email,
        "start_at": start_at,
        "end_at": end_at,
        "exported_at": format_at(state.clock()),
    }
    body = stream_export(state.engine, ctx.tenant_id, span, team=team, fmt=fmt, header=header)
    name = f"audit-{ctx.tenant_slug}-{team or 'org'}.{fmt}"
    return StreamingResponse(
        body,
        media_type=MEDIA_TYPES[fmt],
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )
