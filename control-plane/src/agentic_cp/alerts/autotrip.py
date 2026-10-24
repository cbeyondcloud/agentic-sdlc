"""Auto-trip (M2-11; spec §8.6): pause a team whose spend rate or error rate passes the
threshold in its tenant's active policy (`org.yaml: auto_trip`, tightened per team).

A worker job, once per pass and tenant, inside that tenant's context:

- **Measure** each team over the policy's window from the run events received: the spend
  the `step_finished` events report (`cost.usd`) per hour, and the share of steps that ran
  (not skipped or paused) that ended in `error`. An event counts by its own time, so a
  laptop uploading yesterday's events does not look like a spike. No reported cost means
  the spend is unmeasured (None, never 0), and fewer than `min_steps` steps leave the
  error rate unmeasured; unmeasured never trips.
- **Decide** per team and kind (`spend`, `errors`) with the state in `auto_trips`:
  `armed -> tripped` above the threshold; a tripped team stays tripped while its pause
  switch holds. With `auto_clear` the switch is cleared once the rate is below
  `threshold x reset_ratio` and `hold_minutes` have passed. When a person clears the switch
  first, the state is `disarmed` until the rate is below that reset level, then `armed`
  again: the gap between the threshold and the reset level is the hysteresis that keeps a
  rate hovering around the threshold from tripping again and again.
- **Act** in the same transaction: a trip sets a team pause switch (`created_by`
  `system:auto-trip`, `killswitch.set` audited), mails the people holding
  `killswitch.team` in the team (the outbox) and appends `autotrip.tripped`; every other
  state change appends its own record (`autotrip.cleared`, `.disarmed`, `.rearmed`)."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from uuid import UUID

from sqlalchemy import and_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from agentic_cp.alerts.sinks import people_holding
from agentic_cp.audit import AuditEvent, append
from agentic_cp.ingest.register import version_settings
from agentic_cp.ingest.store import event_time
from agentic_cp.killswitch import store
from agentic_cp.killswitch.store import AUTO_TRIP_ACTOR
from agentic_cp.notify.outbox import enqueue
from agentic_cp.notify.templates import render
from agentic_cp.policy_sync.service import active_policy
from agentic_cp.tables import auto_trips, kill_switches, run_events, runs
from agentic_cp.tenancy.context import require_tenant_id, tenant_tx
from agentic_cp.tenancy.tenants import current_tenant

KINDS = ("spend", "errors")
#: Step outcomes that never ran a model call: not counted in the error rate.
NOT_RUN = frozenset({"skipped", "paused"})
_CENT = Decimal("0.01")
_RATE = Decimal("0.0001")


def _decimal(value: object) -> Decimal | None:
    if value is None:
        return None
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return amount if amount.is_finite() and amount >= 0 else None


@dataclass(frozen=True)
class AutoTripSettings:
    """One team's thresholds and the org's measuring and reset rules."""

    spend: Decimal | None
    errors: Decimal | None
    window: timedelta
    min_steps: int
    reset_ratio: Decimal
    hold: timedelta
    auto_clear: bool

    @classmethod
    def for_team(cls, stored: Mapping | None, team: str) -> AutoTripSettings | None:
        """From the accepted version's `run_settings["auto_trip"]`; None when absent."""
        if not stored:
            return None
        own = (stored.get("teams") or {}).get(team) or {}
        return cls(
            spend=_decimal(own.get("spend_usd_per_hour", stored.get("spend_usd_per_hour"))),
            errors=_decimal(own.get("error_rate", stored.get("error_rate"))),
            window=timedelta(minutes=int(stored.get("window_minutes", 60))),
            min_steps=int(stored.get("min_steps", 20)),
            reset_ratio=_decimal(stored.get("reset_ratio")) or Decimal("0.5"),
            hold=timedelta(minutes=int(stored.get("hold_minutes", 30))),
            auto_clear=bool(stored.get("auto_clear", False)),
        )

    def threshold(self, kind: str) -> Decimal | None:
        return self.spend if kind == "spend" else self.errors


@dataclass(frozen=True)
class Sample:
    """One received `step_finished` event: its team, own time, outcome and cost (text)."""

    team: str
    at: datetime | None
    outcome: str | None
    usd: str | None


@dataclass(frozen=True)
class Measure:
    """A team's rates over the window; None is unmeasured."""

    spend_per_hour: Decimal | None
    error_rate: Decimal | None
    steps: int

    def value(self, kind: str) -> Decimal | None:
        return self.spend_per_hour if kind == "spend" else self.error_rate


UNMEASURED = Measure(spend_per_hour=None, error_rate=None, steps=0)


def measure_samples(
    samples: Iterable[Sample], *, since: datetime, window: timedelta, min_steps: int
) -> dict[str, Measure]:
    """Each team's spend per hour and error rate from the samples whose own time is at or
    after `since`."""
    spend: dict[str, Decimal] = {}
    ran: dict[str, int] = {}
    errors: dict[str, int] = {}
    teams: set[str] = set()
    for sample in samples:
        if sample.at is None or sample.at < since:
            continue
        teams.add(sample.team)
        amount = _decimal(sample.usd)
        if amount is not None:
            spend[sample.team] = spend.get(sample.team, Decimal(0)) + amount
        if sample.outcome not in NOT_RUN:
            ran[sample.team] = ran.get(sample.team, 0) + 1
            if sample.outcome == "error":
                errors[sample.team] = errors.get(sample.team, 0) + 1
    hours = Decimal(int(window.total_seconds())) / Decimal(3600)
    found = {}
    for team in teams:
        steps = ran.get(team, 0)
        found[team] = Measure(
            spend_per_hour=spend[team] / hours if team in spend else None,
            error_rate=(
                Decimal(errors.get(team, 0)) / Decimal(steps) if steps >= min_steps else None
            ),
            steps=steps,
        )
    return found


async def measure(
    conn: AsyncConnection, *, since: datetime, window: timedelta, min_steps: int
) -> dict[str, Measure]:
    """`measure_samples` over the `step_finished` events received since `since` in the
    transaction's tenant (an event received then happened then or earlier)."""
    rows = await conn.execute(
        select(
            runs.c.team,
            run_events.c.entry["at"].astext,
            run_events.c.entry["outcome"].astext,
            run_events.c.entry[("cost", "usd")].astext,
        )
        .join(
            runs,
            and_(runs.c.tenant_id == run_events.c.tenant_id, runs.c.run_id == run_events.c.run_id),
        )
        .where(run_events.c.event == "step_finished", run_events.c.received_at >= since)
    )
    samples = (
        Sample(team=team, at=event_time(at), outcome=outcome, usd=usd)
        for team, at, outcome, usd in rows
    )
    return measure_samples(samples, since=since, window=window, min_steps=min_steps)


@dataclass(frozen=True)
class Step:
    action: str | None  # trip | clear | disarm | rearm | None
    state: str


def decide(
    state: str,
    value: Decimal | None,
    threshold: Decimal | None,
    reset_ratio: Decimal,
    *,
    switch_active: bool,
    tripped_at: datetime | None,
    now: datetime,
    hold: timedelta,
    auto_clear: bool,
) -> Step:
    """The next state of one team and kind. Unmeasured (None) never trips and counts as
    calm; so does a threshold the policy no longer sets."""
    calm = value is None or threshold is None or value < threshold * reset_ratio
    if state == "armed":
        if value is not None and threshold is not None and value > threshold:
            return Step("trip", "tripped")
        return Step(None, "armed")
    if state == "tripped":
        if not switch_active:  # a person cleared it
            return Step("rearm", "armed") if calm else Step("disarm", "disarmed")
        held = tripped_at is None or now - tripped_at >= hold
        if auto_clear and calm and held:
            return Step("clear", "armed")
        return Step(None, "tripped")
    if calm:  # disarmed
        return Step("rearm", "armed")
    return Step(None, state)


def _shown(kind: str, value: Decimal | None) -> str:
    if value is None:
        return "unmeasured"
    if kind == "spend":
        return f"{value.quantize(_CENT, ROUND_HALF_UP)} USD per hour"
    return f"{(value * 100).quantize(Decimal(1), ROUND_HALF_UP)}%"


def _stored(kind: str, value: Decimal | None) -> str | None:
    """Decimal text for `auto_trips` and the audit record (never a float)."""
    if value is None:
        return None
    if kind == "spend":
        return str(value.quantize(_CENT, ROUND_HALF_UP))
    rate = value.quantize(_RATE, ROUND_HALF_UP).normalize()
    return format(rate, "f")


def _what(kind: str) -> str:
    return "spend rate" if kind == "spend" else "error rate"


def _reason(kind: str, value: Decimal, threshold: Decimal, cfg: AutoTripSettings, steps: int):
    minutes = int(cfg.window.total_seconds() // 60)
    measured = _shown(kind, value) + (f" of {steps} steps" if kind == "errors" else "")
    return (
        f"auto-trip: {_what(kind)} {measured} over the {_shown(kind, threshold)} threshold "
        f"in the last {minutes} minutes"
    )


def _texts(kind: str, value: Decimal | None, threshold: Decimal | None) -> dict:
    """The measured value (rounded) and the threshold (as the policy wrote it), as text."""
    return {
        "value": _stored(kind, value),
        "threshold": None if threshold is None else format(threshold, "f"),
    }


async def _save(conn, team: str, kind: str, state: str, switch_id, value, threshold, now):
    values = {
        "state": state,
        "switch_id": switch_id,
        **_texts(kind, value, threshold),
        "changed_at": now,
    }
    tenant_id = await require_tenant_id(conn)
    await conn.execute(
        insert(auto_trips)
        .values(tenant_id=tenant_id, team=team, kind=kind, **values)
        .on_conflict_do_update(index_elements=["tenant_id", "team", "kind"], set_=values)
    )


async def _audit(conn, event: str, team: str, kind: str, clock, *, reason="", **after) -> None:
    await append(
        conn,
        AuditEvent(
            event=event,
            actor=AUTO_TRIP_ACTOR,
            actor_kind="system",
            target=f"team:{team}",
            team=team,
            reason=reason,
            after={"kind": kind, **after},
        ),
        clock=clock,
    )


async def _mail(conn, team: str, kind: str, switch_id: UUID, value, threshold, cfg, clock):
    recipients = await people_holding(conn, team, permission="killswitch.team")
    if not recipients:
        return
    tenant = await current_tenant(conn)
    reset = threshold * cfg.reset_ratio
    rendered = render(
        "auto_trip",
        {
            "team": team,
            "what": _what(kind),
            "value": _shown(kind, value),
            "threshold": _shown(kind, threshold),
            "window_minutes": str(int(cfg.window.total_seconds() // 60)),
            "reset": _shown(kind, reset),
            "clear_phrase": (
                f"the {_what(kind)} has been below {_shown(kind, reset)} for "
                f"{int(cfg.hold.total_seconds() // 60)} minutes after the trip"
                if cfg.auto_clear
                else "a person clears it"
            ),
        },
        tenant_name=tenant.name,
    )
    for email in recipients:
        await enqueue(
            conn,
            kind="auto_trip",
            recipient=email,
            rendered=rendered,
            dedupe_key=f"autotrip:{team}:{kind}:{switch_id}:{email}",
            actor=AUTO_TRIP_ACTOR,
            clock=clock,
            team=team,
        )


async def _switch_active(conn, switch_id) -> bool:
    if switch_id is None:
        return False
    found = await conn.execute(
        select(kill_switches.c.id)
        .where(kill_switches.c.id == switch_id, kill_switches.c.cleared_at.is_(None))
        .with_for_update()
    )
    return found.first() is not None


async def evaluate_tenant(conn: AsyncConnection, clock: Callable[[], datetime]) -> list[str]:
    """One auto-trip pass over the transaction's tenant; the audit events it appended (one
    per state change, in team and kind order)."""
    active = await active_policy(conn)
    if active is None:
        return []
    stored = (await version_settings(conn, active.version) or {}).get("auto_trip")
    org = AutoTripSettings.for_team(stored, "")
    if org is None:
        return []
    now = clock()
    since = now - org.window
    measures = await measure(conn, since=since, window=org.window, min_steps=org.min_steps)
    rows = {(row.team, row.kind): row for row in await conn.execute(select(auto_trips))}
    events: list[str] = []
    for team in sorted(set(measures) | {team for team, _ in rows}):
        cfg = AutoTripSettings.for_team(stored, team)
        found = measures.get(team, UNMEASURED)
        for kind in KINDS:
            row = rows.get((team, kind))
            state = row.state if row is not None else "armed"
            threshold, value = cfg.threshold(kind), found.value(kind)
            if state == "armed" and (threshold is None or value is None):
                continue
            step = decide(
                state,
                value,
                threshold,
                cfg.reset_ratio,
                switch_active=await _switch_active(conn, row.switch_id if row else None),
                tripped_at=row.changed_at if row is not None else None,
                now=now,
                hold=cfg.hold,
                auto_clear=cfg.auto_clear,
            )
            if step.action is None:
                continue
            events.append(
                await _apply(conn, step, team, kind, row, value, threshold, cfg, found, clock)
            )
    return events


async def _apply(conn, step: Step, team, kind, row, value, threshold, cfg, found, clock) -> str:
    now = clock()
    shown = _texts(kind, value, threshold)
    if step.action == "trip":
        switch = await store.insert_switch(
            conn,
            scope_kind="team",
            scope_value=team,
            team=team,
            mode="pause",
            reason=_reason(kind, value, threshold, cfg, found.steps),
            actor=AUTO_TRIP_ACTOR,
            actor_kind="system",
            clock=clock,
        )
        await _mail(conn, team, kind, switch.id, value, threshold, cfg, clock)
        await _audit(
            conn,
            "autotrip.tripped",
            team,
            kind,
            clock,
            reason=switch.reason,
            switch=str(switch.id),
            window_minutes=int(cfg.window.total_seconds() // 60),
            **shown,
        )
        await _save(conn, team, kind, "tripped", switch.id, value, threshold, now)
        return "autotrip.tripped"
    if step.action == "clear":
        await store.clear_switch(
            conn,
            row.switch_id,
            reason=f"auto-trip: {_what(kind)} back below the reset level",
            actor=AUTO_TRIP_ACTOR,
            actor_kind="system",
            clock=clock,
        )
        await _audit(
            conn, "autotrip.cleared", team, kind, clock, switch=str(row.switch_id), **shown
        )
        await _save(conn, team, kind, "armed", None, value, threshold, now)
        return "autotrip.cleared"
    event = "autotrip.disarmed" if step.action == "disarm" else "autotrip.rearmed"
    reason = (
        "the pause switch was cleared while the rate is above the reset level"
        if step.action == "disarm"
        else "the rate is below the reset level"
    )
    await _audit(conn, event, team, kind, clock, reason=reason, **shown)
    await _save(conn, team, kind, step.state, None, value, threshold, now)
    return event


async def auto_trip_job(
    engine: AsyncEngine, git, tenant_id: UUID, settings, clock: Callable[[], datetime]
) -> list[str]:
    """The worker's per-tenant job (`policy_sync.worker.EXTRA_JOBS`)."""
    async with tenant_tx(engine, tenant_id) as conn:
        return await evaluate_tenant(conn, clock)
