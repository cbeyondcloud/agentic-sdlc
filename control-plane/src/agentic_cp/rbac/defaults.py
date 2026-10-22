"""The default roles: spec §12.3 and the §23 permission matrix as the seed policy.

`seed_roles` adds every missing builtin role to the transaction's tenant and never changes
an existing one, so a tenant's edits survive releases. It runs for a new tenant
(`admin tenant create`) and for every tenant after `migrate`."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncConnection

from agentic.orgapi.rbac import PERMISSIONS, unknown_permissions
from agentic_cp.audit import OPERATOR, AuditEvent, append
from agentic_cp.tables import roles
from agentic_cp.tenancy.context import require_tenant_id
from agentic_cp.tenancy.tenants import bump_rbac_revision


@dataclass(frozen=True)
class RoleSpec:
    name: str
    permissions: tuple[str, ...]
    team_assignable: bool = False
    """A team lead may assign it within their team (spec §23: Developer/Approver only)."""
    assignable: bool = True
    """False for roles that are only derived (repo-maintainer from CODEOWNERS)."""


ORG_ADMIN = "org-admin"
REPO_MAINTAINER = "repo-maintainer"

DEFAULT_ROLES: tuple[RoleSpec, ...] = (
    RoleSpec(ORG_ADMIN, tuple(PERMISSIONS)),
    RoleSpec(
        "team-lead",
        (
            "run.trigger",
            "run.cancel.own",
            "run.cancel.any",
            "gate.approve",
            "skills.personal.edit",
            "agent.propose.repo",
            "agent.approve.repo",
            "agent.edit.repo",
            "agent.publish.team",
            "pipeline.publish.team",
            "policy.edit.team",
            "budget.raise.team",
            "killswitch.team",
            "rbac.assign.team",
            "dashboard.view.own",
            "dashboard.view.team",
            "audit.read.own",
            "audit.read.team",
            "trace.view.team",
        ),
    ),
    RoleSpec(
        REPO_MAINTAINER,
        (
            "run.trigger",
            "run.cancel.own",
            "gate.approve",
            "skills.personal.edit",
            "agent.propose.repo",
            "agent.approve.repo",
            "agent.edit.repo",
            "dashboard.view.repo",
            "audit.read.repo",
        ),
        assignable=False,
    ),
    RoleSpec(
        "developer",
        (
            "run.trigger",
            "run.cancel.own",
            "gate.approve",
            "skills.personal.edit",
            "agent.propose.repo",
            "agent.edit.repo",
            "dashboard.view.own",
            "audit.read.own",
        ),
        team_assignable=True,
    ),
    RoleSpec(
        "approver",
        ("gate.approve", "skills.personal.edit", "agent.propose.repo", "dashboard.view.team"),
        team_assignable=True,
    ),
    RoleSpec(
        "auditor",
        (
            "audit.read.own",
            "audit.read.team",
            "audit.read.repo",
            "audit.read.org",
            "dashboard.view.own",
            "dashboard.view.team",
            "dashboard.view.repo",
            "dashboard.view.org",
            "trace.view.team",
            "trace.view.org",
        ),
    ),
    RoleSpec("exec-viewer", ("dashboard.view.org",)),
    RoleSpec("service-account", ("run.trigger",)),
)

BUILTIN = {spec.name: spec for spec in DEFAULT_ROLES}
assert not any(unknown_permissions(s.permissions) for s in DEFAULT_ROLES)


async def seed_roles(
    conn: AsyncConnection,
    *,
    clock: Callable[[], datetime],
    actor: str = OPERATOR,
    audit: bool = True,
) -> list[str]:
    """Bring the transaction's tenant's builtin roles up to this release; returns the names
    of the roles added or extended.

    A missing builtin role is added. An existing one gains the permissions this release's
    seed has and the previous seed (`seed_permissions`) did not - permissions new in this
    release - and keeps every edit the tenant made, removals included (R22, DF-419). A role
    seeded before `seed_permissions` existed is taken as seeded with this release.

    A change bumps `rbac_revision` and is audited (`rbac.seeded`) in this transaction;
    `audit=False` is for a caller that records the roles in its own audit record (the
    tenant bootstrap's `rbac.bootstrap`)."""
    existing = {
        row.name: row
        for row in await conn.execute(
            select(roles.c.name, roles.c.permissions, roles.c.builtin, roles.c.seed_permissions)
        )
    }
    missing = [spec for spec in DEFAULT_ROLES if spec.name not in existing]
    extended: dict[str, list[str]] = {}
    for spec in DEFAULT_ROLES:
        row = existing.get(spec.name)
        if row is None or not row.builtin:
            continue
        seeded = set(row.seed_permissions) if row.seed_permissions is not None else None
        new = sorted(set(spec.permissions) - seeded) if seeded is not None else []
        values: dict = {}
        if new:
            values["permissions"] = sorted(set(row.permissions) | set(new))
            extended[spec.name] = new
        if seeded != set(spec.permissions):
            values["seed_permissions"] = sorted(spec.permissions)
        if values:
            await conn.execute(update(roles).where(roles.c.name == spec.name).values(**values))
    if missing:
        tenant_id = await require_tenant_id(conn)
        await conn.execute(
            insert(roles)
            .values(
                [
                    {
                        "tenant_id": tenant_id,
                        "name": spec.name,
                        "permissions": list(spec.permissions),
                        "seed_permissions": sorted(spec.permissions),
                        "builtin": True,
                        "team_assignable": spec.team_assignable,
                    }
                    for spec in missing
                ]
            )
            .on_conflict_do_nothing()
        )
    if not missing and not extended:
        return []
    revision = await bump_rbac_revision(conn)
    added = [spec.name for spec in missing]
    names = added + sorted(extended)
    if not audit:
        return names
    await append(
        conn,
        AuditEvent(
            event="rbac.seeded",
            actor=actor,
            actor_kind="system",
            target="roles",
            after={"roles": added, "added": extended, "rbac_revision": revision},
        ),
        clock=clock,
    )
    return names
