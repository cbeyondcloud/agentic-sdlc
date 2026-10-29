"""Run health (M3-07): heartbeats in, zombies out.

A CLI driving a run sends `POST /v1/runs/{run_id}/heartbeat` every
`run_health.heartbeat_seconds`. `record_heartbeat` counts a beat at the server's now minus
the beat's own delay (`sent_at - beat_at`, both client times, so the client's clock skew
cancels out): a beat taken longer ago than the window - one queued while the CLI was
offline - is a sign of nothing and is not counted. `state: waiting` (the CLI's last beat
when the run stops at a gate or a pause) sets the run's `waiting`, and so does an
ingested batch ending in a wait (`waits`); only a running beat clears it, so a batch
delivered late never makes a waiting run look dead.

The worker's `zombie_job` marks an open run that is not waiting and has shown no sign of
life - no counted beat, no event, nothing since it opened - for `run_health.stale_minutes`
of the tenant's active policy `stale` (a run with events whose CLI never beats - an older
release - for the deployment's `run_slot_hours`, review F2: a long agent step sends
nothing). `stale` is not `open`, so the run stops counting
against the M2-08 caps at once; `run.stale` is on the audit chain and its starter is
mailed once per episode. A counted beat revives a stale run (`run.revived`, ruling: the
CLI is alive, so the run is; it takes its slot again even over a cap, which the audit
shows); an event of a stale run is stored as evidence and leaves it stale (a replay by
`agentic sync` of a run that died), except `run_closed`, which closes it."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from agentic.orgapi import HeartbeatRequest, HeartbeatResult
from agentic.policy.model import RunHealthPolicy
from agentic_cp.alerts.sinks import active_person
from agentic_cp.audit import AuditEvent, append
from agentic_cp.auth.tokens import TokenContext
from agentic_cp.errors import not_found
from agentic_cp.ingest.register import version_settings
from agentic_cp.notify.outbox import SYSTEM, enqueue
from agentic_cp.notify.templates import render
from agentic_cp.policy_sync.service import active_policy
from agentic_cp.rbac.authz import EnforcerCache, Team, require
from agentic_cp.tables import runs
from agentic_cp.tenancy.context import tenant_tx
from agentic_cp.tenancy.tenants import current_tenant

#: A beat claiming a longer delay than this is judged as this old (a client's bad clock).
MAX_DELAY = timedelta(days=1)
#: Runs marked stale per tenant per worker pass; the rest wait for the next pass.
SWEEP_BATCH = 500


def waits(entries: Sequence[Mapping]) -> bool:
    """Whether a batch ends with the run waiting: a gate (or another person's decision)
    or a step a pause switch or a tool approval held."""
    if not entries:
        return False
    last = entries[-1]
    if last.get("event") == "gate_waiting":
        return True
    return last.get("event") == "step_finished" and last.get("outcome") == "paused"


def window_from(stored: Mapping | None) -> timedelta:
    """The zombie window of a version's run settings; the default for a version stored
    before M3-07 or a tenant without an active policy."""
    found = (stored or {}).get("run_health") or {}
    minutes = found.get("stale_minutes", RunHealthPolicy().stale_minutes)
    return timedelta(minutes=int(minutes))


async def stale_window(conn: AsyncConnection) -> timedelta:
    """The transaction's tenant's zombie window, from its active policy."""
    active = await active_policy(conn)
    return window_from(await version_settings(conn, active.version) if active else None)


async def record_heartbeat(
    conn: AsyncConnection,
    ctx: TokenContext,
    run_id: str,
    body: HeartbeatRequest,
    *,
    cache: EnforcerCache,
    clock: Callable[[], datetime],
) -> HeartbeatResult:
    """One beat of the run, from its starter or a holder of `run.cancel.any` in its team."""
    row = (
        await conn.execute(select(runs).where(runs.c.run_id == run_id).with_for_update())
    ).first()
    if row is None:
        raise not_found("run")
    if ctx.user_id != row.started_by:
        await require(ctx, "run.cancel.any", Team(row.team), conn=conn, cache=cache)
    now = clock()
    delay = min(max(body.sent_at - body.beat_at, timedelta(0)), MAX_DELAY)
    alive_at = now - delay
    if row.status not in ("open", "stale") or alive_at < now - await stale_window(conn):
        return HeartbeatResult(status=row.status, counted=False)
    values: dict = {"waiting": body.state == "waiting"}
    if row.last_heartbeat_at is None or alive_at > row.last_heartbeat_at:
        values["last_heartbeat_at"] = alive_at
    revived = row.status == "stale"
    if revived:
        values.update(status="open", stale_at=None)
    await conn.execute(update(runs).where(runs.c.run_id == run_id).values(**values))
    if revived:
        await append(
            conn,
            AuditEvent(
                event="run.revived",
                actor=ctx.email,
                actor_kind=ctx.actor_kind,
                target=f"run:{run_id}",
                team=row.team,
                run_id=run_id,
                reason="its CLI sent a heartbeat after the run was marked stale",
                before={"status": "stale", "stale_at": row.stale_at.isoformat()},
                after={"status": "open", "state": body.state},
            ),
            clock=clock,
        )
    return HeartbeatResult(status="open", counted=True, revived=revived)


async def sweep(
    conn: AsyncConnection, clock: Callable[[], datetime], quiet: timedelta | None = None
) -> list[str]:
    """One zombie pass over the transaction's tenant: the runs it marked stale.

    A run with events whose CLI never sent a beat (a release before M3-07, review F2) is
    silent for a whole agent step, and a stale run's events never revive it: it is judged
    by `quiet` (the deployment's `run_slot_hours`, when it holds no slot anyway) instead of
    the policy's window. A registered run with no event and no beat (DF-509) and a run
    that beats are judged by the window."""
    window = await stale_window(conn)
    quiet = max(quiet or window, window)
    now = clock()
    seen = func.greatest(runs.c.opened_at, runs.c.last_event_at, runs.c.last_heartbeat_at)
    unbeaten = runs.c.last_heartbeat_at.is_(None) & runs.c.last_event_at.is_not(None)
    found = await conn.execute(
        select(
            runs.c.run_id,
            runs.c.team,
            runs.c.started_by,
            seen.label("seen"),
            unbeaten.label("unbeaten"),
        )
        .where(
            runs.c.status == "open",
            runs.c.waiting.is_(False),
            or_(
                and_(unbeaten, seen < now - quiet),
                and_(~unbeaten, seen < now - window),
            ),
        )
        .order_by(runs.c.run_id)
        .limit(SWEEP_BATCH)
        .with_for_update(skip_locked=True)
    )
    marked = []
    for row in found.all():
        minutes = int((quiet if row.unbeaten else window).total_seconds() // 60)
        await conn.execute(
            update(runs).where(runs.c.run_id == row.run_id).values(status="stale", stale_at=now)
        )
        await append(
            conn,
            AuditEvent(
                event="run.stale",
                actor=SYSTEM,
                actor_kind="system",
                target=f"run:{row.run_id}",
                team=row.team,
                run_id=row.run_id,
                reason=f"no heartbeat or event for {minutes} minutes: its concurrency slot "
                "is released",
                before={"status": "open"},
                after={
                    "status": "stale",
                    "last_seen": row.seen.isoformat(),
                    "stale_minutes": minutes,
                },
            ),
            clock=clock,
        )
        await _mail(conn, row, minutes, clock)
        marked.append(row.run_id)
    return marked


async def _mail(conn: AsyncConnection, row, minutes: int, clock) -> None:
    starter = await active_person(conn, row.started_by)  # None: a service account's run
    if starter is None:
        return
    tenant = await current_tenant(conn)
    rendered = render(
        "run.stale",
        {"run_id": row.run_id, "team": row.team, "minutes": str(minutes)},
        tenant_name=tenant.name,
    )
    await enqueue(
        conn,
        kind="run.stale",
        recipient=starter,
        rendered=rendered,
        # once per stale episode: a revived run that goes stale again was seen later
        dedupe_key=f"stale:{row.run_id}:{row.seen.isoformat()}:{starter}",
        actor=SYSTEM,
        clock=clock,
        team=row.team,
    )


async def zombie_job(
    engine: AsyncEngine, git, tenant_id: UUID, settings, clock: Callable[[], datetime]
) -> list[str]:
    """The worker's per-tenant job (`policy_sync.worker.EXTRA_JOBS`)."""
    async with tenant_tx(engine, tenant_id) as conn:
        return await sweep(conn, clock, timedelta(hours=settings.run_slot_hours))
