"""`/v1/notifications`: the tenant's outbox as admins see it (M2-07): what is queued, sent or
failed, and why the last attempt failed - never a mail's body (an invite's holds its link)."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Query, Request
from sqlalchemy import select

from agentic.orgapi import API_PREFIX, NotificationInfo
from agentic_cp.auth.deps import Caller
from agentic_cp.errors import PROBLEM_RESPONSES
from agentic_cp.rbac.authz import Org, require
from agentic_cp.tables import notifications
from agentic_cp.tenancy.context import tenant_tx

router = APIRouter(
    prefix=f"{API_PREFIX}/notifications", tags=["notifications"], responses=PROBLEM_RESPONSES
)


@router.get("")
async def list_notifications(
    request: Request,
    ctx: Caller,
    status: Literal["pending", "sent", "failed"] | None = None,
    limit: int = Query(default=100, ge=1, le=500),
) -> list[NotificationInfo]:
    """The newest mails first (org `audit.read.org`)."""
    state = request.app.state
    async with tenant_tx(state.engine, ctx.tenant_id) as conn:
        await require(ctx, "audit.read.org", Org(), conn=conn, cache=state.enforcers)
        query = select(notifications).order_by(notifications.c.created_at.desc()).limit(limit)
        if status is not None:
            query = query.where(notifications.c.status == status)
        rows = list(await conn.execute(query))
    return [
        NotificationInfo(
            id=row.id,
            kind=row.kind,
            recipient=row.recipient,
            subject=row.subject,
            status=row.status,
            attempts=row.attempts,
            next_attempt_at=row.next_attempt_at,
            last_error=row.last_error,
            created_at=row.created_at,
            sent_at=row.sent_at,
        )
        for row in rows
    ]
