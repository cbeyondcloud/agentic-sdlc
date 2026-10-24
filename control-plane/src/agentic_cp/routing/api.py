"""`/v1/routing` (M2-09): the tenant's routing applies - which gateway version applied which
policy version, or why not - and a re-queue of the active version.

Reading needs org `policy.edit.org` or `audit.read.org`; queueing needs `policy.edit.org`.
The worker applies a queued version on its next pass (`routing.apply.routing_job`)."""

from __future__ import annotations

from fastapi import APIRouter, Query, Request, Response
from sqlalchemy import select

from agentic.orgapi import API_PREFIX, RoutingApplyInfo
from agentic_cp.auth.deps import Caller
from agentic_cp.errors import PROBLEM_RESPONSES, ApiProblem
from agentic_cp.policy_sync.service import active_policy
from agentic_cp.rbac.authz import Org, authorize, require
from agentic_cp.routing.outbox import gateway_configured, queue_apply
from agentic_cp.tables import routing_applies
from agentic_cp.tenancy.context import tenant_tx

router = APIRouter(prefix=f"{API_PREFIX}/routing", tags=["routing"], responses=PROBLEM_RESPONSES)


def _info(row) -> RoutingApplyInfo:
    return RoutingApplyInfo(
        id=row.id,
        policy_version=row.policy_version,
        status=row.status,
        attempts=row.attempts,
        gateway_version=row.gateway_version,
        previous_gateway_version=row.previous_gateway_version,
        plan_sha256=row.plan_sha256,
        problems=list(row.problems),
        warnings=list(row.warnings),
        last_error=row.last_error,
        requested_by=row.requested_by,
        created_at=row.created_at,
        next_attempt_at=row.next_attempt_at,
        applied_at=row.applied_at,
    )


@router.get("")
async def list_applies(
    request: Request, ctx: Caller, limit: int = Query(default=20, ge=1, le=200)
) -> list[RoutingApplyInfo]:
    """The tenant's routing applies, newest first."""
    state = request.app.state
    async with tenant_tx(state.engine, ctx.tenant_id) as conn:
        allowed = await authorize(ctx, "audit.read.org", Org(), conn=conn, cache=state.enforcers)
        if not allowed.allowed:
            await require(ctx, "policy.edit.org", Org(), conn=conn, cache=state.enforcers)
        rows = await conn.execute(
            select(routing_applies)
            .order_by(routing_applies.c.created_at.desc(), routing_applies.c.id)
            .limit(limit)
        )
        return [_info(row) for row in rows]


@router.post("/apply", status_code=202)
async def queue_active(request: Request, response: Response, ctx: Caller) -> RoutingApplyInfo:
    """Queue the active policy version for the gateway again (org `policy.edit.org`)."""
    state = request.app.state
    async with tenant_tx(state.engine, ctx.tenant_id) as conn:
        await require(ctx, "policy.edit.org", Org(), conn=conn, cache=state.enforcers)
        active = await active_policy(conn)
        if active is None:
            raise ApiProblem(
                409,
                "routing.no_policy",
                "this tenant has no validated policy to apply",
                hint="set the policy repo first (PUT /v1/tenant/policy-source)",
            )
        if not gateway_configured(state.settings):
            raise ApiProblem(
                409,
                "routing.no_gateway",
                "this deployment names no gateway admin API to apply routing through",
                hint="the operator sets AGENTIC_CP_GATEWAY_ADMIN_URL and _KEY",
            )
        apply_id = await queue_apply(
            conn, active.version, actor=ctx.email, actor_kind=ctx.actor_kind, clock=state.clock
        )
        row = (
            await conn.execute(select(routing_applies).where(routing_applies.c.id == apply_id))
        ).one()
    return _info(row)
