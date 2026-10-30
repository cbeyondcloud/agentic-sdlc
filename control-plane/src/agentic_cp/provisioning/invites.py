"""Invites (M2-18, ruling P8): `aci_` + 32 random bytes, stored only as sha256, single use,
expiring after `invite_hours` (72 by default).

`create_invite` revokes the person's earlier unused invites, stores the new one and queues
the invite mail in the caller's transaction - the link puts the token in its fragment, so
no server or log sees it and a mail scanner that opens links cannot use it up. Without a
public URL for the control plane there is nothing to link to, so no invite is made.
`resolve_invite` finds an invite's tenant before any tenant is known, through the
registered definer function of the same name."""

from __future__ import annotations

import hashlib
import secrets
from collections.abc import Callable
from datetime import datetime, timedelta

from sqlalchemy import Text, cast, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from agentic.orgapi import INVITE_PREFIX, InviteInfo, invite_url
from agentic_cp.audit import AuditEvent, append, format_at
from agentic_cp.auth.users import UserRow
from agentic_cp.notify.outbox import SYSTEM, enqueue
from agentic_cp.notify.templates import render
from agentic_cp.settings import Settings
from agentic_cp.tables import invites, notifications
from agentic_cp.tenancy.context import require_tenant_id
from agentic_cp.tenancy.tenants import current_tenant

RESOLVED_COLUMNS = (
    "tenant_id",
    "tenant_status",
    "invite_id",
    "user_id",
    "email",
    "user_kind",
    "user_status",
    "expires_at",
    "used_at",
    "revoked_at",
)
_ALPHABET = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")


def invite_digest(token: str) -> bytes:
    return hashlib.sha256(token.encode("utf-8")).digest()


def well_formed(token: str) -> bool:
    body = token[len(INVITE_PREFIX) :]
    return token.startswith(INVITE_PREFIX) and 20 <= len(body) <= 128 and set(body) <= _ALPHABET


async def resolve_invite(conn: AsyncConnection, token: str):
    """The invite's tenant, user and state, or None. Runs with no tenant set."""
    found = func.resolve_invite(invite_digest(token)).table_valued(*RESOLVED_COLUMNS)
    return (await conn.execute(select(found))).first()


async def revoke_unused(
    conn: AsyncConnection, user: UserRow, *, now: datetime, actor: str, actor_kind: str, clock
) -> int:
    """Revoke the person's invites not yet used; how many."""
    revoked = await conn.execute(
        update(invites)
        .where(
            invites.c.user_id == user.id,
            invites.c.used_at.is_(None),
            invites.c.revoked_at.is_(None),
        )
        .values(revoked_at=now)
        .returning(invites.c.id)
    )
    ids = [row.id for row in revoked]
    await cancel_dead_invite_mails(conn, now=now, clock=clock, invite_ids=ids)
    for invite_id in ids:
        await append(
            conn,
            AuditEvent(
                event="invite.revoked",
                actor=actor,
                actor_kind=actor_kind,
                target=f"invite:{invite_id}",
                after={"user": user.email},
            ),
            clock=clock,
        )
    return len(ids)


async def cancel_dead_invite_mails(
    conn: AsyncConnection,
    *,
    now: datetime,
    clock: Callable[[], datetime],
    invite_ids: list | None = None,
) -> int:
    """DF-572: mark failed every queued invite mail whose invite was revoked, used or has
    expired (only those of `invite_ids`, when given), clearing its body - it holds the live
    link - and audit `notification.cancelled`; how many. Called when invites are revoked
    and before each outbox pass, with or without a relay."""
    if invite_ids is not None and not invite_ids:
        return 0
    found = (
        select(
            notifications.c.id, notifications.c.recipient, invites.c.revoked_at, invites.c.used_at
        )
        .join(
            invites, notifications.c.dedupe_key == func.concat("invite:", cast(invites.c.id, Text))
        )
        .where(
            notifications.c.kind == "invite",
            notifications.c.status == "pending",
            or_(
                invites.c.revoked_at.is_not(None),
                invites.c.used_at.is_not(None),
                invites.c.expires_at <= now,
            ),
        )
        .with_for_update(of=notifications, skip_locked=True)
    )
    if invite_ids is not None:
        found = found.where(invites.c.id.in_(invite_ids))
    rows = list(await conn.execute(found))
    for row in rows:
        if row.revoked_at is not None:
            reason = "the invite was revoked"
        elif row.used_at is not None:
            reason = "the invite was already used"
        else:
            reason = "the invite expired"
        await conn.execute(
            update(notifications)
            .where(notifications.c.id == row.id)
            .values(status="failed", body_text="", body_html="", last_error=reason)
        )
        await append(
            conn,
            AuditEvent(
                event="notification.cancelled",
                actor=SYSTEM,
                actor_kind="system",
                target=f"notification:{row.id}",
                reason=reason,
                after={"kind": "invite", "to": row.recipient},
            ),
            clock=clock,
        )
    return len(rows)


async def create_invite(
    conn: AsyncConnection,
    user: UserRow,
    *,
    settings: Settings,
    clock: Callable[[], datetime],
    actor: str,
    actor_kind: str,
    role: str,
    team: str | None,
) -> InviteInfo | None:
    """A new invite for a person and its mail; None for a service account or when the
    deployment has no public URL (nothing to link to)."""
    if user.kind != "human" or not settings.public_url:
        return None
    now = clock()
    await revoke_unused(conn, user, now=now, actor=actor, actor_kind=actor_kind, clock=clock)
    token = INVITE_PREFIX + secrets.token_urlsafe(32)
    expires_at = now + timedelta(hours=settings.invite_hours)
    created = await conn.execute(
        invites.insert()
        .values(
            tenant_id=await require_tenant_id(conn),
            user_id=user.id,
            token_sha256=invite_digest(token),
            created_by=actor,
            created_at=now,
            expires_at=expires_at,
        )
        .returning(invites.c.id)
    )
    invite_id = created.scalar_one()
    tenant = await current_tenant(conn)
    rendered = render(
        "invite",
        {
            "email": user.email,
            "inviter": actor,
            "role": role,
            "team_phrase": f" in team {team}" if team else "",
            "link": invite_url(settings.public_url, token),
            "expires_at": expires_at.strftime("%Y-%m-%d %H:%M"),
        },
        tenant_name=tenant.name,
    )
    await enqueue(
        conn,
        kind="invite",
        recipient=user.email,
        rendered=rendered,
        dedupe_key=f"invite:{invite_id}",
        actor=actor,
        actor_kind=actor_kind,
        clock=clock,
        team=team,
    )
    await append(
        conn,
        AuditEvent(
            event="invite.created",
            actor=actor,
            actor_kind=actor_kind,
            target=f"invite:{invite_id}",
            team=team,
            after={"user": user.email, "expires_at": format_at(expires_at)},
        ),
        clock=clock,
    )
    # queued either way; `mailed` says whether a relay will send it (review)
    return InviteInfo(
        id=invite_id, email=user.email, expires_at=expires_at, mailed=bool(settings.smtp_host)
    )
