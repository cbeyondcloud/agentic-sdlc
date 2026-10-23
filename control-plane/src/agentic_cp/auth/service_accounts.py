"""`/v1/service-accounts` (M2-14, spec §12.3, ruling P6): team-scoped accounts for CI and
nightly jobs that may only trigger runs.

A service account is a `users` row of kind `service` named `<name>@<team>.service.invalid`
(RFC 2606: never deliverable), holding exactly one assignment - `service-account` in its
team - and a token that expires after the `days` asked for (required, at most 365: owner
decision 75). The token is in the create response only. Org admins create accounts in any
team, team leads in their own (`rbac.assign.team`). Nothing can give one more than
`run.trigger`: the role cannot be widened, another role cannot be assigned, and `authorize`
refuses a service principal anything else."""

from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Query, Request
from sqlalchemy import and_, select

from agentic.orgapi import (
    API_PREFIX,
    ServiceAccountInfo,
    ServiceAccountIssued,
    ServiceAccountRequest,
)
from agentic_cp.audit import AuditEvent, append, format_at
from agentic_cp.auth.deps import Caller
from agentic_cp.auth.tokens import issue_token
from agentic_cp.auth.users import create_user, user_by_email
from agentic_cp.errors import PROBLEM_RESPONSES, ApiProblem
from agentic_cp.policy_sync.service import active_teams
from agentic_cp.rbac import store
from agentic_cp.rbac.api import may_assign_in
from agentic_cp.rbac.defaults import SERVICE_ACCOUNT
from agentic_cp.tables import assignments, users
from agentic_cp.tenancy.context import tenant_tx
from agentic_cp.tenancy.tenants import bump_rbac_revision, lock_rbac

router = APIRouter(
    prefix=f"{API_PREFIX}/service-accounts", tags=["service-accounts"], responses=PROBLEM_RESPONSES
)


def service_email(name: str, team: str) -> str:
    return f"{name}@{team}.service.invalid"


@router.post("", status_code=201)
async def create_service_account(
    body: ServiceAccountRequest, request: Request, ctx: Caller
) -> ServiceAccountIssued:
    """A new service account in `team` and its token (shown in this response only)."""
    state = request.app.state
    now = state.clock()
    expires_at = now + timedelta(days=body.days)
    email = service_email(body.name, body.team)
    async with tenant_tx(state.engine, ctx.tenant_id) as conn:
        await lock_rbac(conn)
        await may_assign_in(ctx, conn, state.enforcers, body.team)
        if (teams := await active_teams(conn)) is not None and body.team not in teams:
            raise ApiProblem(
                422,
                "rbac.unknown_team",
                f"team {body.team!r} is not in the tenant's active policy",
            )
        if await user_by_email(conn, email) is not None:
            raise ApiProblem(
                409,
                "service_account.exists",
                f"team {body.team} already has a service account named {body.name}",
            )
        user = await create_user(
            conn, email, kind="service", display_name=f"service account {body.name}"
        )
        await store.create_assignment(
            conn,
            user_id=user.id,
            role=SERVICE_ACCOUNT,
            kind="team",
            value=body.team,
            created_by=ctx.email,
        )
        revision = await bump_rbac_revision(conn)
        await append(
            conn,
            AuditEvent(
                event="service_account.created",
                actor=ctx.email,
                actor_kind=ctx.actor_kind,
                target=f"user:{email}",
                team=body.team,
                after={
                    "user": email,
                    "role": SERVICE_ACCOUNT,
                    "team": body.team,
                    "rbac_revision": revision,
                },
            ),
            clock=state.clock,
        )
        token_id, value = await issue_token(
            conn, user_id=user.id, name=SERVICE_ACCOUNT, expires_at=expires_at, created_by=ctx.email
        )
        await append(
            conn,
            AuditEvent(
                event="token.issued",
                actor=ctx.email,
                actor_kind=ctx.actor_kind,
                target=f"token:{token_id}",
                team=body.team,
                after={"name": SERVICE_ACCOUNT, "user": email, "expires_at": format_at(expires_at)},
            ),
            clock=state.clock,
        )
    return ServiceAccountIssued(
        id=user.id,
        email=email,
        team=body.team,
        token=value,
        token_id=token_id,
        expires_at=expires_at,
    )


@router.get("")
async def list_service_accounts(
    request: Request,
    ctx: Caller,
    team: str | None = Query(default=None, max_length=63),
) -> list[ServiceAccountInfo]:
    """The tenant's service accounts (org `rbac.assign`), or one team's (its team lead)."""
    state = request.app.state
    async with tenant_tx(state.engine, ctx.tenant_id) as conn:
        await may_assign_in(ctx, conn, state.enforcers, team)
        query = (
            select(
                users.c.id,
                users.c.email,
                users.c.status,
                users.c.created_at,
                assignments.c.scope_value,
            )
            .join(
                assignments,
                and_(
                    assignments.c.tenant_id == users.c.tenant_id,
                    assignments.c.user_id == users.c.id,
                    assignments.c.role == SERVICE_ACCOUNT,
                ),
            )
            .where(users.c.kind == "service")
            .order_by(assignments.c.scope_value, users.c.email)
        )
        if team is not None:
            query = query.where(assignments.c.scope_value == team)
        rows = list(await conn.execute(query))
    return [
        ServiceAccountInfo(
            id=row.id,
            email=row.email,
            team=row.scope_value,
            status=row.status,
            created_at=row.created_at,
        )
        for row in rows
    ]
