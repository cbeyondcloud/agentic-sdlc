"""Applying a queued routing plan to the gateway (M2-09): the worker's per-tenant job.

One apply is three steps, so no tenant lock is ever held across a network call (the lesson
of M2-17 review F2: every audited write in the tenant would wait for the gateway):

  1. a short transaction leases the newest due `pending` row (`FOR UPDATE SKIP LOCKED`,
     then `next_attempt_at` moved past the lease and the attempt counted) and reads the
     tenant's gateway tenant, the version's plan and the team limits the last applied
     version wrote;
  2. with no transaction open: read the gateway's overlay, replace only
     `tenants.<gateway tenant>` (and, when the deployment allows, this tenant's own
     `rate_limit.per_team` entries), write it naming the version read - a 409 means someone
     wrote meanwhile, so read and redo, up to `CONFLICT_TRIES` times - then read it back
     (that exact version) and compare. An overlay that already holds the plan is not
     written again;
  3. a short transaction records the outcome on the row and in the tenant's audit chain:
     `routing.applied` (gateway version, previous version, plan digest), `routing.refused`
     (a 422: terminal until the next version or a re-queue), a retry with backoff (1, 2,
     4 ... 60 minutes), `routing.apply_failed` after `routing_max_attempts`, or
     `routing.skipped` (the tenant has no gateway tenant).

A row superseded while its apply was in flight keeps that status unless the apply landed:
then it is `applied`, which is what the gateway holds until the newer row is applied."""

from __future__ import annotations

import copy
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from agentic_cp.audit import AuditEvent, append
from agentic_cp.keys.gateway import GatewayUnavailable
from agentic_cp.notify.delivery import backoff
from agentic_cp.policy_sync.service import routing_from_bundle
from agentic_cp.routing.gateway import (
    GatewayRouting,
    RoutingConflict,
    RoutingRefused,
    gateway_routing_for,
)
from agentic_cp.routing.generate import RoutingPlan, plan_digest
from agentic_cp.settings import Settings
from agentic_cp.tables import policy_versions, routing_applies
from agentic_cp.tenancy.context import tenant_tx
from agentic_cp.tenancy.tenants import current_tenant

log = logging.getLogger("agentic_cp.routing")

#: The worker's actor for applies it finishes.
SYSTEM = "system:routing"
#: How long a leased apply is left alone (a crash mid-apply is retried after it).
LEASE = timedelta(minutes=10)
#: Read-modify-write rounds one attempt makes against a gateway someone else is writing.
CONFLICT_TRIES = 3
#: Problems kept in an audit record (the row keeps them all).
AUDITED = 20


@dataclass
class ApplyReport:
    status: str  # applied | invalid | pending | failed | skipped
    policy_version: str
    gateway_version: int | None = None
    problems: list[str] = field(default_factory=list)


@dataclass
class _Outcome:
    kind: str  # applied | invalid | retry
    error: str = ""
    problems: list[str] = field(default_factory=list)
    gateway_version: int | None = None
    previous_version: int | None = None
    per_team: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


async def routing_job(engine, git, tenant_id: UUID, settings: Settings, clock) -> None:
    """The worker's per-tenant job (`EXTRA_JOBS`): nothing without a gateway admin."""
    gateway = gateway_routing_for(settings)
    if gateway.configured:
        await apply_due(engine, tenant_id, gateway, settings, clock)


async def plan_for(conn: AsyncConnection, version: str) -> RoutingPlan | None:
    """The plan stored with an accepted version, or generated again from its bundle (a
    version accepted before plans were stored, or of another generator format)."""
    row = (
        await conn.execute(
            select(policy_versions.c.settings, policy_versions.c.bundle).where(
                policy_versions.c.version == version, policy_versions.c.status == "accepted"
            )
        )
    ).first()
    if row is None:
        return None
    plan = RoutingPlan.from_settings((row.settings or {}).get("routing"))
    if plan is None and row.bundle is not None:
        plan = RoutingPlan.from_settings(routing_from_bundle(row.bundle, version))
    return plan


async def apply_due(
    engine: AsyncEngine,
    tenant_id: UUID,
    gateway: GatewayRouting,
    settings: Settings,
    clock: Callable[[], datetime],
) -> ApplyReport | None:
    """Apply the tenant's newest due routing row; None when nothing is due."""
    async with tenant_tx(engine, tenant_id) as conn:
        now = clock()
        row = (
            await conn.execute(
                select(routing_applies)
                .where(routing_applies.c.status == "pending")
                .where(routing_applies.c.next_attempt_at <= now)
                .order_by(routing_applies.c.created_at.desc())
                .limit(1)
                .with_for_update(skip_locked=True)
            )
        ).first()
        if row is None:
            return None
        tenant = await current_tenant(conn)
        if not tenant.gateway_tenant:
            await _finish(conn, row, "skipped", clock, values={}, reason="no gateway tenant")
            return ApplyReport("skipped", row.policy_version)
        plan = await plan_for(conn, row.policy_version)
        if plan is None:
            problem = "the policy version's routing plan cannot be read"
            await _finish(
                conn,
                row,
                "invalid",
                clock,
                values={"problems": [problem]},
                reason=problem,
                event="routing.refused",
            )
            return ApplyReport("invalid", row.policy_version, problems=[problem])
        owned = await _owned_per_team(conn)
        attempts = row.attempts + 1
        await conn.execute(
            update(routing_applies)
            .where(routing_applies.c.id == row.id)
            .values(attempts=attempts, next_attempt_at=now + LEASE)
        )
    comment = f"agentic policy {row.policy_version} for tenant {tenant.slug}"
    outcome = await _push(gateway, tenant.gateway_tenant, plan, owned, settings, comment)
    async with tenant_tx(engine, tenant_id) as conn:
        return await _record(conn, row, attempts, plan, outcome, settings, clock)


async def _owned_per_team(conn: AsyncConnection) -> list[str]:
    """The `rate_limit.per_team` entries the last applied version of this tenant wrote."""
    found = (
        await conn.execute(
            select(routing_applies.c.per_team)
            .where(routing_applies.c.status == "applied")
            .order_by(
                routing_applies.c.applied_at.desc(),
                routing_applies.c.gateway_version.desc().nulls_last(),
            )
            .limit(1)
        )
    ).first()
    return list(found.per_team) if found is not None else []


# ── the gateway (no transaction open) ────────────────────────────────────────


def compose(
    current: dict,
    gateway_tenant: str,
    plan: RoutingPlan,
    owned: list[str],
    *,
    team_rate_limits: bool,
) -> tuple[dict, list[str], list[str]]:
    """The overlay with this tenant's part replaced: (fragment, per_team written, warnings).
    Nothing outside `tenants.<gateway_tenant>` and this tenant's own `per_team` entries
    changes."""
    fragment = copy.deepcopy(current)
    warnings = list(plan.warnings)
    tenants = dict(fragment.get("tenants") or {})
    if plan.tenant:
        tenants[gateway_tenant] = copy.deepcopy(plan.tenant)
    else:
        tenants.pop(gateway_tenant, None)
    if tenants:
        fragment["tenants"] = tenants
    else:
        fragment.pop("tenants", None)
    written: list[str] = []
    if team_rate_limits or owned:
        rate_limit = dict(fragment.get("rate_limit") or {})
        per_team = dict(rate_limit.get("per_team") or {})
        for team in owned:
            per_team.pop(team, None)
        for team, limits in sorted(plan.per_team.items()) if team_rate_limits else []:
            if team in per_team:
                warnings.append(
                    f"team {team}'s request limit at the gateway was set by another writer "
                    "(another tenant's team of that name, or an operator); left as it is"
                )
                continue
            per_team[team] = dict(limits)
            written.append(team)
        if per_team:
            rate_limit["per_team"] = per_team
        else:
            rate_limit.pop("per_team", None)
        if rate_limit:
            fragment["rate_limit"] = rate_limit
        else:
            fragment.pop("rate_limit", None)
    if plan.per_team and not team_rate_limits:
        warnings.append(
            f"team rate limits for {', '.join(sorted(plan.per_team))} are not applied: the "
            "gateway keys them by team name across every tenant, and "
            "AGENTIC_CP_GATEWAY_TEAM_RATE_LIMITS is off"
        )
    return fragment, written, warnings


async def _push(
    gateway: GatewayRouting,
    gateway_tenant: str,
    plan: RoutingPlan,
    owned: list[str],
    settings: Settings,
    comment: str,
) -> _Outcome:
    try:
        for _ in range(CONFLICT_TRIES):
            current = await gateway.read()
            fragment, written, warnings = compose(
                current.fragment,
                gateway_tenant,
                plan,
                owned,
                team_rate_limits=settings.gateway_team_rate_limits,
            )
            if fragment == current.fragment:  # already there: no new gateway version
                return _Outcome(
                    "applied",
                    gateway_version=current.version,
                    previous_version=current.version,
                    per_team=written,
                    warnings=warnings,
                )
            try:
                stored = await gateway.write(
                    fragment, expected_version=current.version, comment=comment
                )
            except RoutingConflict:
                continue
            back = await gateway.read()
            if back.version != stored.version:  # someone wrote since: read ours by number
                back = await gateway.read_version(stored.version)
            if back is None or back.fragment != fragment:
                return _Outcome(
                    "retry",
                    error=f"the gateway's version {stored.version} does not read back as "
                    "what was written",
                )
            return _Outcome(
                "applied",
                gateway_version=stored.version,
                previous_version=current.version,
                per_team=written,
                warnings=warnings,
            )
        return _Outcome(
            "retry", error="the gateway's routing overlay kept changing while it was written"
        )
    except RoutingRefused as refused:
        return _Outcome("invalid", error=str(refused), problems=refused.errors)
    except GatewayUnavailable as error:
        return _Outcome("retry", error=str(error))


# ── recording the outcome ────────────────────────────────────────────────────


async def _finish(
    conn: AsyncConnection,
    row,
    status: str,
    clock: Callable[[], datetime],
    *,
    values: dict,
    reason: str = "",
    event: str | None = None,
    before: dict | None = None,
    after: dict | None = None,
    superseded: bool = False,
) -> None:
    keep_superseded = superseded and status != "applied"
    await conn.execute(
        update(routing_applies)
        .where(routing_applies.c.id == row.id)
        .values(**values, **({} if keep_superseded else {"status": status}))
    )
    name = event or f"routing.{status}"
    await append(
        conn,
        AuditEvent(
            event=name,
            actor=SYSTEM,
            actor_kind="system",
            target=f"routing:{row.id}",
            reason=reason[:300],
            before=before,
            after={"policy_version": row.policy_version, **(after or {})},
            policy_version=row.policy_version,
        ),
        clock=clock,
    )


async def _record(
    conn: AsyncConnection,
    row,
    attempts: int,
    plan: RoutingPlan,
    outcome: _Outcome,
    settings: Settings,
    clock: Callable[[], datetime],
) -> ApplyReport:
    now = clock()
    # a newer version may have superseded this row while the gateway was called
    status = (
        await conn.execute(select(routing_applies.c.status).where(routing_applies.c.id == row.id))
    ).scalar_one()
    superseded = status == "superseded"
    version = row.policy_version
    if outcome.kind == "applied":
        digest = plan_digest(plan)
        await _finish(
            conn,
            row,
            "applied",
            clock,
            values={
                "applied_at": now,
                "gateway_version": outcome.gateway_version,
                "previous_gateway_version": outcome.previous_version,
                "plan_sha256": digest,
                "per_team": outcome.per_team,
                "warnings": outcome.warnings,
                "last_error": None,
            },
            before={"gateway_version": outcome.previous_version},
            after={
                "gateway_version": outcome.gateway_version,
                "plan_sha256": digest,
                "per_team": outcome.per_team,
                "warnings": outcome.warnings[:AUDITED],
            },
        )
        log.info(
            "routing for policy %s applied as gateway version %s", version, outcome.gateway_version
        )
        return ApplyReport("applied", version, outcome.gateway_version)
    if outcome.kind == "invalid":
        await _finish(
            conn,
            row,
            "invalid",
            clock,
            values={"problems": outcome.problems, "last_error": outcome.error[:300]},
            reason=(outcome.problems or [outcome.error])[0],
            event="routing.refused",
            after={"problems": outcome.problems[:AUDITED]},
            superseded=superseded,
        )
        return ApplyReport("invalid", version, problems=outcome.problems)
    error = outcome.error[:300]
    log.warning("routing for policy %s not applied (attempt %d): %s", version, attempts, error)
    if attempts >= settings.routing_max_attempts:
        await _finish(
            conn,
            row,
            "failed",
            clock,
            values={"last_error": error},
            reason=error,
            event="routing.apply_failed",
            after={"attempts": attempts},
            superseded=superseded,
        )
        return ApplyReport("failed", version)
    await conn.execute(
        update(routing_applies)
        .where(routing_applies.c.id == row.id)
        .values(last_error=error, next_attempt_at=now + backoff(attempts))
    )
    return ApplyReport("pending", version)
