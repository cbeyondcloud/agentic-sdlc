"""Alert sinks (M2-16, ruling P4): the mail outbox, and the webhook seam.

`MailAlertSink` queues one mail per recipient with a dedupe key per crossing and recipient:
a run alert goes to the run's starter, a team alert to the active people holding, at team
scope, a role that grants `budget.raise.team` in that team (never service accounts).
`NoWebhook` is the webhook seam's default: a deployment that posts alerts to a chat or
incident tool passes its own sink to `create_app(alert_webhook=...)` (interface only in
this slice)."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncConnection

from agentic_cp.alerts.budget import AlertSink, BudgetAlert
from agentic_cp.notify.outbox import SYSTEM, enqueue
from agentic_cp.notify.templates import render
from agentic_cp.tables import assignments, roles, users
from agentic_cp.tenancy.tenants import current_tenant

#: The most people one alert or gate mail goes to.
MAX_RECIPIENTS = 20


async def people_holding(
    conn: AsyncConnection, team: str, *, role: str | None = None, permission: str | None = None
) -> list[str]:
    """Active people (not service accounts) assigned, in `team`, `role` or a role that
    grants `permission`."""
    query = (
        select(users.c.email)
        .distinct()
        .select_from(assignments)
        .join(
            users,
            and_(users.c.tenant_id == assignments.c.tenant_id, users.c.id == assignments.c.user_id),
        )
        .join(
            roles,
            and_(roles.c.tenant_id == assignments.c.tenant_id, roles.c.name == assignments.c.role),
        )
        .where(
            assignments.c.scope_kind == "team",
            assignments.c.scope_value == team,
            users.c.status == "active",
            users.c.kind == "human",
        )
        .order_by(users.c.email)
        .limit(MAX_RECIPIENTS)
    )
    if role is not None:
        query = query.where(assignments.c.role == role)
    if permission is not None:
        query = query.where(roles.c.permissions.contains([permission]))
    return list((await conn.execute(query)).scalars())


async def active_person(conn: AsyncConnection, user_id) -> str | None:
    found = await conn.execute(
        select(users.c.email).where(
            users.c.id == user_id, users.c.status == "active", users.c.kind == "human"
        )
    )
    return found.scalar_one_or_none()


class MailAlertSink(AlertSink):
    async def deliver(
        self, conn: AsyncConnection, alert: BudgetAlert, *, clock: Callable[[], datetime]
    ) -> None:
        if alert.scope == "run":
            starter = await active_person(conn, alert.starter)
            recipients = [starter] if starter else []
        else:
            recipients = await people_holding(conn, alert.team, permission="budget.raise.team")
        if not recipients:
            return
        tenant = await current_tenant(conn)
        rendered = render(
            "budget.threshold",
            {
                "what": alert.what,
                "percent": str(alert.threshold),
                "spent_usd": f"{alert.spent_usd:.2f}",
                "budget_usd": f"{alert.budget_usd:.2f}",
                "period_phrase": f" in {alert.period}" if alert.period else "",
            },
            tenant_name=tenant.name,
        )
        for email in recipients:
            await enqueue(
                conn,
                kind="budget.threshold",
                recipient=email,
                rendered=rendered,
                dedupe_key=alert.dedupe_key(email),
                actor=SYSTEM,
                clock=clock,
                team=alert.team,
            )


class NoWebhook(AlertSink):
    """The webhook seam's default: nothing is posted."""

    async def deliver(self, conn, alert, *, clock) -> None:
        return None


def default_sinks(webhook: object | None = None) -> list:
    return [MailAlertSink(), webhook if webhook is not None else NoWebhook()]
