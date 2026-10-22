"""`/v1/me` and the caller's own tokens. Every handler works inside the token's tenant."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Request, Response
from sqlalchemy import select

from agentic.orgapi import (
    API_PREFIX,
    AssignmentRef,
    Me,
    TenantRef,
    TokenInfo,
    TokenIssued,
    TokenRequest,
    UserRef,
)
from agentic_cp.audit import AuditEvent, append, format_at
from agentic_cp.auth.deps import Caller
from agentic_cp.auth.tokens import (
    TokenContext,
    TokenLifetimeError,
    issue_token,
    list_tokens,
    own_token,
    revoke_token,
    token_lifetime,
)
from agentic_cp.auth.users import user_by_id
from agentic_cp.errors import PROBLEM_RESPONSES, ApiProblem, not_found
from agentic_cp.rbac.authz import effective_permissions
from agentic_cp.rbac.store import REPO_MAINTAINER
from agentic_cp.tables import assignments, repo_maintainers
from agentic_cp.tenancy.context import tenant_tx
from agentic_cp.tenancy.tenants import current_tenant

router = APIRouter(prefix=API_PREFIX, tags=["auth"], responses=PROBLEM_RESPONSES)


async def assignments_of(conn, user_id: UUID) -> list[AssignmentRef]:
    """The user's assignments, then the maintainerships CODEOWNERS derives (`derived`)."""
    rows = await conn.execute(
        select(assignments.c.role, assignments.c.scope_kind, assignments.c.scope_value)
        .where(assignments.c.user_id == user_id)
        .order_by(assignments.c.scope_kind, assignments.c.scope_value, assignments.c.role)
    )
    held = [AssignmentRef(role=role, scope=kind, team=value or None) for role, kind, value in rows]
    derived = await conn.execute(
        select(repo_maintainers.c.repo_key)
        .where(repo_maintainers.c.user_id == user_id)
        .order_by(repo_maintainers.c.repo_key)
    )
    held += [
        AssignmentRef(role=REPO_MAINTAINER, scope="repo", repo=key, derived=True)
        for (key,) in derived
    ]
    return held


@router.get("/me")
async def me(request: Request, ctx: Caller) -> Me:
    """Who the token belongs to, in which tenant, and the roles they hold."""
    async with tenant_tx(request.app.state.engine, ctx.tenant_id) as conn:
        tenant = await current_tenant(conn)
        user = await user_by_id(conn, ctx.user_id)
        held = await assignments_of(conn, ctx.user_id)
        permissions = await effective_permissions(ctx, conn=conn, cache=request.app.state.enforcers)
    if user is None:  # deleted between resolution and now
        raise not_found("user")
    return Me(
        user=UserRef(id=user.id, email=user.email, display_name=user.display_name),
        kind=user.kind,
        tenant=TenantRef(slug=tenant.slug, name=tenant.name),
        token_expires_at=ctx.expires_at,
        assignments=held,
        permissions=permissions,
    )


@router.post("/tokens", status_code=201)
async def create_token(body: TokenRequest, request: Request, ctx: Caller) -> TokenIssued:
    """A new token for the caller in the caller's tenant (rotation). The value is in this
    response only. `revoke_current` revokes the token that made the request."""
    state = request.app.state
    try:
        lifetime = token_lifetime(state.settings, ctx.kind, body.days)
    except TokenLifetimeError as error:
        raise ApiProblem(422, "auth.lifetime_too_long", str(error)) from None
    now = state.clock()
    async with tenant_tx(state.engine, ctx.tenant_id) as conn:
        token_id, value = await issue_token(
            conn,
            user_id=ctx.user_id,
            name=body.name,
            expires_at=now + lifetime,
            created_by=ctx.email,
        )
        await append(
            conn,
            AuditEvent(
                event="token.issued",
                actor=ctx.email,
                actor_kind=ctx.actor_kind,
                target=f"token:{token_id}",
                after={
                    "name": body.name,
                    "user": ctx.email,
                    "expires_at": format_at(now + lifetime),
                },
            ),
            clock=state.clock,
        )
        if body.revoke_current and await revoke_token(conn, ctx.token_id, now=now):
            await append(conn, _revoked(ctx, ctx.token_id, "rotated"), clock=state.clock)
    return TokenIssued(id=token_id, name=body.name, token=value, expires_at=now + lifetime)


@router.get("/tokens")
async def get_tokens(request: Request, ctx: Caller) -> list[TokenInfo]:
    """The caller's tokens, without values."""
    async with tenant_tx(request.app.state.engine, ctx.tenant_id) as conn:
        rows = await list_tokens(conn, ctx.user_id)
    return [
        TokenInfo(
            id=row.id,
            name=row.name,
            created_at=row.created_at,
            expires_at=row.expires_at,
            revoked_at=row.revoked_at,
            last_used_at=row.last_used_at,
            current=row.id == ctx.token_id,
        )
        for row in rows
    ]


@router.delete("/tokens/{id}", status_code=204, response_class=Response)
async def delete_token(id: UUID, request: Request, ctx: Caller) -> Response:
    """Revoke one of the caller's tokens. Anyone else's token - in this tenant or another -
    answers like a missing one."""
    state = request.app.state
    async with tenant_tx(state.engine, ctx.tenant_id) as conn:
        if await own_token(conn, id, ctx.user_id) is None:
            raise not_found("token")
        if await revoke_token(conn, id, now=state.clock()):
            await append(conn, _revoked(ctx, id, "revoked by its owner"), clock=state.clock)
    return Response(status_code=204)


def _revoked(ctx: TokenContext, token_id: UUID, reason: str) -> AuditEvent:
    return AuditEvent(
        event="token.revoked",
        actor=ctx.email,
        actor_kind=ctx.actor_kind,
        target=f"token:{token_id}",
        reason=reason,
    )
