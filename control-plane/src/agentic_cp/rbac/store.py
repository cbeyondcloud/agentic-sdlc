"""RBAC rows (DR-1): roles and assignments live in our own typed, RLS-protected tables.

Every function works in the transaction's tenant. `load_rows` is what the read-only casbin
adapter in `authz` loads; nothing here imports casbin. Writers bump `rbac_revision` and
append their audit record in the same transaction (the API does both)."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncConnection

from agentic_cp.tables import assignments, roles, users
from agentic_cp.tenancy.context import require_tenant_id
from agentic_cp.tenancy.tenants import current_tenant

ORG_ADMIN = "org-admin"


@dataclass(frozen=True)
class Grant:
    """`user` holds `role` in `scope` (`org`, `team:<slug>` or `repo:<key>`). `tenant`
    overrides the rows' tenant (only tests build such a row)."""

    user_id: UUID
    role: str
    scope: str
    tenant: UUID | None = None


@dataclass(frozen=True)
class PolicyRows:
    """One tenant's RBAC policy as plain data: role -> permissions, and the grants."""

    tenant_id: UUID
    roles: dict[str, frozenset[str]]
    grants: list[Grant]


@dataclass(frozen=True)
class RoleRow:
    name: str
    permissions: tuple[str, ...]
    builtin: bool
    team_assignable: bool


@dataclass(frozen=True)
class AssignmentRow:
    id: UUID
    user_id: UUID
    email: str
    role: str
    scope_kind: str
    scope_value: str

    @property
    def team(self) -> str | None:
        return self.scope_value or None


def _scope(kind: str, value: str) -> str:
    return "org" if kind == "org" else f"{kind}:{value}"


async def load_rows(conn: AsyncConnection) -> tuple[int, PolicyRows]:
    """The tenant's revision and policy rows, read in one transaction. Grants of disabled
    users are left out."""
    tenant = await current_tenant(conn)
    role_rows = await conn.execute(select(roles.c.name, roles.c.permissions))
    grant_rows = await conn.execute(
        select(
            assignments.c.user_id,
            assignments.c.role,
            assignments.c.scope_kind,
            assignments.c.scope_value,
        )
        .join(
            users,
            (users.c.tenant_id == assignments.c.tenant_id) & (users.c.id == assignments.c.user_id),
        )
        .where(users.c.status == "active")
    )
    rows = PolicyRows(
        tenant_id=tenant.id,
        roles={name: frozenset(perms) for name, perms in role_rows},
        grants=[Grant(user, role, _scope(kind, value)) for user, role, kind, value in grant_rows],
    )
    return tenant.rbac_revision, rows


def _role(row) -> RoleRow:
    return RoleRow(
        name=row.name,
        permissions=tuple(sorted(row.permissions)),
        builtin=row.builtin,
        team_assignable=row.team_assignable,
    )


async def list_roles(conn: AsyncConnection) -> list[RoleRow]:
    return [_role(r) for r in await conn.execute(select(roles).order_by(roles.c.name))]


async def get_role(conn: AsyncConnection, name: str) -> RoleRow | None:
    row = (await conn.execute(select(roles).where(roles.c.name == name))).first()
    return _role(row) if row is not None else None


async def put_role(
    conn: AsyncConnection, name: str, permissions: list[str], team_assignable: bool
) -> RoleRow:
    """Create or replace a role's permissions (a builtin role stays builtin)."""
    tenant_id = await require_tenant_id(conn)
    existing = await get_role(conn, name)
    values = {
        "permissions": sorted(set(permissions)),
        "team_assignable": team_assignable,
        "updated_at": func.now(),
    }
    if existing is None:
        await conn.execute(roles.insert().values(tenant_id=tenant_id, name=name, **values))
    else:
        await conn.execute(roles.update().where(roles.c.name == name).values(**values))
    return await get_role(conn, name)


async def delete_role(conn: AsyncConnection, name: str) -> None:
    await conn.execute(roles.delete().where(roles.c.name == name))


async def role_in_use(conn: AsyncConnection, name: str) -> bool:
    found = await conn.execute(select(assignments.c.id).where(assignments.c.role == name).limit(1))
    return found.first() is not None


def _assignment_query():
    return select(
        assignments.c.id,
        assignments.c.user_id,
        users.c.email,
        assignments.c.role,
        assignments.c.scope_kind,
        assignments.c.scope_value,
    ).join(
        users,
        (users.c.tenant_id == assignments.c.tenant_id) & (users.c.id == assignments.c.user_id),
    )


async def list_assignments(
    conn: AsyncConnection, *, team: str | None = None, email: str | None = None
) -> list[AssignmentRow]:
    query = _assignment_query().order_by(users.c.email, assignments.c.role)
    if team is not None:
        query = query.where(assignments.c.scope_kind == "team", assignments.c.scope_value == team)
    if email is not None:
        query = query.where(users.c.email == email)
    return [AssignmentRow(*row) for row in await conn.execute(query)]


async def get_assignment(conn: AsyncConnection, assignment_id: UUID) -> AssignmentRow | None:
    row = (await conn.execute(_assignment_query().where(assignments.c.id == assignment_id))).first()
    return AssignmentRow(*row) if row is not None else None


async def find_assignment(
    conn: AsyncConnection, user_id: UUID, role: str, kind: str, value: str
) -> AssignmentRow | None:
    query = _assignment_query().where(
        assignments.c.user_id == user_id,
        assignments.c.role == role,
        assignments.c.scope_kind == kind,
        assignments.c.scope_value == value,
    )
    row = (await conn.execute(query)).first()
    return AssignmentRow(*row) if row is not None else None


async def create_assignment(
    conn: AsyncConnection, *, user_id: UUID, role: str, kind: str, value: str, created_by: str
) -> UUID:
    tenant_id = await require_tenant_id(conn)
    created = await conn.execute(
        assignments.insert()
        .values(
            tenant_id=tenant_id,
            user_id=user_id,
            role=role,
            scope_kind=kind,
            scope_value=value,
            created_by=created_by,
        )
        .returning(assignments.c.id)
    )
    return created.scalar_one()


async def delete_assignment(conn: AsyncConnection, assignment_id: UUID) -> None:
    await conn.execute(assignments.delete().where(assignments.c.id == assignment_id))


async def org_admin_count(conn: AsyncConnection) -> int:
    """Active users holding `org-admin` at org scope."""
    found = await conn.execute(
        select(func.count(func.distinct(assignments.c.user_id)))
        .select_from(assignments)
        .join(
            users,
            (users.c.tenant_id == assignments.c.tenant_id) & (users.c.id == assignments.c.user_id),
        )
        .where(
            assignments.c.role == ORG_ADMIN,
            assignments.c.scope_kind == "org",
            users.c.status == "active",
        )
    )
    return found.scalar_one()
