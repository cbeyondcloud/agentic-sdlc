"""Tenant rows: created by the owner role (operator commands), read by the runtime role.

The runtime role sees only its own tenant's row (the `own_tenant` policy) and may change
only `rbac_revision` (a column grant)."""

from __future__ import annotations

import re
from dataclasses import dataclass, fields
from datetime import datetime
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from agentic_cp.tables import SLUG_PATTERN, tenants
from agentic_cp.tenancy.context import TenancyError, require_tenant_id


class TenantExists(TenancyError):
    """A tenant with that slug already exists."""


@dataclass(frozen=True)
class TenantRow:
    id: UUID
    slug: str
    name: str
    status: str
    policy_repo_url: str | None
    policy_branch: str
    webhook_token_sha256: bytes | None
    gateway_tenant: str | None
    rbac_revision: int
    created_at: datetime
    mail_from: str | None = None
    mail_from_name: str | None = None

    def __repr__(self) -> str:  # the webhook hash is not for logs
        return f"TenantRow(id={self.id}, slug={self.slug!r}, status={self.status!r})"


def _row(row) -> TenantRow:
    mapping = row._mapping
    return TenantRow(**{f.name: mapping[f.name] for f in fields(TenantRow)})


async def create_tenant(
    owner_conn: AsyncConnection,
    slug: str,
    name: str,
    *,
    tenant_id: UUID | None = None,
    policy_repo: str | None = None,
    policy_branch: str = "main",
    gateway_tenant: str | None = None,
) -> TenantRow:
    """Create a tenant (owner role only). A used or malformed slug is refused. A caller
    that must work inside the new tenant in the same transaction picks its `tenant_id`
    first (the bootstrap)."""
    if not re.fullmatch(SLUG_PATTERN, slug):
        raise TenancyError(
            "a tenant slug is lower-case letters, digits and '-', starting with a letter "
            "(2-63 characters)"
        )
    taken = await owner_conn.execute(select(tenants.c.id).where(tenants.c.slug == slug))
    if taken.first() is not None:
        raise TenantExists(f"tenant {slug!r} already exists")
    created = await owner_conn.execute(
        tenants.insert()
        .values(
            slug=slug,
            name=name,
            policy_repo_url=policy_repo,
            policy_branch=policy_branch,
            gateway_tenant=gateway_tenant,
            **({"id": tenant_id} if tenant_id is not None else {}),
        )
        .returning(*tenants.c)
    )
    return _row(created.one())


async def tenant_by_slug(owner_conn: AsyncConnection, slug: str) -> TenantRow | None:
    """Look a tenant up by slug (owner role: operator commands only)."""
    found = await owner_conn.execute(select(tenants).where(tenants.c.slug == slug))
    row = found.first()
    return _row(row) if row is not None else None


async def list_tenants(owner_conn: AsyncConnection) -> list[TenantRow]:
    """Every tenant (owner role: operator commands only)."""
    return [_row(r) for r in await owner_conn.execute(select(tenants).order_by(tenants.c.slug))]


async def current_tenant(conn: AsyncConnection) -> TenantRow:
    """The row of the transaction's tenant."""
    tenant_id = await require_tenant_id(conn)
    found = await conn.execute(select(tenants).where(tenants.c.id == tenant_id))
    row = found.first()
    if row is None:
        raise TenancyError("the transaction's tenant does not exist")
    return _row(row)


async def lock_rbac(conn: AsyncConnection) -> None:
    """Serialise the tenant's RBAC writers: wait for, then hold until commit, the lock on the
    tenant's row that `bump_rbac_revision` takes anyway. Every RBAC write calls it before
    its first read, so a guard (the last org admin, a duplicate assignment) reads what the
    previous writer committed instead of a snapshot both share (READ COMMITTED). The
    no-key lock leaves inserts elsewhere in the tenant (their foreign-key checks) free."""
    tenant_id = await require_tenant_id(conn)
    await conn.execute(
        select(tenants.c.id).where(tenants.c.id == tenant_id).with_for_update(key_share=True)
    )


async def bump_rbac_revision(conn: AsyncConnection) -> int:
    """Advance the transaction's tenant's RBAC revision; returns the new value. Called in
    the same transaction as every RBAC change, so every replica's enforcer cache sees it."""
    tenant_id = await require_tenant_id(conn)
    bumped = await conn.execute(
        update(tenants)
        .where(tenants.c.id == tenant_id)
        .values(rbac_revision=tenants.c.rbac_revision + 1)
        .returning(tenants.c.rbac_revision)
    )
    return bumped.scalar_one()
