"""Users of a tenant: people and service accounts. Every function works in the transaction's
tenant (RLS); an email is stored lower-case and is unique per tenant only (ND-17)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from agentic_cp.tables import users
from agentic_cp.tenancy.context import require_tenant_id

_EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
KINDS = ("human", "service")


class UserError(ValueError):
    """A user record was refused (a malformed email, an unknown kind)."""


@dataclass(frozen=True)
class UserRow:
    id: UUID
    email: str
    display_name: str
    kind: str
    status: str


def normalise_email(email: str) -> str:
    value = email.strip().lower()
    if not _EMAIL.fullmatch(value) or len(value) > 254:
        raise UserError("not an email address")
    return value


def _row(row) -> UserRow:
    m = row._mapping
    return UserRow(
        id=m["id"],
        email=m["email"],
        display_name=m["display_name"],
        kind=m["kind"],
        status=m["status"],
    )


async def create_user(
    conn: AsyncConnection, email: str, *, kind: str = "human", display_name: str = ""
) -> UserRow:
    if kind not in KINDS:
        raise UserError(f"a user's kind is one of {', '.join(KINDS)}")
    tenant_id = await require_tenant_id(conn)
    created = await conn.execute(
        users.insert()
        .values(
            tenant_id=tenant_id, email=normalise_email(email), kind=kind, display_name=display_name
        )
        .returning(*users.c)
    )
    return _row(created.one())


async def user_by_email(conn: AsyncConnection, email: str) -> UserRow | None:
    found = await conn.execute(select(users).where(users.c.email == normalise_email(email)))
    row = found.first()
    return _row(row) if row is not None else None


async def user_by_id(conn: AsyncConnection, user_id: UUID) -> UserRow | None:
    found = await conn.execute(select(users).where(users.c.id == user_id))
    row = found.first()
    return _row(row) if row is not None else None
