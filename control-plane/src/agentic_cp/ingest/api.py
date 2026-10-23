"""`/v1/runs`: run event ingest and the runs read back (M2-04), in the caller's tenant only.

A run id names a different run in every tenant (the key includes the tenant), so another
tenant's run is simply unknown here."""

from __future__ import annotations

from fastapi import APIRouter, Query, Request
from sqlalchemy import select

from agentic.orgapi import API_PREFIX, ChainHead, IngestRequest, IngestResult, RunSummary
from agentic_cp.auth.deps import Caller
from agentic_cp.errors import PROBLEM_RESPONSES, ApiProblem, not_found
from agentic_cp.ingest.store import AUDITED, Unaudited, audit_rejection, ingest_batch
from agentic_cp.rbac.authz import Team, authorize
from agentic_cp.tables import run_events, runs, users
from agentic_cp.tenancy.context import tenant_tx
from sdlc_commons.ledger import run_id_refusal

router = APIRouter(prefix=f"{API_PREFIX}/runs", tags=["runs"], responses=PROBLEM_RESPONSES)


def _checked_run_id(run_id: str) -> str:
    if why := run_id_refusal(run_id):
        raise ApiProblem(422, "invalid_request", f"run_id: {why}")
    return run_id


@router.post("/{run_id}/events")
async def ingest(run_id: str, body: IngestRequest, request: Request, ctx: Caller) -> IngestResult:
    """Append a batch of the run's chained events: verified entry by entry, stored all or
    nothing; replayed entries are no-ops (`duplicates`)."""
    state = request.app.state
    _checked_run_id(run_id)
    if len(body.entries) > state.settings.max_ingest_entries:
        raise ApiProblem(
            413,
            "ingest.too_large",
            f"a batch holds at most {state.settings.max_ingest_entries} events",
            hint="send the run's events in smaller batches",
        )
    try:
        async with tenant_tx(state.engine, ctx.tenant_id) as conn:
            return await ingest_batch(
                conn, ctx, run_id, body.entries, cache=state.enforcers, clock=state.clock
            )
    except ApiProblem as problem:
        if problem.code in AUDITED and not isinstance(problem, Unaudited):
            async with tenant_tx(state.engine, ctx.tenant_id) as conn:
                await audit_rejection(conn, ctx, run_id, problem, state.clock)
        raise


async def _readable_run(request: Request, ctx, run_id: str, conn):
    row = (
        await conn.execute(
            select(runs, users.c.email)
            .join(
                users, (users.c.tenant_id == runs.c.tenant_id) & (users.c.id == runs.c.started_by)
            )
            .where(runs.c.run_id == run_id)
        )
    ).first()
    if row is None:
        raise not_found("run")
    if row.started_by != ctx.user_id:
        cache = request.app.state.enforcers
        for permission in ("dashboard.view.team", "audit.read.team"):
            decision = await authorize(ctx, permission, Team(row.team), conn=conn, cache=cache)
            if decision.allowed:
                break
        else:
            raise ApiProblem(
                403,
                "rbac.forbidden",
                f"you do not hold dashboard.view.team or audit.read.team for team {row.team}",
            )
    return row


@router.get("/{run_id}")
async def get_run(run_id: str, request: Request, ctx: Caller) -> RunSummary:
    """The run: who started it, its team, policy version and chain head. Readable by its
    starter and by holders of `dashboard.view.team` or `audit.read.team` in its team."""
    async with tenant_tx(request.app.state.engine, ctx.tenant_id) as conn:
        row = await _readable_run(request, ctx, _checked_run_id(run_id), conn)
    return RunSummary(
        run_id=row.run_id,
        team=row.team,
        started_by=row.email,
        policy_version=row.policy_version,
        policy_known=row.policy_known,
        status=row.status,
        head=ChainHead(seq=row.head_seq, hash=row.head_hash),
        opened_at=row.opened_at,
        last_event_at=row.last_event_at,
    )


@router.get("/{run_id}/events")
async def get_events(
    run_id: str,
    request: Request,
    ctx: Caller,
    after_seq: int = Query(default=0, ge=0),
    limit: int = Query(default=500, ge=1, le=1000),
) -> list[dict]:
    """The run's events exactly as received, after `after_seq` (for `agentic audit verify`)."""
    async with tenant_tx(request.app.state.engine, ctx.tenant_id) as conn:
        await _readable_run(request, ctx, _checked_run_id(run_id), conn)
        rows = await conn.execute(
            select(run_events.c.entry)
            .where(run_events.c.run_id == run_id, run_events.c.seq > after_seq)
            .order_by(run_events.c.seq)
            .limit(limit)
        )
        return [entry for (entry,) in rows]
