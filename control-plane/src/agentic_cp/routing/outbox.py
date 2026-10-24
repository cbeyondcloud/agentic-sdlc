"""Queueing a routing apply (M2-09): one `routing_applies` row, written in the caller's
transaction - the policy activation's, or an admin's re-queue - so an apply exists exactly
when its activation committed. Rows still pending for an older version are superseded:
only the newest version is worth sending to the gateway. `routing.queued` is audited."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from uuid import UUID

from sqlalchemy import update

from agentic_cp.audit import AuditEvent, append
from agentic_cp.settings import Settings
from agentic_cp.tables import routing_applies
from agentic_cp.tenancy.context import require_tenant_id


def gateway_configured(settings: Settings) -> bool:
    """Does this deployment name a gateway admin API to apply routing through?"""
    return bool(settings.gateway_admin_url) and settings.gateway_admin_key is not None


async def queue_apply(
    conn,
    version: str,
    *,
    actor: str,
    actor_kind: str,
    clock: Callable[[], datetime],
) -> UUID:
    """Queue `version` (an accepted version of the transaction's tenant) for the gateway."""
    tenant_id = await require_tenant_id(conn)
    now = clock()
    superseded = await conn.execute(
        update(routing_applies)
        .where(routing_applies.c.status == "pending")
        .values(status="superseded")
        .returning(routing_applies.c.policy_version)
    )
    replaced = sorted({row.policy_version for row in superseded})
    added = await conn.execute(
        routing_applies.insert()
        .values(
            tenant_id=tenant_id,
            policy_version=version,
            requested_by=actor,
            next_attempt_at=now,
            created_at=now,
        )
        .returning(routing_applies.c.id)
    )
    apply_id = added.scalar_one()
    await append(
        conn,
        AuditEvent(
            event="routing.queued",
            actor=actor,
            actor_kind=actor_kind,
            target=f"routing:{apply_id}",
            after={"policy_version": version, "superseded": replaced},
            policy_version=version,
        ),
        clock=clock,
    )
    return apply_id
