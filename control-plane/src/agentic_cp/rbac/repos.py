"""Registered repos and their derived maintainers, per tenant (M2-03, ND-6).

A refresh fetches the repo's CODEOWNERS over git (the tenant's own git config, ND-15),
stores the owners of the agentic paths on the repo row, and resyncs `repo_maintainers`:
the tenant's active users whose host-qualified handle or email is one of those owners.
A resync is an RBAC write: it takes the tenant's RBAC lock, and a change bumps
`rbac_revision` and is audited (`maintainers.synced`) in the same transaction, so every
replica's enforcer sees it on its next request."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from uuid import UUID

from sqlalchemy import or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from agentic.policy.repos import repo_host
from agentic_cp.audit import AuditEvent, append
from agentic_cp.gitops import GitError, GitRunner, RepoUrlError, check_repo_url
from agentic_cp.policy_sync.service import Actor
from agentic_cp.rbac.codeowners import Maintainers, fetch_codeowners, maintainers
from agentic_cp.settings import Settings
from agentic_cp.tables import repo_maintainers, repos, users
from agentic_cp.tenancy.context import require_tenant_id, tenant_tx
from agentic_cp.tenancy.tenants import TenantRow, bump_rbac_revision, current_tenant, lock_rbac

log = logging.getLogger("agentic_cp.repos")
#: The actor of the worker's CODEOWNERS refreshes.
REFRESH_ACTOR = Actor("system:codeowners-sync")


@dataclass(frozen=True)
class RepoRow:
    repo_key: str
    remote: str
    team: str
    owners: tuple[str, ...]
    unresolved: tuple[str, ...]
    codeowners_path: str | None
    codeowners_commit: str | None
    refreshed_at: datetime | None


def _row(row) -> RepoRow:
    return RepoRow(
        repo_key=row.repo_key,
        remote=row.remote,
        team=row.team,
        owners=tuple(row.owners),
        unresolved=tuple(row.unresolved),
        codeowners_path=row.codeowners_path,
        codeowners_commit=row.codeowners_commit,
        refreshed_at=row.refreshed_at,
    )


async def get_repo(conn: AsyncConnection, repo_key: str) -> RepoRow | None:
    row = (await conn.execute(select(repos).where(repos.c.repo_key == repo_key))).first()
    return _row(row) if row is not None else None


async def insert_repo(
    conn: AsyncConnection, repo_key: str, remote: str, team: str, registered_by: str
) -> bool:
    """Insert the repo; False when another registration inserted it first (review F8)."""
    tenant_id = await require_tenant_id(conn)
    inserted = await conn.execute(
        insert(repos)
        .values(
            tenant_id=tenant_id,
            repo_key=repo_key,
            remote=remote,
            team=team,
            registered_by=registered_by,
        )
        .on_conflict_do_nothing()
        .returning(repos.c.repo_key)
    )
    return inserted.first() is not None


async def maintainer_emails(conn: AsyncConnection, repo_key: str) -> list[str]:
    rows = await conn.execute(
        select(users.c.email)
        .join(
            repo_maintainers,
            (repo_maintainers.c.tenant_id == users.c.tenant_id)
            & (repo_maintainers.c.user_id == users.c.id),
        )
        .where(repo_maintainers.c.repo_key == repo_key)
        .order_by(users.c.email)
    )
    return [email for (email,) in rows]


async def refresh(
    conn: AsyncConnection,
    git: GitRunner,
    settings: Settings,
    tenant: TenantRow,
    repo_key: str,
    clock: Callable[[], datetime],
    actor: Actor,
) -> RepoRow:
    """Fetch the repo's CODEOWNERS and resync its maintainers. `GitError` when the repo
    cannot be fetched (nothing changes then)."""
    row = await get_repo(conn, repo_key)
    if row is None:
        raise LookupError(repo_key)
    try:  # again here: a remote stored before the rule, or by hand, never reaches git (R12)
        remote = check_repo_url(row.remote, allow_local=settings.allow_local_repos)
    except RepoUrlError as error:
        raise GitError(str(error)) from None
    workdir = Path(settings.policy_workdir) / str(tenant.id) / "repos"
    found = await asyncio.to_thread(fetch_codeowners, git, workdir, remote, tenant.slug)
    derived = maintainers(found.text, repo_host(repo_key)) if found else Maintainers()
    await conn.execute(
        update(repos)
        .where(repos.c.repo_key == repo_key)
        .values(
            owners=sorted(derived.owners),
            unresolved=sorted(derived.unresolved),
            codeowners_path=found.path if found else None,
            codeowners_commit=found.commit if found else None,
            refreshed_at=clock(),
        )
    )
    commit = found.commit if found else None
    await resync(conn, repo_key, sorted(derived.owners), clock, actor, commit=commit)
    return await get_repo(conn, repo_key)


async def resync(
    conn: AsyncConnection,
    repo_key: str,
    owners: list[str],
    clock: Callable[[], datetime],
    actor: Actor,
    *,
    commit: str | None = None,
) -> bool:
    """Make `repo_maintainers` of `repo_key` the active users matching `owners`; True when
    the set changed (then the revision moved and the change is audited)."""
    tenant_id = await require_tenant_id(conn)
    await lock_rbac(conn)
    wanted: dict[UUID, tuple[str, str]] = {}
    if owners:
        rows = await conn.execute(
            select(users.c.id, users.c.email, users.c.handles).where(
                users.c.status == "active",
                or_(users.c.handles.overlap(owners), users.c.email.in_(owners)),
            )
        )
        for user_id, email, handles in rows:
            matched = email if email in owners else sorted(set(handles) & set(owners))[0]
            wanted[user_id] = (email, matched)
    current = {
        user_id: email
        for user_id, email in await conn.execute(
            select(repo_maintainers.c.user_id, users.c.email)
            .join(
                users,
                (users.c.tenant_id == repo_maintainers.c.tenant_id)
                & (users.c.id == repo_maintainers.c.user_id),
            )
            .where(repo_maintainers.c.repo_key == repo_key)
        )
    }
    if set(current) == set(wanted):
        return False
    await conn.execute(repo_maintainers.delete().where(repo_maintainers.c.repo_key == repo_key))
    if wanted:
        await conn.execute(
            repo_maintainers.insert().values(
                [
                    {
                        "tenant_id": tenant_id,
                        "repo_key": repo_key,
                        "user_id": user_id,
                        "source_entry": entry,
                    }
                    for user_id, (_, entry) in wanted.items()
                ]
            )
        )
    revision = await bump_rbac_revision(conn)
    after: dict = {"users": sorted(email for email, _ in wanted.values())}
    after["rbac_revision"] = revision
    if commit:
        after["commit"] = commit
    await append(
        conn,
        AuditEvent(
            event="maintainers.synced",
            actor=actor.name,
            actor_kind=actor.kind,
            target=f"repo:{repo_key}",
            before={"users": sorted(current.values())},
            after=after,
        ),
        clock=clock,
    )
    return True


async def resync_all(conn: AsyncConnection, clock: Callable[[], datetime], actor: Actor) -> None:
    """Resync every repo of the tenant from its stored owners (after a handle change)."""
    for repo_key, owners in await conn.execute(select(repos.c.repo_key, repos.c.owners)):
        await resync(conn, repo_key, list(owners), clock, actor)


async def refresh_due(
    engine: AsyncEngine,
    git: GitRunner,
    tenant_id: UUID,
    settings: Settings,
    clock: Callable[[], datetime],
) -> None:
    """The worker's CODEOWNERS refresh: every repo of the tenant not refreshed for
    `codeowners_refresh_minutes`, each in its own transaction."""
    cutoff = clock() - timedelta(minutes=settings.codeowners_refresh_minutes)
    async with tenant_tx(engine, tenant_id) as conn:
        due = [
            key
            for (key,) in await conn.execute(
                select(repos.c.repo_key).where(
                    or_(repos.c.refreshed_at.is_(None), repos.c.refreshed_at < cutoff)
                )
            )
        ]
    for repo_key in due:
        try:
            async with tenant_tx(engine, tenant_id) as conn:
                tenant = await current_tenant(conn)
                await refresh(conn, git, settings, tenant, repo_key, clock, REFRESH_ACTOR)
        except GitError as error:
            log.warning("repo %s: CODEOWNERS refresh failed: %s", repo_key, error)
            async with tenant_tx(engine, tenant_id) as conn:  # next try after the interval
                await conn.execute(
                    update(repos).where(repos.c.repo_key == repo_key).values(refreshed_at=clock())
                )
