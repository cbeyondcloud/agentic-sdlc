"""Control-plane tokens (owner row 75): opaque bearer values, `acp_` + 32 random bytes,
stored only as sha256, bound to one tenant and one user, revocable at once. People's tokens
last at most 90 days, service accounts' at most 365.

A token is resolved before its tenant is known, through the registered definer function
`resolve_token` (an exact hash match, one row at most); every later step runs inside the
tenant the token names."""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncConnection

from agentic.orgapi import TOKEN_PREFIX
from agentic_cp.settings import Settings
from agentic_cp.tables import tokens
from agentic_cp.tenancy.context import require_tenant_id

RESOLVED_COLUMNS = (
    "tenant_id",
    "tenant_slug",
    "tenant_status",
    "rbac_revision",
    "token_id",
    "user_id",
    "email",
    "kind",
    "user_status",
    "expires_at",
    "revoked_at",
)
#: Longest plausible token: the prefix plus 43 base64url characters (32 bytes).
_MAX_LENGTH = 128
_ALPHABET = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")


class TokenLifetimeError(ValueError):
    """A token was requested for longer than its kind allows."""


def new_token() -> tuple[str, bytes]:
    """A fresh token value and its sha256 (the only form ever stored)."""
    value = TOKEN_PREFIX + secrets.token_urlsafe(32)
    return value, token_digest(value)


def token_digest(value: str) -> bytes:
    return hashlib.sha256(value.encode("utf-8")).digest()


def well_formed(value: str) -> bool:
    """True when `value` could be a token; anything else is refused without a lookup."""
    body = value[len(TOKEN_PREFIX) :]
    return (
        value.startswith(TOKEN_PREFIX) and 20 <= len(body) <= _MAX_LENGTH and set(body) <= _ALPHABET
    )


@dataclass(frozen=True)
class ResolvedToken:
    """What `resolve_token` returns for a hash (no token value, no other tenant)."""

    tenant_id: UUID
    tenant_slug: str
    tenant_status: str
    rbac_revision: int
    token_id: UUID
    user_id: UUID
    email: str
    kind: str
    user_status: str
    expires_at: datetime
    revoked_at: datetime | None


@dataclass(frozen=True)
class TokenContext:
    """The authenticated caller of one request: the tenant comes from here only."""

    tenant_id: UUID
    tenant_slug: str
    user_id: UUID
    email: str
    kind: str
    token_id: UUID
    rbac_revision: int
    expires_at: datetime

    @property
    def actor_kind(self) -> str:
        return "human" if self.kind == "human" else "service"


async def resolve_token(conn: AsyncConnection, value: str) -> ResolvedToken | None:
    """The token's tenant and principal, or None. Runs with no tenant set."""
    found = func.resolve_token(token_digest(value)).table_valued(*RESOLVED_COLUMNS)
    row = (await conn.execute(select(found))).first()
    return ResolvedToken(**row._mapping) if row is not None else None


def token_lifetime(settings: Settings, kind: str, days: int | None) -> timedelta:
    """The lifetime of a new token for a principal of `kind` (owner row 75)."""
    longest = settings.user_token_days if kind == "human" else settings.service_token_max_days
    if days is None:
        return timedelta(days=longest)
    if days > longest:
        who = "people" if kind == "human" else "service accounts"
        raise TokenLifetimeError(f"tokens for {who} last at most {longest} days")
    return timedelta(days=days)


async def issue_token(
    conn: AsyncConnection,
    *,
    user_id: UUID,
    name: str,
    expires_at: datetime,
    created_by: str,
) -> tuple[UUID, str]:
    """Store a new token for `user_id` in the transaction's tenant; returns its id and the
    value (shown to the caller once, never stored)."""
    tenant_id = await require_tenant_id(conn)
    value, digest = new_token()
    created = await conn.execute(
        tokens.insert()
        .values(
            tenant_id=tenant_id,
            user_id=user_id,
            name=name,
            token_sha256=digest,
            created_by=created_by,
            expires_at=expires_at,
        )
        .returning(tokens.c.id)
    )
    return created.scalar_one(), value


async def own_token(conn: AsyncConnection, token_id: UUID, user_id: UUID):
    """The caller's own token row (any state), or None."""
    found = await conn.execute(
        select(tokens.c.id, tokens.c.name, tokens.c.revoked_at).where(
            tokens.c.id == token_id, tokens.c.user_id == user_id
        )
    )
    return found.first()


async def revoke_token(conn: AsyncConnection, token_id: UUID, *, now: datetime) -> bool:
    """Revoke a token of the transaction's tenant; False when it was already revoked."""
    revoked = await conn.execute(
        tokens.update()
        .where(tokens.c.id == token_id, tokens.c.revoked_at.is_(None))
        .values(revoked_at=now)
        .returning(tokens.c.id)
    )
    return revoked.first() is not None


async def list_tokens(conn: AsyncConnection, user_id: UUID) -> list:
    found = await conn.execute(
        select(tokens).where(tokens.c.user_id == user_id).order_by(tokens.c.created_at)
    )
    return list(found)


#: A token's stored last use moves at most this often.
TOUCH_EVERY = timedelta(minutes=1)


async def touch_token(conn: AsyncConnection, token_id: UUID, *, now: datetime) -> None:
    """Record use, at most once a minute (a write per request would be wasteful)."""
    await conn.execute(
        tokens.update()
        .where(
            and_(
                tokens.c.id == token_id,
                or_(
                    tokens.c.last_used_at.is_(None),
                    tokens.c.last_used_at < now - TOUCH_EVERY,
                ),
            )
        )
        .values(last_used_at=now)
    )


class RecentTouches:
    """The tokens this process recorded the use of within `TOUCH_EVERY`: their next request
    needs no `touch_token` transaction, which would change nothing (M3-22: it was a third of
    every request's database work). Bounded: when full it starts again empty."""

    def __init__(self, max_tokens: int = 100_000) -> None:
        self.max_tokens = max_tokens
        self._at: dict[UUID, datetime] = {}

    def due(self, token_id: UUID, now: datetime) -> bool:
        last = self._at.get(token_id)
        return last is None or not (last <= now < last + TOUCH_EVERY)

    def done(self, token_id: UUID, now: datetime) -> None:
        if len(self._at) >= self.max_tokens:
            self._at.clear()
        self._at[token_id] = now
