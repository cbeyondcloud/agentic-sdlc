"""Draining a tenant's outbox (M2-07, ruling P2).

One mail per transaction: the due row is locked (`FOR UPDATE SKIP LOCKED`), sent while the
lock is held, and updated in the same transaction - so two workers never send one row, and
a row whose send succeeded is marked sent unless the process dies before the commit (the
resend then carries the same Message-ID). A transient failure reschedules the row with
exponential backoff; a permanent one, or the last attempt, marks it failed. A relay that
cannot be used at all (`RelayUnusable`) stops the pass and uses up no attempt, so an outage
of any length loses no mail. Sent and
failed rows lose their bodies (P3). `notification.sent` / `.failed` go on the tenant's
audit chain."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from agentic_cp.audit import AuditEvent, append
from agentic_cp.notify.outbox import SYSTEM
from agentic_cp.notify.smtp import (
    Notifier,
    Outgoing,
    PermanentFailure,
    RelayUnusable,
    notifier_for,
)
from agentic_cp.settings import Settings
from agentic_cp.tables import notifications
from agentic_cp.tenancy.context import tenant_tx
from agentic_cp.tenancy.tenants import current_tenant

log = logging.getLogger("agentic_cp.notify")

#: The longest wait between two attempts.
MAX_BACKOFF = timedelta(minutes=60)
#: When a mail is tried again after the relay could not be used (it goes behind other due
#: mails, so each pass probes the relay with one mail).
RELAY_RETRY = timedelta(minutes=1)


@dataclass
class DeliveryReport:
    sent: int = 0
    retried: int = 0
    failed: int = 0


def backoff(attempts: int) -> timedelta:
    """The wait after the `attempts`-th failed send: 1, 2, 4 ... minutes, at most 60."""
    return min(timedelta(minutes=2 ** max(attempts - 1, 0)), MAX_BACKOFF)


async def deliver_due(
    engine: AsyncEngine,
    tenant_id: UUID,
    notifier: Notifier,
    clock: Callable[[], datetime],
    *,
    settings: Settings,
    batch: int = 50,
) -> DeliveryReport:
    """Send up to `batch` due mails of one tenant."""
    report = DeliveryReport()
    for _ in range(batch):
        async with tenant_tx(engine, tenant_id) as conn:
            now = clock()
            row = (
                await conn.execute(
                    select(notifications)
                    .where(notifications.c.status == "pending")
                    .where(notifications.c.next_attempt_at <= now)
                    .order_by(notifications.c.next_attempt_at, notifications.c.created_at)
                    .limit(1)
                    .with_for_update(skip_locked=True)
                )
            ).first()
            if row is None:
                break
            tenant = await current_tenant(conn)
            sender = tenant.mail_from or settings.smtp_from
            if sender is None:
                log.warning("tenant %s: mail queued but no sender is configured", tenant.slug)
                break
            outgoing = Outgoing(
                id=row.id,
                sender=sender,
                sender_name=tenant.mail_from_name or settings.smtp_from_name,
                recipient=row.recipient,
                subject=row.subject,
                text=row.body_text,
                html=row.body_html,
            )
            attempts = row.attempts + 1
            values: dict = {"attempts": attempts}
            event = None
            relay_down = False
            try:
                await notifier.send(outgoing)
            except Exception as error:  # noqa: BLE001 - every failure is recorded on the row
                relay_down = isinstance(error, RelayUnusable)
                problem = str(error)[:300] or type(error).__name__
                values["last_error"] = problem
                if relay_down:
                    # The relay's failure, not this mail's: no attempt is used up and the
                    # body is kept, however long the outage lasts (review F3).
                    log.warning(
                        "tenant %s: the mail relay cannot be used: %s", tenant.slug, problem
                    )
                    values = {"last_error": problem, "next_attempt_at": now + RELAY_RETRY}
                    report.retried += 1
                elif (
                    isinstance(error, PermanentFailure) or attempts >= settings.notify_max_attempts
                ):
                    values.update(status="failed", body_text="", body_html="")
                    event = ("notification.failed", problem)
                    report.failed += 1
                else:
                    values["next_attempt_at"] = now + backoff(attempts)
                    report.retried += 1
            else:
                values.update(status="sent", sent_at=now, body_text="", body_html="")
                event = ("notification.sent", "")
                report.sent += 1
            await conn.execute(
                update(notifications).where(notifications.c.id == row.id).values(**values)
            )
            if event is not None:
                await append(
                    conn,
                    AuditEvent(
                        event=event[0],
                        actor=SYSTEM,
                        actor_kind="system",
                        target=f"notification:{row.id}",
                        reason=event[1],
                        after={"kind": row.kind, "to": row.recipient, "attempts": attempts},
                    ),
                    clock=clock,
                )
        if relay_down:  # no other mail would get through this pass either (review)
            break
    return report


async def deliver_job(engine, git, tenant_id, settings: Settings, clock) -> None:
    """The worker's per-tenant mail job (`EXTRA_JOBS`): nothing without a relay."""
    notifier = notifier_for(settings)
    if notifier is not None:
        await deliver_due(engine, tenant_id, notifier, clock, settings=settings)
