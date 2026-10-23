"""Budget threshold alerts (M2-16, ruling P5): 80 % and 100 % of a run's budget and of its
team's monthly budget.

Ingest adds each new `step_finished` cost to its run (a replayed entry is a duplicate and
never reaches here), so spend only grows and each threshold is crossed - and alerted -
once. Team spend is the sum over the team's runs opened in the current UTC month; checks of
one team serialise on a transaction advisory lock, so two ingests at once cannot both miss
(or both see) a crossing. Each crossing is audited (`budget.threshold_crossed`) and handed
to every `AlertSink` (the mail outbox, and the webhook seam)."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncConnection

from agentic_cp.audit import AuditEvent, append
from agentic_cp.ingest.register import version_settings
from agentic_cp.notify.outbox import SYSTEM
from agentic_cp.policy_sync.service import active_policy
from agentic_cp.tables import runs
from agentic_cp.tenancy.context import require_tenant_id

THRESHOLDS = (80, 100)


def crossed(before: Decimal, after: Decimal, budget: Decimal) -> list[int]:
    """The thresholds (percent of `budget`) that spend passed going from `before` to
    `after`."""
    if budget <= 0:
        return []
    return [t for t in THRESHOLDS if before * 100 < budget * t <= after * 100]


def _usd(value: object) -> Decimal | None:
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return amount if amount.is_finite() and amount >= 0 else None


def step_spend(entries: Iterable[Mapping]) -> Decimal:
    """The cost the `step_finished` events among `entries` report (`cost.usd`)."""
    total = Decimal(0)
    for entry in entries:
        cost = entry.get("cost") if entry.get("event") == "step_finished" else None
        amount = _usd(cost.get("usd")) if isinstance(cost, Mapping) else None
        total += amount or 0
    return total


@dataclass(frozen=True)
class BudgetAlert:
    scope: str  # run | team
    team: str
    threshold: int
    spent_usd: Decimal
    budget_usd: Decimal
    period: str  # "" for a run; YYYY-MM for a team
    run_id: str | None = None
    starter: object = None  # the run's starter (users.id), for a run alert

    @property
    def subject(self) -> str:
        return self.run_id if self.scope == "run" and self.run_id else self.team

    @property
    def what(self) -> str:
        return f"Run {self.run_id}" if self.scope == "run" else f"Team {self.team}"

    def dedupe_key(self, recipient: str) -> str:
        return f"budget:{self.scope}:{self.subject}:{self.period}:{self.threshold}:{recipient}"


class AlertSink:
    """Where alerts go: `deliver` runs in the ingest transaction (the outbox, or a webhook
    seam that only records what to send). Subclass or match the shape."""

    async def deliver(
        self, conn: AsyncConnection, alert: BudgetAlert, *, clock: Callable[[], datetime]
    ) -> None:
        raise NotImplementedError


async def _raise(conn, alert: BudgetAlert, sinks: Sequence, clock) -> None:
    await append(
        conn,
        AuditEvent(
            event="budget.threshold_crossed",
            actor=SYSTEM,
            actor_kind="system",
            target=f"run:{alert.run_id}" if alert.scope == "run" else f"team:{alert.team}",
            team=alert.team,
            run_id=alert.run_id,
            after={
                "scope": alert.scope,
                "threshold": alert.threshold,
                "spent_usd": str(alert.spent_usd),
                "budget_usd": str(alert.budget_usd),
                "period": alert.period,
            },
        ),
        clock=clock,
    )
    for sink in sinks:
        await sink.deliver(conn, alert, clock=clock)


async def check_run(
    conn: AsyncConnection, run, before: Decimal, after: Decimal, *, sinks: Sequence, clock
) -> None:
    """Alert each threshold of the run's budget its spend just passed."""
    if run.budget_usd is None:
        return
    budget = Decimal(run.budget_usd)
    for threshold in crossed(before, after, budget):
        alert = BudgetAlert(
            scope="run",
            team=run.team,
            threshold=threshold,
            spent_usd=after,
            budget_usd=budget,
            period="",
            run_id=run.run_id,
            starter=run.started_by,
        )
        await _raise(conn, alert, sinks, clock)


async def team_budget(conn: AsyncConnection, team: str) -> Decimal | None:
    """The team's monthly budget in the tenant's active policy, or None."""
    active = await active_policy(conn)
    if active is None:
        return None
    budgets = (await version_settings(conn, active.version) or {}).get("team_budgets") or {}
    return _usd(budgets.get(team)) if team in budgets else None


async def check_team(conn: AsyncConnection, run, added: Decimal, *, sinks: Sequence, clock) -> None:
    """Alert each threshold of the team's monthly budget this month's spend just passed."""
    if added <= 0:
        return
    budget = await team_budget(conn, run.team)
    if budget is None:
        return
    tenant_id = await require_tenant_id(conn)
    key = func.hashtextextended(f"agentic.team_spend:{tenant_id}:{run.team}", 0)
    await conn.execute(select(func.pg_advisory_xact_lock(key)))
    now = clock()
    month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if run.opened_at < month:
        return  # a run's spend counts for the month it opened in
    after = (
        await conn.execute(
            select(func.coalesce(func.sum(runs.c.spent_usd), 0)).where(
                runs.c.team == run.team, runs.c.opened_at >= month
            )
        )
    ).scalar_one()
    after = Decimal(after)
    for threshold in crossed(after - added, after, budget):
        alert = BudgetAlert(
            scope="team",
            team=run.team,
            threshold=threshold,
            spent_usd=after,
            budget_usd=budget,
            period=now.strftime("%Y-%m"),
        )
        await _raise(conn, alert, sinks, clock)
