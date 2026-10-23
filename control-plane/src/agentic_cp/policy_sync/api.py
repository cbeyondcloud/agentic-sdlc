"""`/v1/policy`: the caller's tenant's validated policy (M2-02).

Any authenticated user of the tenant reads the active policy, its versions and bundles
(laptops need them); a manual sync needs org `policy.edit.org`. The webhook carries no
bearer token: its path holds the tenant's opaque hook token (ND-14), resolved through the
registered `resolve_webhook`, and the payload is never read."""

from __future__ import annotations

import hashlib
import logging
import re
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Query, Request, Response
from sqlalchemy import func, select

from agentic.orgapi import (
    API_PREFIX,
    WEBHOOK_TOKEN_PREFIX,
    ActivePolicy,
    PolicyVersionInfo,
    SyncOutcome,
)
from agentic_cp.auth.deps import Caller
from agentic_cp.errors import PROBLEM_RESPONSES, ApiProblem, not_found
from agentic_cp.policy_sync.service import Actor, active_policy, sync_once
from agentic_cp.rbac.authz import Org, require
from agentic_cp.tables import policy_versions
from agentic_cp.tenancy.context import tenant_tx
from agentic_cp.tenancy.ratelimit import NoLimit

log = logging.getLogger("agentic_cp.policy_sync")
router = APIRouter(prefix=f"{API_PREFIX}/policy", tags=["policy"], responses=PROBLEM_RESPONSES)
_HOOK = re.compile(re.escape(WEBHOOK_TOKEN_PREFIX) + r"[A-Za-z0-9_-]{20,128}")


@router.get("/active")
async def get_active(request: Request, ctx: Caller) -> ActivePolicy:
    """The tenant's active policy version and where to download its bundle."""
    async with tenant_tx(request.app.state.engine, ctx.tenant_id) as conn:
        active = await active_policy(conn)
    if active is None:
        raise ApiProblem(
            404,
            "policy.none",
            "this tenant has no validated policy yet",
            hint="an org admin sets the policy repo (PUT /v1/tenant/policy-source)",
        )
    return active


@router.get("/versions")
async def list_versions(
    request: Request, ctx: Caller, limit: int = Query(default=20, ge=1, le=200)
) -> list[PolicyVersionInfo]:
    """The tenant's policy versions, newest first, accepted and rejected."""
    async with tenant_tx(request.app.state.engine, ctx.tenant_id) as conn:
        active = await active_policy(conn)
        rows = await conn.execute(
            select(
                policy_versions.c.version,
                policy_versions.c.branch,
                policy_versions.c.status,
                policy_versions.c.problems,
                policy_versions.c.digest,
                policy_versions.c.received_at,
            )
            .order_by(policy_versions.c.received_at.desc(), policy_versions.c.version)
            .limit(limit)
        )
        return [
            PolicyVersionInfo(
                version=row.version,
                branch=row.branch,
                status=row.status,
                problems=list(row.problems),
                digest=row.digest,
                received_at=row.received_at,
                active=active is not None and active.version == row.version,
            )
            for row in rows
        ]


@router.get(
    "/versions/{version}/bundle",
    response_class=Response,
    responses={200: {"content": {"application/gzip": {}}}},
)
async def get_bundle(version: str, request: Request, ctx: Caller) -> Response:
    """An accepted version's bundle (`git archive`, gzipped); `ETag` is its digest."""
    async with tenant_tx(request.app.state.engine, ctx.tenant_id) as conn:
        row = (
            await conn.execute(
                select(policy_versions.c.bundle, policy_versions.c.digest).where(
                    policy_versions.c.version == version,
                    policy_versions.c.status == "accepted",
                )
            )
        ).first()
    if row is None:
        raise not_found("policy version")
    return Response(
        content=row.bundle,
        media_type="application/gzip",
        headers={"ETag": f'"{row.digest}"', "Cache-Control": "private, immutable"},
    )


@router.post("/sync", responses={202: {"model": SyncOutcome}})
async def manual_sync(request: Request, response: Response, ctx: Caller) -> SyncOutcome:
    """Sync the tenant's policy now (org `policy.edit.org`); 202 when a sync is running."""
    state = request.app.state
    async with tenant_tx(state.engine, ctx.tenant_id) as conn:
        await require(ctx, "policy.edit.org", Org(), conn=conn, cache=state.enforcers)
    outcome = await sync_once(
        state.engine,
        state.git,
        ctx.tenant_id,
        state.settings,
        state.clock,
        actor=Actor(ctx.email, ctx.actor_kind),
    )
    if outcome.kind == "busy":
        response.status_code = 202
    return outcome


@router.post("/webhook/{hook_token}", status_code=202)
async def webhook(hook_token: str, request: Request, background: BackgroundTasks) -> dict:
    """A push to the tenant's policy repo: sync that tenant after answering (R15)."""
    if not _HOOK.fullmatch(hook_token):
        raise not_found("webhook")
    state = request.app.state
    digest = hashlib.sha256(hook_token.encode("utf-8")).digest()
    async with state.engine.begin() as conn:  # no tenant yet: the definer function only
        tenant_id = (await conn.execute(select(func.resolve_webhook(digest)))).scalar()
    if tenant_id is None:
        raise not_found("webhook")
    limiter = state.rate_limiter or NoLimit()
    decision = await limiter.check(tenant_id, tenant_id, "webhook")
    if not decision.allowed:
        headers = {"Retry-After": str(decision.retry_after)} if decision.retry_after else None
        raise ApiProblem(
            429, "rate.limited", "too many webhook calls; try again later", headers=headers
        )
    background.add_task(_sync_after_hook, request.app, tenant_id)
    return {"accepted": True}


async def _sync_after_hook(app, tenant_id: UUID) -> None:
    state = app.state
    try:
        await sync_once(state.engine, state.git, tenant_id, state.settings, state.clock)
    except Exception as error:  # after the response: log, never raise into the server
        log.warning("webhook sync failed (%s)", type(error).__name__, exc_info=error)
