"""Our record of the gateway proxy keys (M2-17): which `key_id` belongs to which user, and
the gateway calls that change them, each audited in the tenant's chain by key id only.

A key is bound to the tenant's `gateway_tenant` (ND-16), the user's email and a team. When
the gateway cannot be reached, a revocation is recorded as requested
(`revoke_requested_at`) and `retry_revocations` - a worker job - finishes it, so a revoked
person's key never quietly stays valid."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from agentic_cp.audit import AuditEvent, append
from agentic_cp.auth.users import UserRow
from agentic_cp.keys.gateway import GatewayKeys, GatewayUnavailable, IssuedKey, gateway_keys_for
from agentic_cp.settings import Settings
from agentic_cp.tables import proxy_keys
from agentic_cp.tenancy.context import require_tenant_id, tenant_tx
from agentic_cp.tenancy.tenants import current_tenant

log = logging.getLogger("agentic_cp.keys")
#: The worker's actor for revocations it finishes.
SYSTEM = "system:control-plane"


async def _audit(conn, clock, event: str, actor: str, actor_kind: str, **fields) -> None:
    await append(
        conn, AuditEvent(event=event, actor=actor, actor_kind=actor_kind, **fields), clock=clock
    )


async def issue_key(
    conn: AsyncConnection,
    gateway: GatewayKeys,
    *,
    user: UserRow,
    team: str | None,
    actor: str,
    actor_kind: str,
    clock: Callable[[], datetime],
) -> IssuedKey:
    """A new proxy key for `user` at the gateway, recorded and audited in this transaction.
    If recording fails, the new key is revoked again (best effort) before the error."""
    tenant = await current_tenant(conn)
    if not tenant.gateway_tenant:
        raise GatewayUnavailable(
            f"tenant {tenant.slug} has no gateway tenant, so no proxy key can be issued "
            "(set it with `admin tenant create --gateway-tenant`)"
        )
    issued = await gateway.create(
        tenant=tenant.gateway_tenant, team=team, user=user.email, label=f"agentic {user.email}"
    )
    try:
        await record_key(conn, key_id=issued.key_id, user_id=user.id, team=team, created_by=actor)
        await _audit(
            conn,
            clock,
            "proxy_key.issued",
            actor,
            actor_kind,
            target=f"proxy_key:{issued.key_id}",
            team=team,
            after={"key_id": issued.key_id, "user": user.email, "team": team},
        )
    except BaseException:
        await forget_issued(gateway, issued)
        raise
    return issued


async def forget_issued(gateway: GatewayKeys, issued: IssuedKey) -> None:
    """Revoke a key the gateway issued but we could not keep (best effort, logged)."""
    try:
        await gateway.revoke(issued.key_id)
    except GatewayUnavailable:
        log.error(
            "proxy key %s was issued but not recorded; revoke it at the gateway", issued.key_id
        )


async def record_key(
    conn: AsyncConnection, *, key_id: str, user_id: UUID, team: str | None, created_by: str
) -> None:
    tenant_id = await require_tenant_id(conn)
    await conn.execute(
        proxy_keys.insert().values(
            tenant_id=tenant_id, key_id=key_id, user_id=user_id, team=team, created_by=created_by
        )
    )


async def active_keys(conn: AsyncConnection, user_id: UUID) -> list:
    """The user's keys not yet revoked at the gateway, newest first."""
    found = await conn.execute(
        select(proxy_keys)
        .where(proxy_keys.c.user_id == user_id, proxy_keys.c.revoked_at.is_(None))
        .order_by(proxy_keys.c.created_at.desc(), proxy_keys.c.key_id)
    )
    return list(found)


async def mark_revoked(conn: AsyncConnection, key_id: str, now: datetime) -> bool:
    """Record a key as revoked; False when it already was (another worker finished it)."""
    marked = await conn.execute(
        update(proxy_keys)
        .where(proxy_keys.c.key_id == key_id, proxy_keys.c.revoked_at.is_(None))
        .values(
            revoked_at=now, revoke_requested_at=func.coalesce(proxy_keys.c.revoke_requested_at, now)
        )
        .returning(proxy_keys.c.key_id)
    )
    return marked.first() is not None


async def request_revocation(conn: AsyncConnection, user_id: UUID, now: datetime) -> list:
    """Mark every active key of the user as to be revoked and return them. The gateway is
    called after this transaction commits (`finish_revocations`); a key it misses, or one
    left by a crash in between, is finished by `retry_revocations`."""
    rows = await active_keys(conn, user_id)
    await conn.execute(
        update(proxy_keys)
        .where(
            proxy_keys.c.user_id == user_id,
            proxy_keys.c.revoked_at.is_(None),
            proxy_keys.c.revoke_requested_at.is_(None),
        )
        .values(revoke_requested_at=now)
    )
    return rows


async def finish_revocations(
    engine: AsyncEngine,
    tenant_id: UUID,
    gateway: GatewayKeys,
    rows: list,
    *,
    actor: str,
    actor_kind: str,
    clock: Callable[[], datetime],
    reason: str,
    user: str | None = None,
    audit_pending: bool = True,
) -> tuple[list[str], list[str]]:
    """Revoke the keys of `rows` at the gateway, then record the outcome: (revoked, pending).

    The gateway calls run with no transaction open (review F2): a slow or unreachable
    gateway (up to the client's timeout per key) never holds the tenant's audit-chain or
    RBAC lock, which every other audited write in the tenant would wait for. Only then one
    short transaction marks the revoked keys and appends their audit records."""
    if not rows:
        return [], []
    revoked, failed = [], {}
    for row in rows:
        try:
            await gateway.revoke(row.key_id)
        except GatewayUnavailable as error:
            failed[row.key_id] = str(error)
        else:
            revoked.append(row.key_id)
    after_user = {"user": user} if user else {}
    async with tenant_tx(engine, tenant_id) as conn:
        for row in rows:
            if row.key_id in failed:
                if not audit_pending:
                    continue
                event, why = "proxy_key.revoke_pending", f"{reason}; {failed[row.key_id]}"
            elif await mark_revoked(conn, row.key_id, clock()):
                event, why = "proxy_key.revoked", reason
            else:  # recorded meanwhile by another worker: audited once, there
                continue
            await _audit(
                conn,
                clock,
                event,
                actor,
                actor_kind,
                target=f"proxy_key:{row.key_id}",
                team=row.team,
                reason=why[:300],
                after={"key_id": row.key_id, **after_user},
            )
    return revoked, list(failed)


async def retry_revocations(
    engine: AsyncEngine, tenant_id: UUID, gateway: GatewayKeys, clock: Callable[[], datetime]
) -> list[str]:
    """Finish the tenant's pending revocations; the key ids revoked now."""
    async with tenant_tx(engine, tenant_id) as conn:
        rows = list(
            await conn.execute(
                select(proxy_keys).where(
                    proxy_keys.c.revoke_requested_at.is_not(None),
                    proxy_keys.c.revoked_at.is_(None),
                )
            )
        )
    revoked, _ = await finish_revocations(
        engine,
        tenant_id,
        gateway,
        rows,
        actor=SYSTEM,
        actor_kind="system",
        clock=clock,
        reason="a pending revocation, finished",
        audit_pending=False,
    )
    return revoked


async def retry_job(engine, git, tenant_id, settings: Settings, clock) -> None:
    """The worker's per-tenant job (`EXTRA_JOBS`)."""
    gateway = gateway_keys_for(settings)
    if gateway.configured:
        await retry_revocations(engine, tenant_id, gateway, clock)
