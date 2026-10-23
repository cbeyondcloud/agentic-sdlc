"""Proxy-key routes (M2-17, ruling P12): a person rotates their own gateway key; an org
admin revokes a person's keys. A new key is in the rotate response only (`no-store`)."""

from __future__ import annotations

from fastapi import APIRouter, Request, Response

from agentic.orgapi import API_PREFIX, ProxyKeyIssued, ProxyKeysRevoked
from agentic_cp.audit import AuditEvent, append
from agentic_cp.auth.deps import Caller
from agentic_cp.auth.users import UserError, user_by_email
from agentic_cp.errors import PROBLEM_RESPONSES, ApiProblem, not_found
from agentic_cp.keys.gateway import GatewayUnavailable
from agentic_cp.keys.store import (
    active_keys,
    forget_issued,
    mark_revoked,
    record_key,
    revoke_user_keys,
)
from agentic_cp.rbac.authz import Org, require
from agentic_cp.tenancy.context import tenant_tx

router = APIRouter(prefix=API_PREFIX, tags=["proxy-keys"], responses=PROBLEM_RESPONSES)

NO_STORE = "no-store"


def unavailable(error: GatewayUnavailable) -> ApiProblem:
    return ApiProblem(
        503,
        "gateway.unavailable",
        f"the gateway's key API is unavailable: {error}",
        hint="nothing was changed; try again later, or tell your platform team",
    )


@router.post("/proxy-keys/rotate")
async def rotate_own_key(request: Request, response: Response, ctx: Caller) -> ProxyKeyIssued:
    """Replace the caller's newest gateway key: the old one stops working at once (within
    the gateway's key refresh), the new one is in this response only. A key whose
    revocation is pending is never rotated back to life; a new key that cannot be recorded
    is revoked again, so the gateway never holds a key the control plane does not know."""
    state = request.app.state
    issued = None
    try:
        async with tenant_tx(state.engine, ctx.tenant_id) as conn:
            held = [
                k for k in await active_keys(conn, ctx.user_id) if k.revoke_requested_at is None
            ]
            if not held:
                raise ApiProblem(
                    404,
                    "proxy_key.none",
                    "you hold no gateway key issued through the control plane",
                    hint="redeem your invite (`agentic login --invite <link>`) or ask your org "
                    "admin to invite you again",
                )
            old = held[0]
            try:
                issued = await state.gateway_keys.rotate(old.key_id)
            except GatewayUnavailable as error:
                raise unavailable(error) from None
            await mark_revoked(conn, old.key_id, state.clock())
            await record_key(
                conn,
                key_id=issued.key_id,
                user_id=ctx.user_id,
                team=old.team,
                created_by=ctx.email,
            )
            await append(
                conn,
                AuditEvent(
                    event="proxy_key.rotated",
                    actor=ctx.email,
                    actor_kind=ctx.actor_kind,
                    target=f"proxy_key:{issued.key_id}",
                    team=old.team,
                    before={"key_id": old.key_id},
                    after={"key_id": issued.key_id, "user": ctx.email},
                ),
                clock=state.clock,
            )
    except BaseException:
        if issued is not None:
            await forget_issued(state.gateway_keys, issued)
        raise
    response.headers["Cache-Control"] = NO_STORE
    return ProxyKeyIssued(
        key=issued.key, key_id=issued.key_id, gateway_url=state.settings.gateway_url
    )


@router.delete("/users/{email}/proxy-keys")
async def revoke_keys(email: str, request: Request, ctx: Caller) -> ProxyKeysRevoked:
    """Revoke every gateway key of a person (org `rbac.assign`). A key the gateway cannot
    revoke now is `pending`; the worker finishes it."""
    state = request.app.state
    async with tenant_tx(state.engine, ctx.tenant_id) as conn:
        await require(ctx, "rbac.assign", Org(), conn=conn, cache=state.enforcers)
        try:
            user = await user_by_email(conn, email)
        except UserError:
            user = None
        if user is None:
            raise not_found("user")
        revoked, pending = await revoke_user_keys(
            conn,
            state.gateway_keys,
            user,
            actor=ctx.email,
            actor_kind=ctx.actor_kind,
            clock=state.clock,
            reason="revoked by an admin",
        )
    return ProxyKeysRevoked(email=user.email, revoked=revoked, pending=pending)
