"""The RBAC API (M2-03): roles, assignments and permission checks in the caller's tenant.

Every write first takes the tenant's RBAC lock (`lock_rbac`), so concurrent writers run
one after the other and each guard reads what the previous one committed. Every write
bumps `rbac_revision` and appends its audit record in the same transaction, so a refused
change (a guard, a failed insert) leaves neither behind, and every replica's enforcer
cache sees a committed change on its next request."""

from __future__ import annotations

import re
from uuid import UUID

from fastapi import APIRouter, Query, Request, Response

from agentic.orgapi import (
    API_PREFIX,
    Assignment,
    AssignmentRequest,
    CheckRequest,
    CheckResult,
    RoleInfo,
    RoleUpdate,
)
from agentic.orgapi.models import ROLE_NAME
from agentic.orgapi.rbac import PERMISSIONS, unknown_permissions
from agentic_cp.audit import AuditEvent, append
from agentic_cp.auth.deps import Caller
from agentic_cp.auth.tokens import TokenContext
from agentic_cp.auth.users import UserError, create_user, normalise_email, user_by_email
from agentic_cp.errors import PROBLEM_RESPONSES, ApiProblem, not_found
from agentic_cp.policy_sync.service import active_teams
from agentic_cp.rbac import store
from agentic_cp.rbac.authz import Org, Repo, Team, authorize, describe, require
from agentic_cp.rbac.defaults import BUILTIN, ORG_ADMIN
from agentic_cp.tenancy.context import tenant_tx
from agentic_cp.tenancy.tenants import bump_rbac_revision, lock_rbac

router = APIRouter(prefix=f"{API_PREFIX}/rbac", tags=["rbac"], responses=PROBLEM_RESPONSES)


def _role_info(role: store.RoleRow) -> RoleInfo:
    return RoleInfo(
        name=role.name,
        permissions=list(role.permissions),
        builtin=role.builtin,
        team_assignable=role.team_assignable,
    )


def _assignment(row: store.AssignmentRow) -> Assignment:
    return Assignment(
        id=row.id, email=row.email, role=row.role, scope=row.scope_kind, team=row.team
    )


def _role_dict(role: store.RoleRow | None) -> dict | None:
    if role is None:
        return None
    return {
        "name": role.name,
        "permissions": list(role.permissions),
        "team_assignable": role.team_assignable,
    }


async def _audit(conn, request: Request, ctx: TokenContext, event: str, **fields) -> None:
    await append(
        conn,
        AuditEvent(event=event, actor=ctx.email, actor_kind=ctx.actor_kind, **fields),
        clock=request.app.state.clock,
    )


def _checked_role_name(name: str, *, creating: bool = False) -> str:
    """A malformed name names no role: 404 when reading, 422 when creating."""
    if not re.fullmatch(ROLE_NAME, name):
        if creating:
            raise ApiProblem(
                422, "invalid_request", "a role name is lower-case letters, digits and '-'"
            )
        raise not_found("role")
    return name


# ── roles ─────────────────────────────────────────────────────────────────────


@router.get("/roles")
async def list_roles(request: Request, ctx: Caller) -> list[RoleInfo]:
    """The tenant's roles (builtin and custom)."""
    async with tenant_tx(request.app.state.engine, ctx.tenant_id) as conn:
        return [_role_info(role) for role in await store.list_roles(conn)]


@router.get("/roles/{name}")
async def get_role(name: str, request: Request, ctx: Caller) -> RoleInfo:
    async with tenant_tx(request.app.state.engine, ctx.tenant_id) as conn:
        role = await store.get_role(conn, _checked_role_name(name))
    if role is None:
        raise not_found("role")
    return _role_info(role)


@router.put("/roles/{name}", status_code=200, responses={201: {"model": RoleInfo}})
async def put_role(
    name: str, body: RoleUpdate, request: Request, response: Response, ctx: Caller
) -> RoleInfo:
    """Create or replace a role (org `rbac.assign`). Org admins always keep `rbac.assign`."""
    state = request.app.state
    if unknown := unknown_permissions(body.permissions):
        raise ApiProblem(
            422, "rbac.unknown_permission", f"unknown permission(s): {', '.join(unknown)}"
        )
    name = _checked_role_name(name, creating=True)
    if name == ORG_ADMIN and "rbac.assign" not in body.permissions:
        raise ApiProblem(409, "rbac.admin_needs_assign", "the org-admin role must keep rbac.assign")
    async with tenant_tx(state.engine, ctx.tenant_id) as conn:
        await lock_rbac(conn)
        await require(ctx, "rbac.assign", Org(), conn=conn, cache=state.enforcers)
        before = await store.get_role(conn, name)
        if before is not None and before.name in BUILTIN and not BUILTIN[name].assignable:
            team_assignable = before.team_assignable  # derived roles never become assignable
        else:
            team_assignable = body.team_assignable
        after = await store.put_role(conn, name, body.permissions, team_assignable)
        revision = await bump_rbac_revision(conn)
        await _audit(
            conn,
            request,
            ctx,
            "role.updated",
            target=f"role:{name}",
            before=_role_dict(before),
            after={**_role_dict(after), "rbac_revision": revision},
        )
    response.status_code = 201 if before is None else 200
    return _role_info(after)


@router.delete("/roles/{name}", status_code=204, response_class=Response)
async def delete_role(name: str, request: Request, ctx: Caller) -> Response:
    """Delete a custom role that nobody holds (org `rbac.assign`)."""
    state = request.app.state
    async with tenant_tx(state.engine, ctx.tenant_id) as conn:
        await lock_rbac(conn)
        role = await store.get_role(conn, _checked_role_name(name))
        if role is None:
            raise not_found("role")
        await require(ctx, "rbac.assign", Org(), conn=conn, cache=state.enforcers)
        if role.builtin:
            raise ApiProblem(409, "rbac.builtin_role", f"{name} is a builtin role")
        if await store.role_in_use(conn, name):
            raise ApiProblem(
                409, "rbac.role_in_use", f"{name} is still assigned; revoke its assignments first"
            )
        await store.delete_role(conn, name)
        revision = await bump_rbac_revision(conn)
        await _audit(
            conn,
            request,
            ctx,
            "role.deleted",
            target=f"role:{name}",
            before=_role_dict(role),
            after={"rbac_revision": revision},
        )
    return Response(status_code=204)


# ── assignments ───────────────────────────────────────────────────────────────


async def _may_assign_in(ctx, conn, cache, team: str | None) -> bool:
    """True with org `rbac.assign`; False with `rbac.assign.team` for `team` (team-lead
    rules apply); 403 otherwise."""
    if (await authorize(ctx, "rbac.assign", Org(), conn=conn, cache=cache)).allowed:
        return True
    if team is None:
        await require(ctx, "rbac.assign", Org(), conn=conn, cache=cache)
    await require(ctx, "rbac.assign.team", Team(team), conn=conn, cache=cache)
    return False


@router.get("/assignments")
async def list_assignments(
    request: Request,
    ctx: Caller,
    team: str | None = Query(default=None, max_length=63),
    user: str | None = Query(default=None, max_length=254),
) -> list[Assignment]:
    """Assignments in the tenant (org `rbac.assign`), or in one team (`rbac.assign.team`)."""
    state = request.app.state
    async with tenant_tx(state.engine, ctx.tenant_id) as conn:
        await _may_assign_in(ctx, conn, state.enforcers, team)
        try:
            email = normalise_email(user) if user else None
        except UserError:
            return []  # no such person can hold anything
        rows = await store.list_assignments(conn, team=team, email=email)
    return [_assignment(row) for row in rows]


@router.post("/assignments", status_code=201)
async def create_assignment(body: AssignmentRequest, request: Request, ctx: Caller) -> Assignment:
    """Give a person a role at org scope or in a team; the person is created in the
    caller's tenant when new. Team leads assign only team-assignable roles in their team."""
    state = request.app.state
    kind, value = ("team", body.team) if body.team else ("org", "")
    async with tenant_tx(state.engine, ctx.tenant_id) as conn:
        await lock_rbac(conn)
        org_wide = await _may_assign_in(ctx, conn, state.enforcers, body.team)
        role = await store.get_role(conn, body.role)
        if role is None:
            raise ApiProblem(422, "rbac.unknown_role", f"no role {body.role!r} in this tenant")
        if role.name in BUILTIN and not BUILTIN[role.name].assignable:
            raise ApiProblem(
                422, "rbac.not_assignable", f"{role.name} is derived (CODEOWNERS), never assigned"
            )
        if not org_wide and not role.team_assignable:
            raise ApiProblem(
                403,
                "rbac.not_assignable",
                f"a team lead may assign only team-assignable roles, not {role.name}",
            )
        if body.team and (teams := await active_teams(conn)) is not None:
            if body.team not in teams:  # R14, DF-404: the active policy names the teams
                raise ApiProblem(
                    422,
                    "rbac.unknown_team",
                    f"team {body.team!r} is not in the tenant's active policy",
                    hint=f"add policy/teams/{body.team}.yaml to the policy repo first",
                )
        try:
            user = await user_by_email(conn, body.email) or await create_user(conn, body.email)
        except UserError as error:
            raise ApiProblem(422, "invalid_request", f"email: {error}") from None
        if await store.find_assignment(conn, user.id, role.name, kind, value):
            raise ApiProblem(409, "rbac.already_assigned", "that assignment already exists")
        assignment_id = await store.create_assignment(
            conn, user_id=user.id, role=role.name, kind=kind, value=value, created_by=ctx.email
        )
        revision = await bump_rbac_revision(conn)
        await _audit(
            conn,
            request,
            ctx,
            "role.assigned",
            target=f"user:{user.email}",
            team=body.team,
            after={
                "user": user.email,
                "role": role.name,
                "scope": kind,
                "team": body.team,
                "rbac_revision": revision,
            },
        )
    return Assignment(
        id=assignment_id, email=user.email, role=role.name, scope=kind, team=body.team
    )


@router.delete("/assignments/{id}", status_code=204, response_class=Response)
async def delete_assignment(id: UUID, request: Request, ctx: Caller) -> Response:
    """Revoke an assignment. The tenant's last org admin cannot be removed."""
    state = request.app.state
    async with tenant_tx(state.engine, ctx.tenant_id) as conn:
        await lock_rbac(conn)
        row = await store.get_assignment(conn, id)
        if row is None:
            raise not_found("assignment")
        org_wide = await _may_assign_in(ctx, conn, state.enforcers, row.team)
        if not org_wide:
            role = await store.get_role(conn, row.role)
            if role is None or not role.team_assignable or row.scope_kind != "team":
                raise ApiProblem(
                    403, "rbac.not_assignable", f"a team lead may not revoke {row.role}"
                )
        if (
            row.role == ORG_ADMIN
            and row.scope_kind == "org"
            and await store.org_admin_count(conn) <= 1
        ):
            raise ApiProblem(
                409,
                "rbac.last_admin",
                "this is the tenant's last org admin",
                hint="assign another org admin first",
            )
        await store.delete_assignment(conn, id)
        revision = await bump_rbac_revision(conn)
        await _audit(
            conn,
            request,
            ctx,
            "role.revoked",
            target=f"user:{row.email}",
            team=row.team,
            before={"user": row.email, "role": row.role, "scope": row.scope_kind, "team": row.team},
            after={"rbac_revision": revision},
        )
    return Response(status_code=204)


# ── checks ────────────────────────────────────────────────────────────────────


@router.post("/check")
async def check(body: CheckRequest, request: Request, ctx: Caller) -> CheckResult:
    """May the caller - or `user`, which needs org `rbac.assign` - do `permission` for the
    org, a team or a repo? The authoritative answer the CLI relies on."""
    if body.permission not in PERMISSIONS:
        raise ApiProblem(422, "rbac.unknown_permission", f"unknown permission {body.permission}")
    if body.repo:
        resource = Repo(body.repo, team=body.team)
    elif body.team:
        resource = Team(body.team)
    else:
        resource = Org()
    state = request.app.state
    async with tenant_tx(state.engine, ctx.tenant_id) as conn:
        subject = ctx.user_id
        if body.user is not None:
            await require(ctx, "rbac.assign", Org(), conn=conn, cache=state.enforcers)
            try:
                other = await user_by_email(conn, body.user)
            except UserError:
                other = None
            if other is None or other.status != "active":
                raise not_found("user")
            subject = other.id
        decision = await authorize(
            ctx, body.permission, resource, conn=conn, cache=state.enforcers, user_id=subject
        )
    where = describe(resource)
    if decision.allowed:
        reason = f"{body.permission} is granted by {decision.role} at {decision.scope}"
    else:
        reason = f"no role grants {body.permission} for {where}"
    return CheckResult(
        allowed=decision.allowed, reason=reason, role=decision.role, scope=decision.scope
    )
