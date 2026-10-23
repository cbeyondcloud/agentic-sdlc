"""The durable outbox (M2-07, ruling P2).

`enqueue` writes one `notifications` row in the caller's transaction - the transaction of
the event that causes the mail - so a mail exists exactly when its event committed. The
row's `dedupe_key` is unique per tenant and the insert is `ON CONFLICT DO NOTHING`, so
queueing the same mail again (a replayed ingest batch, a retried request) is a no-op.
`notification.queued` is appended to the tenant's audit chain for each row added, never
with the mail's body."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from uuid import UUID

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncConnection

from agentic_cp.audit import AuditEvent, append
from agentic_cp.notify.templates import Rendered
from agentic_cp.tables import notifications
from agentic_cp.tenancy.context import require_tenant_id

#: The actor of mails the control plane queues on its own (budget alerts, gate waiting).
SYSTEM = "system:control-plane"


async def enqueue(
    conn: AsyncConnection,
    *,
    kind: str,
    recipient: str,
    rendered: Rendered,
    dedupe_key: str,
    actor: str,
    clock: Callable[[], datetime],
    actor_kind: str = "system",
    team: str | None = None,
) -> UUID | None:
    """Queue a mail in the transaction's tenant; its id, or None when `dedupe_key` was
    already queued (nothing is written then)."""
    tenant_id = await require_tenant_id(conn)
    now = clock()
    added = await conn.execute(
        insert(notifications)
        .values(
            tenant_id=tenant_id,
            kind=kind,
            recipient=recipient,
            subject=rendered.subject,
            body_text=rendered.text,
            body_html=rendered.html,
            dedupe_key=dedupe_key[:300],
            next_attempt_at=now,
            created_at=now,
        )
        .on_conflict_do_nothing(index_elements=["tenant_id", "dedupe_key"])
        .returning(notifications.c.id)
    )
    row = added.first()
    if row is None:
        return None
    await append(
        conn,
        AuditEvent(
            event="notification.queued",
            actor=actor,
            actor_kind=actor_kind,
            target=f"notification:{row.id}",
            team=team,
            after={"kind": kind, "to": recipient, "dedupe_key": dedupe_key[:300]},
        ),
        clock=clock,
    )
    return row.id
