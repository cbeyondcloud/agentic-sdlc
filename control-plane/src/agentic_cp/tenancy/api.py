"""`/v1/tenant`: the caller's tenant and its policy source (M2-02). There is no route that
names a tenant: every handler works on the tenant of the caller's token."""

from __future__ import annotations

import hashlib
import secrets

from fastapi import APIRouter, Request
from sqlalchemy import update

from agentic.orgapi import (
    API_PREFIX,
    WEBHOOK_TOKEN_PREFIX,
    PolicySource,
    PolicySourceChanged,
    TenantInfo,
    WebhookIssued,
)
from agentic_cp.audit import AuditEvent, append
from agentic_cp.auth.deps import Caller
from agentic_cp.errors import PROBLEM_RESPONSES, ApiProblem
from agentic_cp.gitops import RepoUrlError, check_repo_url, scrub
from agentic_cp.policy_sync.service import Actor, sync_once
from agentic_cp.rbac.authz import Org, require
from agentic_cp.tables import tenants
from agentic_cp.tenancy.context import tenant_tx
from agentic_cp.tenancy.tenants import TenantRow, current_tenant

router = APIRouter(prefix=f"{API_PREFIX}/tenant", tags=["tenant"], responses=PROBLEM_RESPONSES)


def tenant_info(tenant: TenantRow) -> TenantInfo:
    return TenantInfo(
        slug=tenant.slug,
        name=tenant.name,
        policy_repo=scrub(tenant.policy_repo_url) if tenant.policy_repo_url else None,
        policy_branch=tenant.policy_branch,
        webhook_configured=tenant.webhook_token_sha256 is not None,
    )


def checked_url(url: str, request: Request) -> str:
    """The URL when a tenant may use it; a 422 problem that never repeats it otherwise."""
    try:
        return check_repo_url(url, allow_local=request.app.state.settings.allow_local_repos)
    except RepoUrlError as error:
        code = "policy.credentials_in_url" if error.credentials else "repo.bad_url"
        raise ApiProblem(422, code, str(error)) from None


@router.get("")
async def get_tenant(request: Request, ctx: Caller) -> TenantInfo:
    """The caller's tenant: its name and policy source (never a credential)."""
    async with tenant_tx(request.app.state.engine, ctx.tenant_id) as conn:
        return tenant_info(await current_tenant(conn))


@router.put("/policy-source")
async def put_policy_source(
    body: PolicySource, request: Request, ctx: Caller
) -> PolicySourceChanged:
    """Point the tenant at its policy repo and branch (org `policy.edit.org`), then sync."""
    state = request.app.state
    async with tenant_tx(state.engine, ctx.tenant_id) as conn:
        await require(ctx, "policy.edit.org", Org(), conn=conn, cache=state.enforcers)
        repo = checked_url(body.repo, request)
        before = await current_tenant(conn)
        await conn.execute(
            update(tenants)
            .where(tenants.c.id == ctx.tenant_id)
            .values(policy_repo_url=repo, policy_branch=body.branch)
        )
        await append(
            conn,
            AuditEvent(
                event="tenant.policy_source_changed",
                actor=ctx.email,
                actor_kind=ctx.actor_kind,
                target=f"tenant:{ctx.tenant_slug}",
                before={"repo": before.policy_repo_url, "branch": before.policy_branch},
                after={"repo": repo, "branch": body.branch},
            ),
            clock=state.clock,
        )
    outcome = await sync_once(
        state.engine,
        state.git,
        ctx.tenant_id,
        state.settings,
        state.clock,
        actor=Actor(ctx.email, ctx.actor_kind),
    )
    async with tenant_tx(state.engine, ctx.tenant_id) as conn:
        info = tenant_info(await current_tenant(conn))
    return PolicySourceChanged(**info.model_dump(), sync=outcome)


@router.post("/webhook-token", status_code=201)
async def rotate_webhook_token(request: Request, ctx: Caller) -> WebhookIssued:
    """A new policy-repo webhook URL for the tenant (org `policy.edit.org`); the previous
    one stops working. The token is in this response only (ND-14)."""
    state = request.app.state
    value = WEBHOOK_TOKEN_PREFIX + secrets.token_urlsafe(32)
    async with tenant_tx(state.engine, ctx.tenant_id) as conn:
        await require(ctx, "policy.edit.org", Org(), conn=conn, cache=state.enforcers)
        before = await current_tenant(conn)
        await conn.execute(
            update(tenants)
            .where(tenants.c.id == ctx.tenant_id)
            .values(webhook_token_sha256=hashlib.sha256(value.encode("utf-8")).digest())
        )
        await append(
            conn,
            AuditEvent(
                event="tenant.webhook_rotated",
                actor=ctx.email,
                actor_kind=ctx.actor_kind,
                target=f"tenant:{ctx.tenant_slug}",
                before={"configured": before.webhook_token_sha256 is not None},
                after={"configured": True},
            ),
            clock=state.clock,
        )
    return WebhookIssued(path=f"{API_PREFIX}/policy/webhook/{value}")
