"""Seed data for the dashboard views (M2-37, M2-38): two tenants with runs, step and gate
events, audit records, repos and kill switches, and a gateway database holding the
gateway's `usage_events` and `audit_events` for both tenants and for a third gateway tenant
that belongs to nobody.

The expected sums are computed from the same data the seed writes (`CALLS`, `STEPS`), so a
reconciliation test never compares against a hand-typed total."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import asyncpg
from sqlalchemy.ext.asyncio import AsyncConnection

from agentic_cp.audit import AuditEvent, append
from agentic_cp.tables import kill_switches, repos, run_events, runs, users
from agentic_cp.tenancy.context import tenant_tx
from agentic_cp.tenancy.tenants import create_tenant

UNATTRIBUTED = "(unattributed)"
NOW = datetime.now(UTC).replace(microsecond=0)
POLICY = "git:" + "c" * 40


def clock() -> datetime:
    return NOW


def at(minutes_ago: float) -> datetime:
    return NOW - timedelta(minutes=minutes_ago)


def iso(moment: datetime) -> str:
    """The ledger's time format (Zyroflo: milliseconds, explicit offset)."""
    return moment.isoformat(timespec="milliseconds")


@dataclass(frozen=True)
class Call:
    """One gateway call: `tags` as the gateway stored them (None: the call carried none)."""

    gateway: str
    cost: str
    team: str
    tags: dict[str, str] | None
    minutes_ago: float = 60
    input_tokens: int | None = 1000
    output_tokens: int = 200
    status: int = 200
    request_id: str = field(default_factory=lambda: f"req-{uuid.uuid4().hex[:16]}")
    #: M3-01: a fixed time (else `minutes_ago`), the model, and the cache counts (None: the
    #: gateway did not record them)
    when: datetime | None = None
    model: str = "model-x"
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None


def tags(run: str, step: str, agent: str, team: str, **more: str) -> dict[str, str]:
    return {"run_id": run, "step": step, "agent": agent, "team": team, **more}


GW_A, GW_B, GW_NOBODY = "GW-A", "GW-B", "GW-NOBODY"
DEV, DESIGN, REVIEW = "developer@1.2.0", "design@1.0.0", "code-review@1.0.0"

#: Every gateway call. Tenant a: runs a-r1 (three steps, develop twice), a-r2, a tagged run
#: nobody registered, untagged calls (one without a team); tenant b and a stranger gateway
#: tenant, which a must never count.
CALLS: tuple[Call, ...] = (
    Call(GW_A, "0.040000", "payments", tags("a-r1", "design", DESIGN, "payments", pipeline="fd")),
    Call(GW_A, "0.060000", "payments", tags("a-r1", "design", DESIGN, "payments", repo="app")),
    Call(GW_A, "0.150000", "payments", tags("a-r1", "develop", DEV, "payments")),
    Call(GW_A, "0.200000", "payments", tags("a-r1", "develop", DEV, "payments")),
    # the caller asserts team "search"; the authenticated team is payments (ST-1 wins)
    Call(GW_A, "0.160000", "payments", tags("a-r1", "develop", DEV, "search")),
    Call(GW_A, "0.070000", "payments", tags("a-r1", "review", REVIEW, "payments"), status=429),
    Call(GW_A, "0.050000", "search", tags("a-r2", "develop", DEV, "search", user="bob@a.test")),
    Call(GW_A, "0.011000", "payments", tags("local-only-1", "develop", DEV, "payments")),
    Call(GW_A, "0.500000", "payments", None, input_tokens=None),
    Call(GW_A, "0.250000", "", None, minutes_ago=120),
    Call(GW_B, "9.990000", "platform", tags("b-r1", "develop", DEV, "platform")),
    Call(GW_NOBODY, "7.770000", "payments", tags("a-r1", "develop", DEV, "payments")),
)


@dataclass(frozen=True)
class Step:
    """One finished step execution the engine reported (`cost` None: no cost recorded).
    `agent` is the agent its gateway calls were tagged with: the engine records no agent
    on step events today (it passes no agent identity to the ledger), so the seed writes
    none either and the views take it from the step's calls."""

    run: str
    step: str
    agent: str
    outcome: str
    cost: str | None


STEPS_A: tuple[Step, ...] = (
    Step("a-r1", "design", DESIGN, "completed", "0.10"),
    Step("a-r1", "develop", DEV, "invalid", "0.20"),
    Step("a-r1", "develop", DEV, "completed", "0.30"),
    Step("a-r1", "review", REVIEW, "completed", None),
    Step("a-r2", "develop", DEV, "budget", "0.05"),
)
STEPS_B: tuple[Step, ...] = (Step("b-r1", "develop", DEV, "completed", "9.00"),)

#: Gate decisions on a-r1's review step: rejected once, then approved.
GATES_A = (
    ("review-approval", "review", "reject", 4000),
    ("review-approval", "review", "approve", None),
)


def calls_of(gateway: str) -> list[Call]:
    return [call for call in CALLS if call.gateway == gateway]


def total(calls) -> Decimal:
    return sum((Decimal(call.cost) for call in calls), Decimal(0))


@dataclass
class Seeded:
    """What the seed made: tenant ids and the gateway database's DSN."""

    a: UUID
    b: UUID
    gateway_dsn: str | None = None


# ── framework side ────────────────────────────────────────────────────────────


async def _user(conn: AsyncConnection, tenant: UUID, email: str) -> UUID:
    user_id = uuid.uuid4()
    await conn.execute(
        users.insert().values(tenant_id=tenant, id=user_id, email=email, kind="human")
    )
    return user_id


class _Chain:
    """Run events for one run, with consecutive seqs (hashes are placeholders: the views
    read the events, the chain is verified on ingest, not here)."""

    def __init__(self, conn: AsyncConnection, tenant: UUID, run_id: str, team: str) -> None:
        self.conn, self.tenant, self.run_id, self.team = conn, tenant, run_id, team
        self.seq = 0
        self.minutes = 300.0

    async def add(self, event: str, step: str = "", **fields) -> None:
        self.seq += 1
        self.minutes -= 5
        block = {"team": self.team, "policy_version": POLICY}
        entry = {
            "seq": self.seq,
            "run_id": self.run_id,
            "event": event,
            "step": step,
            "at": iso(at(self.minutes)),
            "ext": {"agentic": block},
            **fields,
        }
        await self.conn.execute(
            run_events.insert().values(
                tenant_id=self.tenant,
                run_id=self.run_id,
                seq=self.seq,
                hash=f"sha256:{self.seq:064x}",
                prev="" if self.seq == 1 else f"sha256:{self.seq - 1:064x}",
                event=event,
                step=step,
                at=entry["at"],
                entry=json.loads(json.dumps(entry)),
            )
        )


async def _run(conn, tenant: UUID, run_id: str, team: str, starter: UUID) -> _Chain:
    await conn.execute(
        runs.insert().values(
            tenant_id=tenant,
            run_id=run_id,
            team=team,
            started_by=starter,
            policy_version=POLICY,
            policy_known=True,
            head_seq=0,
            opened_at=at(300),
        )
    )
    chain = _Chain(conn, tenant, run_id, team)
    await chain.add("run_opened", kind="feature", project="app", owner=team, criticality="low")
    return chain


async def _steps(chains: dict[str, _Chain], steps: tuple[Step, ...]) -> None:
    for step in steps:
        chain = chains[step.run]
        await chain.add("step_started", step.step, attempt=1)
        extra = {"cost": {"usd": step.cost}} if step.cost is not None else {}
        await chain.add("step_finished", step.step, outcome=step.outcome, **extra)


async def seed_framework(engine, a: UUID, b: UUID) -> None:
    """Runs, step and gate events, audit records, repos and kill switches in a and b."""
    async with tenant_tx(engine, a) as conn:
        alice = await _user(conn, a, "alice@a.test")
        bob = await _user(conn, a, "bob@a.test")
        chains = {
            "a-r1": await _run(conn, a, "a-r1", "payments", alice),
            "a-r2": await _run(conn, a, "a-r2", "search", bob),
        }
        await _run(conn, a, "a-r3", "payments", alice)  # registered, nothing else
        await _steps(chains, STEPS_A)
        r1 = chains["a-r1"]
        for gate, step, decision, waited in GATES_A:
            await r1.add("gate_waiting", step, gate=gate, hard=True)
            await r1.add(
                "gate_decided",
                step,
                gate=gate,
                decision=decision,
                approver="ravi@a.test",
                forced=False,
                waited_ms=waited,
            )
        await r1.add("run_closed", status="completed")
        await conn.execute(
            repos.insert().values(
                tenant_id=a,
                repo_key="git.example.test/a/app",
                remote="https://git.example.test/a/app.git",
                team="payments",
                registered_by="alice@a.test",
                created_at=at(400),
            )
        )
        await conn.execute(
            kill_switches.insert().values(
                tenant_id=a,
                scope_kind="team",
                scope_value="payments",
                team="payments",
                mode="pause",
                reason="incident a",
                created_by="lead@a.test",
                created_at=at(200),
                cleared_by="lead@a.test",
                cleared_at=at(100),
            )
        )
        await append(
            conn,
            AuditEvent(
                event="role.assigned",
                actor="alice@a.test",
                actor_kind="human",
                team="payments",
                target="user:bob@a.test",
            ),
            clock=clock,
        )
    async with tenant_tx(engine, b) as conn:
        carol = await _user(conn, b, "carol@b.test")
        chains = {"b-r1": await _run(conn, b, "b-r1", "platform", carol)}
        await _steps(chains, STEPS_B)
        await chains["b-r1"].add("gate_waiting", "develop", gate="b-gate", hard=True)
        await conn.execute(
            repos.insert().values(
                tenant_id=b,
                repo_key="git.example.test/b/app",
                remote="https://git.example.test/b/app.git",
                team="platform",
                registered_by="carol@b.test",
                created_at=at(400),
            )
        )
        await conn.execute(
            kill_switches.insert().values(
                tenant_id=b,
                scope_kind="tenant",
                scope_value="",
                mode="kill",
                reason="incident b",
                created_by="carol@b.test",
                created_at=at(150),
            )
        )
        await append(
            conn,
            AuditEvent(
                event="role.assigned",
                actor="carol@b.test",
                actor_kind="human",
                team="platform",
                target="user:carol@b.test",
            ),
            clock=clock,
        )


async def make_tenants(engine) -> tuple[UUID, UUID]:
    async with engine.begin() as conn:
        a = await create_tenant(conn, "tenant-a", "Tenant A", gateway_tenant=GW_A)
        b = await create_tenant(conn, "tenant-b", "Tenant B", gateway_tenant=GW_B)
    return a.id, b.id


# ── gateway side ──────────────────────────────────────────────────────────────

#: The gateway's tables, with the columns the contract views read (types as the gateway
#: declares them) and a few the views ignore.
GATEWAY_DDL = (
    "CREATE TABLE usage_events ("
    " id BIGSERIAL PRIMARY KEY, tenant_id TEXT NOT NULL, request_id TEXT NOT NULL UNIQUE,"
    " timestamp TIMESTAMPTZ NOT NULL DEFAULT now(), baseline_tokens INT NOT NULL DEFAULT 0,"
    " model TEXT NOT NULL DEFAULT '', routed_model TEXT NOT NULL DEFAULT '',"
    " provider TEXT NOT NULL DEFAULT '', team TEXT NOT NULL DEFAULT '', tags JSONB,"
    " provider_prompt_tokens INTEGER, response_tokens INTEGER NOT NULL DEFAULT 0,"
    " cache_read_tokens INTEGER, cache_write_tokens INTEGER,"
    " cost_actual_usd NUMERIC(12,6) NOT NULL DEFAULT 0, status_code SMALLINT NOT NULL DEFAULT 0,"
    " total_duration_ms INTEGER NOT NULL DEFAULT 0)",
    "CREATE TABLE audit_events ("
    " id BIGSERIAL PRIMARY KEY, tenant_id TEXT NOT NULL, request_id TEXT NOT NULL,"
    " timestamp TIMESTAMPTZ NOT NULL DEFAULT now(), action TEXT NOT NULL DEFAULT 'proxy_request',"
    " user_id TEXT, details JSONB)",
)

#: Gateway audit rows: (gateway tenant, action). Only the safety actions reach the views.
GATEWAY_AUDIT = (
    (GW_A, "guardrail.flagged"),
    (GW_A, "redaction.applied"),
    (GW_A, "proxy_request"),
    (GW_B, "guardrail.blocked"),
)


async def fill_gateway(dsn: str, ddl=GATEWAY_DDL, calls=CALLS) -> None:
    """The gateway's tables (`ddl`) with `calls` and, when `ddl` makes `audit_events`, the
    gateway audit rows."""
    conn = await asyncpg.connect(dsn)
    try:
        for statement in ddl:
            await conn.execute(statement)
        for call in calls:
            await conn.execute(
                "INSERT INTO usage_events (tenant_id, request_id, timestamp, model, provider,"
                " team, tags, provider_prompt_tokens, response_tokens, cost_actual_usd,"
                " status_code, total_duration_ms) VALUES ($1, $2, $3, $10, 'provider-x',"
                " $4, $5::jsonb, $6, $7, $8::numeric, $9, 1500)",
                call.gateway,
                call.request_id,
                call.when or at(call.minutes_ago),
                call.team,
                json.dumps(call.tags) if call.tags is not None else None,
                call.input_tokens,
                call.output_tokens,
                call.cost,
                call.status,
                call.model,
            )
            if call.cache_read_tokens is not None or call.cache_write_tokens is not None:
                await conn.execute(
                    "UPDATE usage_events SET cache_read_tokens = $2, cache_write_tokens = $3"
                    " WHERE request_id = $1",
                    call.request_id,
                    call.cache_read_tokens,
                    call.cache_write_tokens,
                )
        audited = any("audit_events" in statement for statement in ddl)
        for n, (gateway, action) in enumerate(GATEWAY_AUDIT if audited else ()):
            await conn.execute(
                "INSERT INTO audit_events (tenant_id, request_id, timestamp, action, details)"
                ' VALUES ($1, $2, $3, $4, \'{"categories": ["injection"]}\'::jsonb)',
                gateway,
                f"audit-{n}",
                at(30 + n),
                action,
            )
    finally:
        await conn.close()
