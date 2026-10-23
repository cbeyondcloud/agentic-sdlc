"""`/v1/killswitch`: set, clear and list kill and pause switches, and the state a run's CLI
polls (M2-06, M2-10; spec §8.6).

Who (ruling R3): a switch without `team` is tenant-wide and needs `killswitch.global`; a
switch limited to a team needs `killswitch.team` in that team (an org admin holds it at org
scope, a team lead in their own team only). Clearing needs what setting it needed. Every
change appends `killswitch.set` / `killswitch.cleared` to the tenant's audit chain in the
same transaction. Another tenant's switch is unknown here (404, DR-5)."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Query, Request
from sqlalchemy import select, update

from agentic.killswitch import RunScope, run_level, scope_problem
from agentic.orgapi import API_PREFIX, KillState, KillSwitchInfo, KillSwitchRequest
from agentic_cp.audit import AuditEvent, append
from agentic_cp.auth.deps import Caller
from agentic_cp.auth.tokens import TokenContext
from agentic_cp.errors import PROBLEM_RESPONSES, ApiProblem, not_found
from agentic_cp.killswitch import store
from agentic_cp.policy_sync.service import active_teams
from agentic_cp.rbac.authz import Org, Team, effective_permissions, require
from agentic_cp.tables import kill_switches
from agentic_cp.tenancy.context import require_tenant_id, tenant_tx

router = APIRouter(
    prefix=f"{API_PREFIX}/killswitch", tags=["killswitch"], responses=PROBLEM_RESPONSES
)


def _invalid(detail: str) -> ApiProblem:
    return ApiProblem(422, "killswitch.invalid_scope", detail)


def _checked(body: KillSwitchRequest) -> tuple[str, str, str | None]:
    """(kind, value, team) of a valid request; a team switch is limited to its team."""
    kind, value, team = body.scope_kind, body.scope_value.strip(), body.team
    if problem := scope_problem(kind, value):
        raise _invalid(problem)
    if kind == "team":
        if team is not None and team != value:
            raise _invalid(f"a team switch for {value} cannot be limited to team {team}")
        team = value
    if kind == "tenant" and team is not None:
        raise _invalid("a tenant-wide switch is not limited to a team: use scope_kind team")
    return kind, value, team


async def _may_change(ctx: TokenContext, team: str | None, conn, request: Request) -> None:
    cache = request.app.state.enforcers
    if team is None:
        await require(ctx, "killswitch.global", Org(), conn=conn, cache=cache)
    else:
        await require(ctx, "killswitch.team", Team(team), conn=conn, cache=cache)


def _scope(row_or_values) -> dict:
    get = row_or_values.get if isinstance(row_or_values, dict) else row_or_values._mapping.get
    return {key: get(key) for key in ("scope_kind", "scope_value", "team", "mode")}


@router.post("", status_code=201)
async def set_switch(body: KillSwitchRequest, request: Request, ctx: Caller) -> KillSwitchInfo:
    """Stop (`kill`) or hold (`pause`) the runs the scope names, from their next check."""
    state = request.app.state
    kind, value, team = _checked(body)
    async with tenant_tx(state.engine, ctx.tenant_id) as conn:
        await _may_change(ctx, team, conn, request)
        if team is not None and (teams := await active_teams(conn)) is not None:
            if team not in teams:  # a typo would match no run: refuse it (as R14 does)
                raise ApiProblem(
                    422,
                    "killswitch.unknown_team",
                    f"team {team!r} is not in the tenant's active policy",
                )
        values = {
            "tenant_id": await require_tenant_id(conn),
            "scope_kind": kind,
            "scope_value": value,
            "team": team,
            "mode": body.mode,
            "reason": body.reason.strip(),
            "created_by": ctx.email,
            "created_at": state.clock(),
        }
        inserted = kill_switches.insert().values(values).returning(kill_switches)
        row = (await conn.execute(inserted)).one()
        await append(
            conn,
            AuditEvent(
                event="killswitch.set",
                actor=ctx.email,
                actor_kind=ctx.actor_kind,
                target=f"killswitch:{row.id}",
                team=team,
                reason=row.reason,
                after=_scope(row),
            ),
            clock=state.clock,
        )
    return store.info(row)


@router.delete("/{id}")
async def clear_switch(
    id: UUID,
    request: Request,
    ctx: Caller,
    reason: str = Query(default="cleared", min_length=1, max_length=500),
) -> KillSwitchInfo:
    """Clear a switch: held runs continue on `agentic resume`, killed runs stay killed."""
    state = request.app.state
    async with tenant_tx(state.engine, ctx.tenant_id) as conn:
        row = (
            await conn.execute(
                select(kill_switches).where(kill_switches.c.id == id).with_for_update()
            )
        ).first()
        if row is None:
            raise not_found("switch")
        await _may_change(ctx, row.team, conn, request)
        if row.cleared_at is not None:
            raise ApiProblem(
                409,
                "killswitch.cleared",
                f"the switch was already cleared by {row.cleared_by}",
            )
        row = (
            await conn.execute(
                update(kill_switches)
                .where(kill_switches.c.id == id)
                .values(cleared_by=ctx.email, cleared_at=state.clock())
                .returning(kill_switches)
            )
        ).one()
        await append(
            conn,
            AuditEvent(
                event="killswitch.cleared",
                actor=ctx.email,
                actor_kind=ctx.actor_kind,
                target=f"killswitch:{row.id}",
                team=row.team,
                reason=reason.strip(),
                before=_scope(row),
            ),
            clock=state.clock,
        )
    return store.info(row)


@router.get("")
async def list_switches(
    request: Request, ctx: Caller, include_cleared: bool = Query(default=False)
) -> list[KillSwitchInfo]:
    """The switches the caller may clear: every switch for `killswitch.global`, else the
    ones limited to a team where the caller holds `killswitch.team`."""
    state = request.app.state
    async with tenant_tx(state.engine, ctx.tenant_id) as conn:
        perms = await effective_permissions(ctx, conn=conn, cache=state.enforcers)
        found = await store.rows(conn, include_cleared=include_cleared)
    if "killswitch.global" in perms.org or "killswitch.team" in perms.org:
        return [store.info(row) for row in found]
    teams = {team for team, held in perms.teams.items() if "killswitch.team" in held}
    if not teams:
        raise ApiProblem(
            403,
            "rbac.forbidden",
            "you do not hold killswitch.team or killswitch.global",
            hint="ask an org admin or your team lead for a role that grants it",
        )
    return [store.info(row) for row in found if row.team in teams]


@router.get("/state")
async def kill_state(
    request: Request,
    ctx: Caller,
    team: str | None = Query(default=None, max_length=64),
    repo: str | None = Query(default=None, max_length=300),
    pipeline: str | None = Query(default=None, max_length=128),
) -> KillState:
    """The active switches that may apply to a run of the caller's with this team, repo and
    pipeline: agent, model and backend switches are always included (the CLI decides per
    step). Any authenticated user of the tenant may ask; user switches are the caller's."""
    state = request.app.state
    scope = RunScope(team=team, user=ctx.email.lower(), repo=repo, pipeline=pipeline)
    async with tenant_tx(state.engine, ctx.tenant_id) as conn:
        found = await store.rows(conn)
    applying = [store.info(row) for row in found if run_level(store.as_switch(row), scope)]
    state.metrics.observe_kill_state(bool(applying))
    return KillState(
        switches=applying,
        checked_at=state.clock(),
        ttl_seconds=state.settings.kill_ttl_seconds,
    )
