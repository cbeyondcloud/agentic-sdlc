"""The retention job (M3-06, OR-01, owner decision 115): delete what a tenant's
`retention.yaml` says has expired.

`prune_tenant` works on one tenant in the caller's transaction (the owner role: the evidence
tables are insert-only for the runtime role, `db_roles.INSERT_ONLY`). In order:

1. **feedback** rows captured before the feedback cut-off;
2. **feedback journals** (`feedback_events`) of runs whose last journal entry is that old
   and that have no feedback row left;
3. **runs** whose last activity (opening, last event, last heartbeat) is before the runs
   cut-off and that have no feedback left, with all their **run events**;
4. sent and failed **mails** (`notifications`) older than the logs cut-off (a mail not sent
   yet is never deleted);
5. the **audit chain**'s oldest records written before the audit cut-off, as one unbroken
   prefix: it stops at the first newer record and at the first record a legal hold covers.

Then `retention.pruned` goes on the tenant's chain with the counts, the periods and holds
in force and, when audit records go, the checkpoint (`through_seq`, `through_hash`: the last
pruned record). The records up to the checkpoint are deleted after the event is written, in
the same transaction, so `audit.verify` finds the checkpoint inside the hash chain and the
chain verifies from its new first record. A legal hold stops deletion in its scope: its
`data` kinds (all when empty) for its `teams` (the whole tenant when empty). A kind kept
forever (`null`) is skipped. Each kind deletes at most `batch` rows (runs: `batch` runs) a
pass; `more` says that some were left for the next one.

`run_retention` runs every tenant under the session advisory lock `agentic.retention`, so
two replicas or two overlapping CronJob runs never prune at once (the worker's pattern)."""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import Select, delete, exists, func, select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from agentic_cp.audit import AuditEvent, append, format_at
from agentic_cp.ingest.register import version_settings
from agentic_cp.policy_sync.service import active_policy
from agentic_cp.tables import (
    audit_log,
    feedback,
    feedback_events,
    notifications,
    run_events,
    runs,
)
from agentic_cp.tenancy.context import tenant_tx
from agentic_cp.tenancy.tenants import list_tenants

log = logging.getLogger("agentic_cp.retention")

#: The actor of the retention job in the audit chain.
RETENTION_ACTOR = "system:retention"
LEADER_KEY = "agentic.retention"
#: Rows deleted per kind per pass (runs: runs, each with all its events).
BATCH = 5000
COUNTED = ("feedback", "feedback_events", "runs", "run_events", "notifications", "audit")


@dataclass
class PruneReport:
    """What one tenant's pass deleted (or, in a dry run, would delete)."""

    counts: dict[str, int] = field(default_factory=lambda: dict.fromkeys(COUNTED, 0))
    audit_through: tuple[int, str] | None = None
    holds: list[str] = field(default_factory=list)
    more: bool = False
    dry_run: bool = False
    skipped: str | None = None
    problems: list[str] = field(default_factory=list)

    def line(self) -> str:
        if self.skipped:
            return f"skipped ({self.skipped})"
        verb = "would delete" if self.dry_run else "deleted"
        shown = ", ".join(f"{kind} {count}" for kind, count in self.counts.items())
        tail = " (more left for the next run)" if self.more else ""
        held = f"; legal holds: {', '.join(self.holds)}" if self.holds else ""
        return f"{verb} {shown}{held}{tail}"


def held(holds: list[Mapping], kind: str) -> tuple[bool, set[str]]:
    """Whether a legal hold covers `kind` for the whole tenant, and the teams held for it."""
    whole, teams = False, set()
    for hold in holds:
        if hold.get("data") and kind not in hold["data"]:
            continue
        if hold.get("teams"):
            teams |= set(hold["teams"])
        else:
            whole = True
    return whole, teams


async def _ids(conn: AsyncConnection, query: Select) -> list:
    return [row[0] for row in await conn.execute(query)]


async def prune_tenant(
    conn: AsyncConnection,
    *,
    now: datetime,
    clock: Callable[[], datetime] | None = None,
    dry_run: bool = False,
    batch: int = BATCH,
) -> PruneReport:
    """One pass over the transaction's tenant (see the module docstring)."""
    report = PruneReport(dry_run=dry_run)
    active = await active_policy(conn)
    if active is None:
        report.skipped = "no active policy"
        return report
    stored = (await version_settings(conn, active.version) or {}).get("retention")
    if not stored:
        report.skipped = "the active policy's retention cannot be read"
        return report
    days: dict = stored["days"]
    holds: list[dict] = stored.get("legal_holds", [])
    report.holds = [hold["id"] for hold in holds]

    def cutoff(kind: str) -> datetime | None:
        whole, _ = held(holds, kind)
        return None if days.get(kind) is None or whole else now - timedelta(days=days[kind])

    async def remove(kind: str, table, key, ids: list) -> None:
        if len(ids) >= batch:
            report.more = True
        if ids and not dry_run:
            await conn.execute(delete(table).where(key.in_(ids)))
        report.counts[kind] += len(ids)

    # 1-2. feedback, then the journals no feedback row needs any more
    if (before := cutoff("feedback")) is not None:
        _, teams = held(holds, "feedback")
        query = select(feedback.c.id).where(feedback.c.captured_at < before)
        if teams:
            query = query.where(feedback.c.team.not_in(teams))
        found = await _ids(conn, query.order_by(feedback.c.captured_at).limit(batch))
        await remove("feedback", feedback, feedback.c.id, found)
        left = select(feedback.c.run_id).where(feedback.c.run_id == feedback_events.c.run_id)
        if dry_run:
            left = left.where(feedback.c.id.not_in(found))
        journals = (
            select(feedback_events.c.run_id)
            .where(~exists(left))
            .group_by(feedback_events.c.run_id)
            .having(func.max(feedback_events.c.received_at) < before)
        )
        if teams:
            journals = journals.where(
                feedback_events.c.run_id.not_in(select(runs.c.run_id).where(runs.c.team.in_(teams)))
            )
        run_ids = await _ids(conn, journals.limit(batch))
        if run_ids:
            entries = select(func.count()).where(feedback_events.c.run_id.in_(run_ids))
            report.counts["feedback_events"] += (await conn.execute(entries)).scalar_one()
            if not dry_run:
                await conn.execute(
                    delete(feedback_events).where(feedback_events.c.run_id.in_(run_ids))
                )
            report.more = report.more or len(run_ids) >= batch

    # 3. runs with all their events, once no feedback refers to them
    if (before := cutoff("runs")) is not None:
        _, teams = held(holds, "runs")
        activity = func.greatest(runs.c.opened_at, runs.c.last_event_at, runs.c.last_heartbeat_at)
        query = select(runs.c.run_id).where(
            activity < before,
            ~exists(select(feedback.c.id).where(feedback.c.run_id == runs.c.run_id)),
            ~exists(select(feedback_events.c.seq).where(feedback_events.c.run_id == runs.c.run_id)),
        )
        if teams:
            query = query.where(runs.c.team.not_in(teams))
        run_ids = await _ids(conn, query.order_by(activity).limit(batch))
        if run_ids:
            events = select(func.count()).where(run_events.c.run_id.in_(run_ids))
            report.counts["run_events"] += (await conn.execute(events)).scalar_one()
            if not dry_run:
                await conn.execute(delete(run_events).where(run_events.c.run_id.in_(run_ids)))
        await remove("runs", runs, runs.c.run_id, run_ids)

    # 4. the sent-mail log (mails have no team: a team's logs hold keeps them all)
    _, teams = held(holds, "logs")
    if (before := cutoff("logs")) is not None and not teams:
        sent = func.coalesce(notifications.c.sent_at, notifications.c.created_at)
        query = select(notifications.c.id).where(
            notifications.c.status.in_(("sent", "failed")), sent < before
        )
        found = await _ids(conn, query.order_by(sent).limit(batch))
        await remove("notifications", notifications, notifications.c.id, found)

    # 5. the audit chain's oldest records, as one unbroken prefix
    if (before := cutoff("audit")) is not None:
        await _audit_prefix(conn, report, before, held(holds, "audit")[1], batch)

    if not dry_run:
        through = report.audit_through
        await append(
            conn,
            AuditEvent(
                event="retention.pruned",
                actor=RETENTION_ACTOR,
                actor_kind="system",
                target="retention",
                after={
                    "counts": dict(report.counts),
                    "days": days,
                    "holds": report.holds,
                    "more": report.more,
                    "audit": (
                        {"through_seq": through[0], "through_hash": through[1]}
                        if through is not None
                        else None
                    ),
                },
                policy_version=active.version,
            ),
            clock=clock or (lambda: now),
        )
        if through is not None:
            await conn.execute(delete(audit_log).where(audit_log.c.seq <= through[0]))
    return report


async def _audit_prefix(
    conn: AsyncConnection, report: PruneReport, before: datetime, teams: set[str], batch: int
) -> None:
    """Find the prefix of the chain that may go; record it on `report`."""
    oldest = (await conn.execute(select(func.min(audit_log.c.seq)))).scalar()
    if oldest is None:
        return
    blocking = audit_log.c.at >= format_at(before)
    if teams:
        blocking = blocking | audit_log.c.team.in_(teams)
    first_kept = (await conn.execute(select(func.min(audit_log.c.seq)).where(blocking))).scalar()
    if first_kept is None:  # every record is old: all go (the run's own event stays)
        first_kept = (await conn.execute(select(func.max(audit_log.c.seq)))).scalar() + 1
    through = min(first_kept - 1, oldest + batch - 1)
    if through < oldest:
        return
    report.more = report.more or through < first_kept - 1
    last = (await conn.execute(select(audit_log.c.hash).where(audit_log.c.seq == through))).scalar()
    after = (
        await conn.execute(select(audit_log.c.prev).where(audit_log.c.seq == through + 1))
    ).scalar()
    if last is None or (after is not None and after != last):
        # never prune a prefix that does not link to what stays: the chain is broken there
        report.problems.append(
            f"audit: seq {through + 1} does not link to seq {through}; nothing pruned "
            "(run `admin audit verify`)"
        )
        return
    count = select(func.count()).where(audit_log.c.seq <= through)
    report.counts["audit"] = (await conn.execute(count)).scalar_one()
    report.audit_through = (through, last)


async def run_retention(
    owner: AsyncEngine,
    clock: Callable[[], datetime],
    *,
    dry_run: bool = False,
    tenant: str | None = None,
    batch: int = BATCH,
) -> dict[str, PruneReport | str] | None:
    """Prune every tenant (or the one with slug `tenant`) as the owner role; None when
    another run holds the lock. One tenant's failure is logged and never stops the
    others."""
    from agentic_cp.retention.tenants import purge_deleted

    results: dict[str, PruneReport | str] = {}
    async with owner.connect() as leader:
        key = func.hashtextextended(LEADER_KEY, 0)
        got = (await leader.execute(select(func.pg_try_advisory_lock(key)))).scalar()
        await leader.commit()
        if not got:
            log.info("another retention run holds the lock; nothing done")
            return None
        try:
            async with owner.begin() as conn:
                found = await list_tenants(conn)
            for row in found:
                if tenant is not None and row.slug != tenant:
                    continue
                try:
                    if row.status == "deleted":
                        results[row.slug] = await purge_deleted(owner, row, clock, dry_run=dry_run)
                        continue
                    async with tenant_tx(owner, row.id) as conn:
                        report = await prune_tenant(
                            conn, now=clock(), clock=clock, dry_run=dry_run, batch=batch
                        )
                    for problem in report.problems:
                        log.warning("tenant %s: retention: %s", row.slug, problem)
                    results[row.slug] = report
                except Exception as error:  # one tenant never stops the others
                    log.warning("tenant %s: retention failed (%s)", row.slug, type(error).__name__)
                    log.debug("tenant %s: retention failure", row.slug, exc_info=error)
                    results[row.slug] = type(error).__name__
        finally:
            await leader.execute(select(func.pg_advisory_unlock(key)))
            await leader.commit()
    return results
