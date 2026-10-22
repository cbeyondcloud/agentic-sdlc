"""The per-tenant audit chain (M2-05, spec §14.5).

Each record carries the spec's fields and `hash = entry_hash(record minus hash)`, the run
ledger's formula through `sdlc_commons.ledger`, so one verifier (M2-26) checks both chains.
Every tenant has its own chain starting at seq 1. Appends serialise on a per-tenant
transaction advisory lock, and `PRIMARY KEY (tenant_id, seq)` backs that up. The runtime
role may only insert and read (`db_roles.INSERT_ONLY`)."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, fields
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncConnection

from agentic_cp.tables import audit_log
from agentic_cp.tenancy.context import current_tenant_id
from sdlc_commons.ledger import entry_hash

ACTOR_KINDS = ("human", "service", "system")
#: The actor of operator commands (`python -m agentic_cp admin ...`).
OPERATOR = "system:operator"


class AuditError(Exception):
    """An audit record was refused (no tenant, a float value, an unknown actor kind)."""


def _refuse_floats(name: str, value: object) -> None:
    if isinstance(value, float):
        raise AuditError(
            f"audit field {name!r} holds a float; use an integer or a decimal string "
            f"(a float does not survive the jsonb round trip byte for byte)"
        )
    if isinstance(value, Mapping):
        for key, item in value.items():
            _refuse_floats(f"{name}.{key}", item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _refuse_floats(name, item)


@dataclass(frozen=True)
class AuditEvent:
    """What happened; `append` adds seq, tenant, time and the chain links."""

    event: str
    actor: str
    actor_kind: str
    target: str = ""
    team: str | None = None
    before: Any = None
    after: Any = None
    reason: str = ""
    run_id: str | None = None
    trace_id: str | None = None
    policy_version: str | None = None

    def __post_init__(self) -> None:
        if self.actor_kind not in ACTOR_KINDS:
            raise AuditError(f"actor_kind must be one of {', '.join(ACTOR_KINDS)}")
        _refuse_floats("before", self.before)
        _refuse_floats("after", self.after)


@dataclass(frozen=True)
class AuditRecord:
    """One stored record, exactly as hashed."""

    seq: int
    tenant: str
    team: str | None
    actor: str
    actor_kind: str
    event: str
    target: str
    before: Any
    after: Any
    reason: str
    run_id: str | None
    trace_id: str | None
    policy_version: str | None
    at: str
    prev: str
    hash: str

    def entry(self) -> dict[str, Any]:
        """The hashed body: every field but `hash`."""
        return {f.name: getattr(self, f.name) for f in fields(self) if f.name != "hash"}


def format_at(moment: datetime) -> str:
    """ISO-8601 UTC with milliseconds and `Z`: the exact text that is hashed and stored."""
    return moment.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _record(row) -> AuditRecord:
    m = row._mapping
    values = {f.name: m[f.name] for f in fields(AuditRecord) if f.name != "tenant"}
    return AuditRecord(tenant=str(m["tenant_id"]), **values)


async def _lock(conn: AsyncConnection, tenant: UUID) -> None:
    key = func.hashtextextended(f"agentic.audit:{tenant}", 0)
    await conn.execute(select(func.pg_advisory_xact_lock(key)))


async def append(
    conn: AsyncConnection, event: AuditEvent, *, clock: Callable[[], datetime]
) -> AuditRecord:
    """Append `event` to the chain of the transaction's tenant and return the record."""
    tenant = await current_tenant_id(conn)
    if tenant is None:
        raise AuditError("an audit record needs a tenant: run inside tenant_tx")
    await _lock(conn, tenant)
    head = await conn.execute(
        select(audit_log.c.seq, audit_log.c.hash).order_by(audit_log.c.seq.desc()).limit(1)
    )
    last = head.first()
    body = {
        "seq": (last.seq + 1) if last else 1,
        "tenant": str(tenant),
        "team": event.team,
        "actor": event.actor,
        "actor_kind": event.actor_kind,
        "event": event.event,
        "target": event.target,
        "before": event.before,
        "after": event.after,
        "reason": event.reason,
        "run_id": event.run_id,
        "trace_id": event.trace_id,
        "policy_version": event.policy_version,
        "at": format_at(clock()),
        "prev": last.hash if last else "",
    }
    record = AuditRecord(**body, hash=entry_hash(body))
    row = {k: v for k, v in body.items() if k != "tenant"}
    await conn.execute(audit_log.insert().values(tenant_id=tenant, hash=record.hash, **row))
    return record


async def read_records(
    conn: AsyncConnection,
    *,
    after_seq: int = 0,
    team: str | None = None,
    limit: int | None = None,
) -> list[AuditRecord]:
    """The transaction's tenant's records after `after_seq` (of one team, with `team`), in
    order."""
    query = select(audit_log).where(audit_log.c.seq > after_seq).order_by(audit_log.c.seq)
    if team is not None:
        query = query.where(audit_log.c.team == team)
    if limit is not None:
        query = query.limit(limit)
    return [_record(row) for row in await conn.execute(query)]


async def verify(conn: AsyncConnection, *, from_seq: int = 1) -> list[str]:
    """Every problem in the transaction's tenant's chain from `from_seq` on, each naming
    the seq where it shows ([] = intact)."""
    if await current_tenant_id(conn) is None:
        raise AuditError("verifying a chain needs a tenant: run inside tenant_tx")
    records = await read_records(conn, after_seq=max(from_seq - 2, 0))
    problems: list[str] = []
    expected_seq, prev = 1, ""
    if records and from_seq > 1:
        anchor = records[0]
        if anchor.seq != from_seq - 1:
            problems.append(f"seq {from_seq}: the record before it (seq {from_seq - 1}) is missing")
        expected_seq, prev = anchor.seq + 1, anchor.hash
        records = records[1:]
    for record in records:
        if record.seq != expected_seq:
            problems.append(
                f"seq {record.seq}: expected seq {expected_seq} - a gap after seq "
                f"{expected_seq - 1} (a record was removed)"
            )
        if record.prev != prev:
            problems.append(
                f"seq {record.seq}: links to {record.prev!r}, but the previous record hashes "
                f"to {prev!r} - a record was edited, inserted or removed"
            )
        if entry_hash(record.entry()) != record.hash:
            problems.append(
                f"seq {record.seq}: its recorded hash does not match its contents - "
                f"the record was edited"
            )
        expected_seq, prev = record.seq + 1, record.hash
    return problems
