"""`/v1/repos` and `/v1/users/{email}/handles` (M2-03): repo registration, CODEOWNERS-derived
maintainers and the handles they resolve against, all in the caller's tenant."""

from __future__ import annotations

from fastapi import APIRouter, Request, Response
from sqlalchemy import select, update

from agentic.orgapi import (
    API_PREFIX,
    HandlesInfo,
    HandlesUpdate,
    RepoInfo,
    RepoMaintainers,
    RepoRegistration,
)
from agentic.policy.repos import normalise_remote, repo_allowed
from agentic_cp.audit import AuditEvent, append
from agentic_cp.auth.deps import Caller
from agentic_cp.auth.users import UserError, user_by_email
from agentic_cp.errors import PROBLEM_RESPONSES, ApiProblem, not_found
from agentic_cp.gitops import GitError, scrub
from agentic_cp.policy_sync.service import Actor, active_repo_patterns, active_teams
from agentic_cp.rbac import repos as store
from agentic_cp.rbac.authz import Org, Repo, require
from agentic_cp.tables import users
from agentic_cp.tenancy.api import checked_url
from agentic_cp.tenancy.context import tenant_tx
from agentic_cp.tenancy.tenants import bump_rbac_revision, current_tenant, lock_rbac

router = APIRouter(prefix=API_PREFIX, tags=["repos"], responses=PROBLEM_RESPONSES)


def _info(row: store.RepoRow, emails: list[str]) -> RepoInfo:
    return RepoInfo(
        repo_key=row.repo_key,
        remote=scrub(row.remote),
        team=row.team,
        maintainers=emails,
        unresolved=list(row.unresolved),
        codeowners_path=row.codeowners_path,
        commit=row.codeowners_commit,
        refreshed_at=row.refreshed_at,
    )


def _fetch_failed(error: GitError) -> ApiProblem:
    return ApiProblem(
        502,
        "repo.fetch_failed",
        f"the repo's CODEOWNERS could not be fetched: {error}",
        hint="check the remote and the tenant's git credentials",
    )


@router.post("/repos", status_code=201, responses={200: {"model": RepoInfo}})
async def register_repo(
    body: RepoRegistration, request: Request, response: Response, ctx: Caller
) -> RepoInfo:
    """Register a repo for a team (`agent.edit.repo` there); its remote must match the
    active policy's `allowed_repo_patterns`. Registering again is a no-op."""
    state = request.app.state
    remote = checked_url(body.remote, request, prefix="repo")
    key = normalise_remote(remote)
    actor = Actor(ctx.email, ctx.actor_kind)
    async with tenant_tx(state.engine, ctx.tenant_id) as conn:
        await require(
            ctx, "agent.edit.repo", Repo(key, body.team), conn=conn, cache=state.enforcers
        )
        patterns = await active_repo_patterns(conn)
        if patterns is None:
            raise ApiProblem(
                409,
                "policy.none",
                "this tenant has no validated policy yet, so no repo can be registered",
                hint="an org admin sets the policy repo (PUT /v1/tenant/policy-source)",
            )
        if body.team not in (await active_teams(conn) or []):
            raise ApiProblem(
                422,
                "rbac.unknown_team",
                f"team {body.team!r} is not in the tenant's active policy",
                hint=f"add policy/teams/{body.team}.yaml to the policy repo first",
            )
        existing = await store.get_repo(conn, key)
        if existing is not None:
            if existing.team != body.team:
                raise ApiProblem(
                    409, "repo.team_mismatch", f"{key} is registered for team {existing.team}"
                )
            response.status_code = 200
            return _info(existing, await store.maintainer_emails(conn, key))
        if not repo_allowed(patterns, remote):
            raise ApiProblem(
                422,
                "repo.not_allowed",
                f"{key} does not match the org policy's allowed_repo_patterns",
                hint="ask an org admin to allow it in org.yaml",
            )
        if not await store.insert_repo(conn, key, remote, body.team, ctx.email):
            # another registration won the race: answer as for a re-registration (F8)
            existing = await store.get_repo(conn, key)
            if existing.team != body.team:
                raise ApiProblem(
                    409, "repo.team_mismatch", f"{key} is registered for team {existing.team}"
                )
            response.status_code = 200
            return _info(existing, await store.maintainer_emails(conn, key))
        tenant = await current_tenant(conn)
        try:  # the fetch runs before any audit append takes the tenant's chain lock (F6)
            row = await store.refresh(
                conn, state.git, state.settings, tenant, key, state.clock, actor
            )
        except GitError as error:  # the whole registration rolls back
            raise _fetch_failed(error) from None
        await append(
            conn,
            AuditEvent(
                event="repo.registered",
                actor=ctx.email,
                actor_kind=ctx.actor_kind,
                target=f"repo:{key}",
                team=body.team,
                after={"repo": key, "remote": scrub(remote), "team": body.team},
            ),
            clock=state.clock,
        )
        return _info(row, await store.maintainer_emails(conn, key))


@router.get("/repos/{repo_key:path}/maintainers")
async def get_maintainers(repo_key: str, request: Request, ctx: Caller) -> RepoMaintainers:
    """Who CODEOWNERS makes a maintainer of the repo, and what stays unresolved."""
    async with tenant_tx(request.app.state.engine, ctx.tenant_id) as conn:
        row = await store.get_repo(conn, repo_key)
        if row is None:
            raise not_found("repo")
        emails = await store.maintainer_emails(conn, repo_key)
    return RepoMaintainers(
        users=emails, unresolved=list(row.unresolved), commit=row.codeowners_commit
    )


@router.post("/repos/{repo_key:path}/refresh")
async def refresh_repo(repo_key: str, request: Request, ctx: Caller) -> RepoInfo:
    """Fetch the repo's CODEOWNERS now (`agent.edit.repo` in the repo's team)."""
    state = request.app.state
    async with tenant_tx(state.engine, ctx.tenant_id) as conn:
        row = await store.get_repo(conn, repo_key)
        if row is None:
            raise not_found("repo")
        resource = Repo(repo_key, row.team)
        await require(ctx, "agent.edit.repo", resource, conn=conn, cache=state.enforcers)
        tenant = await current_tenant(conn)
        actor = Actor(ctx.email, ctx.actor_kind)
        try:
            row = await store.refresh(
                conn, state.git, state.settings, tenant, repo_key, state.clock, actor
            )
        except GitError as error:
            raise _fetch_failed(error) from None
        return _info(row, await store.maintainer_emails(conn, repo_key))


@router.put("/users/{email}/handles")
async def put_handles(
    email: str, body: HandlesUpdate, request: Request, ctx: Caller
) -> HandlesInfo:
    """Set a person's host-qualified handles (org `rbac.assign`; ND-6: never self-declared);
    every repo's maintainers are re-resolved against them."""
    state = request.app.state
    handles = sorted({handle.lower() for handle in body.handles})
    async with tenant_tx(state.engine, ctx.tenant_id) as conn:
        await lock_rbac(conn)
        await require(ctx, "rbac.assign", Org(), conn=conn, cache=state.enforcers)
        try:
            user = await user_by_email(conn, email)
        except UserError:
            user = None
        if user is None:
            raise not_found("user")
        before = (
            await conn.execute(select(users.c.handles).where(users.c.id == user.id))
        ).scalar_one()
        await conn.execute(update(users).where(users.c.id == user.id).values(handles=handles))
        revision = await bump_rbac_revision(conn)
        await append(
            conn,
            AuditEvent(
                event="user.handles_changed",
                actor=ctx.email,
                actor_kind=ctx.actor_kind,
                target=f"user:{user.email}",
                before={"handles": sorted(before)},
                after={"handles": handles, "rbac_revision": revision},
            ),
            clock=state.clock,
        )
        await store.resync_all(conn, state.clock, Actor(ctx.email, ctx.actor_kind))
    return HandlesInfo(email=user.email, handles=handles)
