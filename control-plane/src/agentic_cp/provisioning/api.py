"""Provisioning routes (M2-18): re-sending an invite, redeeming one, and offboarding.

`POST /v1/invites/redeem` is the one route without a bearer token: the tenant comes from
the invite, found through the registered definer function `resolve_invite`. The invite is
used up by one conditional UPDATE inside that tenant's transaction, so two redemptions at
once give one token; a gateway that cannot issue the key rolls the whole redemption back
and the link stays usable. Unknown, malformed and revoked links (and those of a suspended
tenant or a disabled person) all answer 404 alike; a used link 409, an expired one 410."""

from __future__ import annotations

from fastapi import APIRouter, Request, Response
from sqlalchemy import select, update

from agentic.orgapi import (
    API_PREFIX,
    InviteInfo,
    InviteRedeem,
    InviteRedeemed,
    TenantRef,
)
from agentic_cp.audit import AuditEvent, append, format_at
from agentic_cp.auth.deps import Caller
from agentic_cp.auth.tokens import TokenContext, issue_token, token_lifetime
from agentic_cp.auth.users import UserError, UserRow, user_by_email, user_by_id
from agentic_cp.errors import PROBLEM_RESPONSES, ApiProblem, not_found
from agentic_cp.keys.api import NO_STORE, unavailable
from agentic_cp.keys.gateway import GatewayUnavailable
from agentic_cp.keys.store import forget_issued, issue_key
from agentic_cp.provisioning.invites import create_invite, resolve_invite, well_formed
from agentic_cp.rbac.authz import Org, Team, authorize, effective_permissions
from agentic_cp.tables import assignments, invites
from agentic_cp.tenancy.context import tenant_tx
from agentic_cp.tenancy.tenants import current_tenant

router = APIRouter(prefix=API_PREFIX, tags=["provisioning"], responses=PROBLEM_RESPONSES)

ASK_AGAIN = "ask your team lead or org admin to send you a new invite"


def _invalid() -> ApiProblem:
    return ApiProblem(404, "invite.invalid", "the invite link is not valid", hint=ASK_AGAIN)


def _used() -> ApiProblem:
    return ApiProblem(409, "invite.used", "this invite link was already used", hint=ASK_AGAIN)


def _expired() -> ApiProblem:
    return ApiProblem(410, "invite.expired", "this invite link has expired", hint=ASK_AGAIN)


async def team_assignments(conn, user_id) -> list[tuple[str, str]]:
    """(role, team) of each team assignment of the person, oldest first."""
    rows = await conn.execute(
        select(assignments.c.role, assignments.c.scope_value)
        .where(assignments.c.user_id == user_id, assignments.c.scope_kind == "team")
        .order_by(assignments.c.created_at, assignments.c.scope_value)
    )
    return [(role, team) for role, team in rows]


def _forbidden() -> ApiProblem:
    return ApiProblem(
        403,
        "rbac.forbidden",
        "you may manage only people of a team you lead (rbac.assign.team), or anyone with "
        "rbac.assign for the org",
        hint="ask an org admin",
    )


async def may_assign_somewhere(ctx: TokenContext, conn, cache) -> None:
    """403 unless the caller holds `rbac.assign` (org) or `rbac.assign.team` in some team,
    checked before a person is looked up (no probing for who exists)."""
    held = await effective_permissions(ctx, conn=conn, cache=cache)
    if "rbac.assign" not in held.org and not any(
        "rbac.assign.team" in perms for perms in held.teams.values()
    ):
        raise _forbidden()


async def may_manage(ctx: TokenContext, conn, cache, user: UserRow) -> None:
    """Org `rbac.assign`, or `rbac.assign.team` in a team the person is assigned in."""
    if (await authorize(ctx, "rbac.assign", Org(), conn=conn, cache=cache)).allowed:
        return
    for _, team in await team_assignments(conn, user.id):
        decision = await authorize(ctx, "rbac.assign.team", Team(team), conn=conn, cache=cache)
        if decision.allowed:
            return
    raise _forbidden()


async def _person(conn, email: str) -> UserRow | None:
    try:
        return await user_by_email(conn, email)
    except UserError:
        return None


@router.post("/users/{email}/invite", status_code=201)
async def reinvite(email: str, request: Request, ctx: Caller) -> InviteInfo:
    """Send a person a new invite (their earlier unused ones stop working)."""
    state = request.app.state
    async with tenant_tx(state.engine, ctx.tenant_id) as conn:
        await may_assign_somewhere(ctx, conn, state.enforcers)
        user = await _person(conn, email)
        if user is None:
            raise not_found("user")
        await may_manage(ctx, conn, state.enforcers, user)
        if user.kind != "human":
            raise ApiProblem(422, "invite.service_account", "service accounts are not invited")
        if user.status != "active":
            raise ApiProblem(409, "user.disabled", f"{user.email} is offboarded")
        if not state.settings.public_url:
            raise ApiProblem(
                409,
                "invite.unconfigured",
                "this control plane has no public URL, so an invite has nothing to link to",
                hint="the platform team sets AGENTIC_CP_PUBLIC_URL",
            )
        teams = await team_assignments(conn, user.id)
        role, team = teams[0] if teams else ("member", None)
        invite = await create_invite(
            conn,
            user,
            settings=state.settings,
            clock=state.clock,
            actor=ctx.email,
            actor_kind=ctx.actor_kind,
            role=role,
            team=team,
        )
    assert invite is not None  # a person, and a public URL
    return invite


@router.post("/invites/redeem")
async def redeem(body: InviteRedeem, request: Request, response: Response) -> InviteRedeemed:
    """Use an invite once: a control-plane token for the invite's tenant and, when this
    deployment issues them, a gateway key bound to the tenant, the person and their team.
    Both are in this response only."""
    state = request.app.state
    now = state.clock()
    if not well_formed(body.token):
        raise _invalid()
    async with state.engine.begin() as conn:  # no tenant yet: the definer function only
        found = await resolve_invite(conn, body.token)
    if (
        found is None
        or found.tenant_status != "active"
        or found.user_status != "active"
        or found.user_kind != "human"
        or found.revoked_at is not None
    ):
        raise _invalid()
    if found.used_at is not None:
        raise _used()
    if found.expires_at <= now:
        raise _expired()
    gateway = state.gateway_keys
    issued = None
    try:
        async with tenant_tx(state.engine, found.tenant_id) as conn:
            taken = await conn.execute(
                update(invites)
                .where(
                    invites.c.id == found.invite_id,
                    invites.c.used_at.is_(None),
                    invites.c.revoked_at.is_(None),
                    invites.c.expires_at > now,
                )
                .values(used_at=now)
                .returning(invites.c.id)
            )
            if taken.first() is None:  # another redemption won the race
                raise _used()
            user = await user_by_id(conn, found.user_id)
            tenant = await current_tenant(conn)
            expires_at = now + token_lifetime(state.settings, "human", None)
            token_id, value = await issue_token(
                conn, user_id=user.id, name="invite", expires_at=expires_at, created_by=user.email
            )
            for event, target, after in (
                ("invite.redeemed", f"invite:{found.invite_id}", {"user": user.email}),
                (
                    "token.issued",
                    f"token:{token_id}",
                    {"name": "invite", "user": user.email, "expires_at": format_at(expires_at)},
                ),
            ):
                await append(
                    conn,
                    AuditEvent(
                        event=event,
                        actor=user.email,
                        actor_kind="human",
                        target=target,
                        after=after,
                    ),
                    clock=state.clock,
                )
            notice = None
            if gateway.configured and tenant.gateway_tenant:
                teams = await team_assignments(conn, user.id)
                try:
                    issued = await issue_key(
                        conn,
                        gateway,
                        user=user,
                        team=teams[0][1] if teams else None,
                        actor=user.email,
                        actor_kind="human",
                        clock=state.clock,
                    )
                except GatewayUnavailable as error:
                    raise unavailable(error) from None
            else:
                notice = (
                    "this control plane does not issue gateway keys: ask your org admin for "
                    "your gateway key, then run `agentic login`"
                )
    except BaseException:
        if issued is not None:  # the redemption rolled back after the key was made
            await forget_issued(gateway, issued)
        raise
    response.headers["Cache-Control"] = NO_STORE
    return InviteRedeemed(
        tenant=TenantRef(slug=tenant.slug, name=tenant.name),
        email=user.email,
        control_plane_url=state.settings.public_url,
        control_plane_token=value,
        token_expires_at=expires_at,
        gateway_url=state.settings.gateway_url,
        gateway_key=issued.key if issued else None,
        gateway_key_id=issued.key_id if issued else None,
        notice=notice,
    )
