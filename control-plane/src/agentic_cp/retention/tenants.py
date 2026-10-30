"""Deleting a tenant (DF-568, M3-06): `python -m agentic_cp admin tenant delete`.

Only a suspended tenant is deleted (its tokens, webhooks and invites are already refused),
never one whose active policy lists a legal hold or that still has gateway keys the gateway
has not revoked, and only when the operator repeats its slug. Every row of the tenant goes
except its audit chain: the chain is evidence and is kept for the audit period in force
(`retention.yaml days.audit`, at least 7 years). The tenant row stays with status `deleted`,
its policy source, webhook and mail sender cleared, and `tenant.deleted` (the counts, the
reason, the audit period) closes the chain. When the chain's newest record is older than
that period, the retention run removes the chain and the tenant row (`purge_deleted`); that
last step is logged, as no chain is left to hold it.

The gateway keeps its own records of the tenant (usage, keys): erase them with the
gateway's own tenant erasure (docs/runbooks/retention.md)."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, timedelta

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from agentic.policy.model import AUDIT_FLOOR_DAYS
from agentic_cp.audit import OPERATOR, AuditEvent, append, format_at
from agentic_cp.ingest.register import version_settings
from agentic_cp.policy_sync.service import active_policy
from agentic_cp.retention.prune import PruneReport
from agentic_cp.tables import TENANT_TABLES, audit_log, metadata, proxy_keys, tenants
from agentic_cp.tenancy.context import tenant_tx
from agentic_cp.tenancy.tenants import TenantRow, tenant_by_slug

log = logging.getLogger("agentic_cp.retention")


class TenantDeleteError(Exception):
    """The tenant may not be deleted (not suspended, on legal hold, unknown)."""


def erase_order() -> list:
    """The tenant's tables but the audit chain, children before their parents."""
    wanted = set(TENANT_TABLES) - {"audit_log"}
    return [table for table in reversed(metadata.sorted_tables) if table.name in wanted]


async def delete_tenant(
    owner: AsyncEngine, slug: str, *, reason: str, clock: Callable[[], datetime]
) -> dict[str, int]:
    """Erase tenant `slug` but its audit chain (see the module docstring); the rows
    deleted per table."""
    async with owner.begin() as conn:
        tenant = await tenant_by_slug(conn, slug)
    if tenant is None:
        raise TenantDeleteError(f"no tenant {slug!r}")
    if tenant.status != "suspended":
        raise TenantDeleteError(
            f"tenant {slug} is {tenant.status}: suspend it first "
            "(`admin tenant suspend`), so nobody is using it while it is erased"
        )
    async with tenant_tx(owner, tenant.id) as conn:
        active = await active_policy(conn)
        stored = (await version_settings(conn, active.version) or {}) if active else {}
        retention = stored.get("retention") or {}
        holds = [hold["id"] for hold in retention.get("legal_holds", [])]
        if holds:
            raise TenantDeleteError(
                f"tenant {slug} is under legal hold ({', '.join(holds)}): nothing is deleted "
                "while a hold is listed in its retention.yaml"
            )
        live_keys = select(func.count()).where(proxy_keys.c.revoked_at.is_(None))
        if (await conn.execute(live_keys)).scalar_one():
            raise TenantDeleteError(
                f"tenant {slug} still has gateway keys the gateway has not revoked: revoke "
                "them first (`agentic rbac revoke-keys`, or the gateway's tenant erasure)"
            )
        audit_days = retention.get("days", {}).get("audit", AUDIT_FLOOR_DAYS)
        counts: dict[str, int] = {}
        for table in erase_order():
            result = await conn.execute(delete(table).where(table.c.tenant_id == tenant.id))
            if result.rowcount:
                counts[table.name] = result.rowcount
        await conn.execute(
            update(tenants)
            .where(tenants.c.id == tenant.id)
            .values(
                status="deleted",
                policy_repo_url=None,
                webhook_token_sha256=None,
                mail_from=None,
                mail_from_name=None,
            )
        )
        await append(
            conn,
            AuditEvent(
                event="tenant.deleted",
                actor=OPERATOR,
                actor_kind="system",
                target=f"tenant:{slug}",
                reason=reason,
                before={"status": tenant.status},
                after={"status": "deleted", "counts": counts, "audit_days": audit_days},
            ),
            clock=clock,
        )
    return counts


async def purge_deleted(
    owner: AsyncEngine, tenant: TenantRow, clock: Callable[[], datetime], *, dry_run: bool
) -> PruneReport:
    """Remove a deleted tenant's audit chain and row once its newest record is past the
    audit period recorded when it was deleted (None: kept forever)."""
    report = PruneReport(dry_run=dry_run)
    async with tenant_tx(owner, tenant.id) as conn:
        deleted = (
            await conn.execute(
                select(audit_log.c.after)
                .where(audit_log.c.event == "tenant.deleted")
                .order_by(audit_log.c.seq.desc())
                .limit(1)
            )
        ).scalar()
        newest = (
            await conn.execute(select(audit_log.c.at).order_by(audit_log.c.seq.desc()).limit(1))
        ).scalar()
        days = (deleted or {}).get("audit_days", AUDIT_FLOOR_DAYS)
        if days is None:
            report.skipped = "deleted tenant; its audit chain is kept forever"
            return report
        if newest is not None and newest >= format_at(clock() - timedelta(days=days)):
            report.skipped = f"deleted tenant; its audit chain is kept {days} days"
            return report
        report.counts["audit"] = (
            await conn.execute(select(func.count()).select_from(audit_log))
        ).scalar_one()
        if not dry_run:
            await conn.execute(delete(audit_log))
            await conn.execute(delete(tenants).where(tenants.c.id == tenant.id))
    if not dry_run:
        log.warning(
            "tenant %s (%s): deleted tenant removed with its %d audit records (period %d days)",
            tenant.slug,
            tenant.id,
            report.counts["audit"],
            days,
        )
    return report
