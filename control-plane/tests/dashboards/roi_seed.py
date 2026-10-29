"""Seed data for the executive cost views (M3-01): two months of runs at fixed times, with
step loops, a registered run with no event and no call, untagged calls, a call of a run the
control plane never registered, cache counts on some calls and two models.

Expected totals are computed from the same data the seed writes (`RUNS`, `CALLS`), so a
reconciliation test never compares against a hand-typed number."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from dash_seed import GW_A, GW_B, POLICY, Call, _user, tags
from sqlalchemy.ext.asyncio import AsyncConnection

from agentic_cp.tables import run_events, runs
from agentic_cp.tenancy.context import tenant_tx

SEP, OCT = "2026-09", "2026-10"
LARGE, SMALL = "model-large", "model-small"
DESIGN, DEV, REVIEW = "design@1.0.0", "developer@1.2.0", "code-review@1.0.0"


def when(month: str, day: int, hour: int = 10) -> datetime:
    year, number = (int(part) for part in month.split("-"))
    return datetime(year, number, day, hour, tzinfo=UTC)


@dataclass(frozen=True)
class Run:
    """One registered run: who, which team and pipeline, when it opened, its step
    executions in order (a step named twice ran twice: a loop) and its gate waits (ms)."""

    run_id: str
    owner: str
    team: str
    pipeline: str | None
    opened: datetime
    steps: tuple[str, ...] = ()
    gate_waits: tuple[int | None, ...] = ()


RUNS_A: tuple[Run, ...] = (
    Run(
        "r1",
        "alice@a.test",
        "payments",
        "feature",
        when(SEP, 10),
        ("design", "develop", "develop", "review"),
        (3_600_000,),
    ),
    # registered without a pipeline: the pipeline comes from its calls' tag
    Run("r2", "bob@a.test", "search", None, when(SEP, 20), ("develop",)),
    Run(
        "r3",
        "alice@a.test",
        "payments",
        "feature",
        when(OCT, 5),
        ("design", "develop", "develop", "develop"),
        (1_800_000, None),
    ),
    Run("r4", "bob@a.test", "search", "bugfix", when(OCT, 12), ("develop",)),
    # registered, then nothing: no event, no call
    Run("r5", "cara@a.test", "payments", "feature", when(OCT, 20)),
)
RUNS_B: tuple[Run, ...] = (
    Run("b1", "carol@b.test", "platform", "feature", when(OCT, 3), ("develop",)),
)


def _call(run: str, step: str, agent: str, team: str, cost: str, at: datetime, **more) -> Call:
    extra = {
        k: more.pop(k) for k in ("model", "cache_read_tokens", "cache_write_tokens") if k in more
    }
    return Call(GW_A, cost, team, tags(run, step, agent, team, **more), when=at, **extra)


CALLS: tuple[Call, ...] = (
    _call("r1", "design", DESIGN, "payments", "0.120000", when(SEP, 10, 11), model=LARGE),
    _call("r1", "design", DESIGN, "payments", "0.080000", when(SEP, 10, 11), model=LARGE),
    _call(
        "r1",
        "develop",
        DEV,
        "payments",
        "0.300000",
        when(SEP, 10, 12),
        model=LARGE,
        cache_read_tokens=4000,
        cache_write_tokens=500,
    ),
    _call("r1", "develop", DEV, "payments", "0.250000", when(SEP, 10, 12), model=LARGE),
    _call("r1", "develop", DEV, "payments", "0.200000", when(SEP, 10, 13), model=LARGE),
    _call("r1", "review", REVIEW, "payments", "0.010000", when(SEP, 10, 14), model=SMALL),
    _call(
        "r2",
        "develop",
        DEV,
        "search",
        "0.050000",
        when(SEP, 20, 11),
        model=SMALL,
        pipeline="bugfix",
    ),
    _call("r2", "develop", DEV, "search", "0.040000", when(SEP, 20, 12), model=SMALL),
    _call("r3", "design", DESIGN, "payments", "0.110000", when(OCT, 5, 11), model=LARGE),
    _call(
        "r3",
        "develop",
        DEV,
        "payments",
        "0.400000",
        when(OCT, 5, 12),
        model=LARGE,
        cache_read_tokens=8000,
        cache_write_tokens=0,
    ),
    _call("r3", "develop", DEV, "payments", "0.350000", when(OCT, 5, 12), model=LARGE),
    _call("r3", "develop", DEV, "payments", "0.300000", when(OCT, 5, 13), model=SMALL),
    # a call of r3 that carried no step tag
    Call(GW_A, "0.020000", "payments", {"run_id": "r3", "team": "payments"}, when=when(OCT, 5, 14)),
    _call("r4", "develop", DEV, "search", "0.060000", when(OCT, 12, 11), model=SMALL),
    # untagged calls: one with a team, one without
    Call(GW_A, "0.300000", "payments", None, when=when(OCT, 15), model=LARGE),
    Call(GW_A, "0.020000", "", None, when=when(SEP, 15), model=SMALL),
    # tagged with a run the control plane never registered (a standalone run)
    _call("local-x", "develop", DEV, "payments", "0.050000", when(OCT, 16), model=SMALL),
    Call(
        GW_B, "2.000000", "platform", tags("b1", "develop", DEV, "platform"), when=when(OCT, 3, 11)
    ),
)

REGISTERED = {run.run_id: run for run in RUNS_A}


def a_calls() -> list[Call]:
    return [call for call in CALLS if call.gateway == GW_A]


def run_of(call: Call) -> Run | None:
    tag = (call.tags or {}).get("run_id")
    return REGISTERED.get(tag) if tag else None


def month_of(call: Call) -> str:
    run = run_of(call)
    moment = run.opened if run else call.when
    return moment.strftime("%Y-%m")


def cost(calls) -> Decimal:
    return sum((Decimal(call.cost) for call in calls), Decimal(0))


def attributed(month: str | None = None) -> list[Call]:
    return [c for c in a_calls() if run_of(c) and (month is None or month_of(c) == month)]


def unattributed(month: str | None = None) -> list[Call]:
    return [c for c in a_calls() if not run_of(c) and (month is None or month_of(c) == month)]


async def _chain(conn: AsyncConnection, tenant: UUID, run: Run) -> None:
    seq = 0

    async def add(event: str, step: str = "", **fields) -> None:
        nonlocal seq
        seq += 1
        moment = run.opened.replace(minute=seq % 60).isoformat(timespec="milliseconds")
        entry = {
            "seq": seq,
            "run_id": run.run_id,
            "event": event,
            "step": step,
            "at": moment,
            "ext": {"agentic": {"team": run.team, "policy_version": POLICY}},
            **fields,
        }
        await conn.execute(
            run_events.insert().values(
                tenant_id=tenant,
                run_id=run.run_id,
                seq=seq,
                hash=f"sha256:{seq:064x}",
                prev="" if seq == 1 else f"sha256:{seq - 1:064x}",
                event=event,
                step=step,
                at=moment,
                entry=json.loads(json.dumps(entry)),
            )
        )

    if not run.steps:
        return
    await add("run_opened", kind="feature", project="app", owner=run.team, criticality="low")
    for attempt, step in enumerate(run.steps, 1):
        await add("step_started", step, attempt=attempt)
        await add("step_finished", step, outcome="completed")
    for waited in run.gate_waits:
        await add("gate_waiting", run.steps[-1], gate="review-approval", hard=True)
        await add(
            "gate_decided",
            run.steps[-1],
            gate="review-approval",
            decision="approve",
            approver="ravi@a.test",
            forced=False,
            waited_ms=waited,
        )


async def _seed_tenant(engine, tenant: UUID, seeded_runs: tuple[Run, ...]) -> None:
    async with tenant_tx(engine, tenant) as conn:
        people: dict[str, UUID] = {}
        for run in seeded_runs:
            if run.owner not in people:
                people[run.owner] = await _user(conn, tenant, run.owner)
            await conn.execute(
                runs.insert().values(
                    tenant_id=tenant,
                    run_id=run.run_id,
                    team=run.team,
                    started_by=people[run.owner],
                    policy_version=POLICY,
                    policy_known=True,
                    head_seq=0,
                    opened_at=run.opened,
                    pipeline=run.pipeline,
                )
            )
            await _chain(conn, tenant, run)


async def seed_roi(engine, a: UUID, b: UUID) -> None:
    """Runs, step and gate events of `RUNS_A` in a and `RUNS_B` in b."""
    await _seed_tenant(engine, a, RUNS_A)
    await _seed_tenant(engine, b, RUNS_B)
