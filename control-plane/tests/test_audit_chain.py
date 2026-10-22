"""The audit chain: one hash chain per tenant, the run ledger's formula, serialised appends,
and a verifier that names the seq of every problem (Review Focus 2)."""

import asyncio
import hashlib
import json
from datetime import UTC, datetime, timedelta
from itertools import count

import anyio
import pytest
from sqlalchemy import func, select, update

from agentic_cp.__main__ import main
from agentic_cp.audit import AuditError, AuditEvent, append, verify
from agentic_cp.tables import audit_log
from agentic_cp.tenancy.context import tenant_tx

pytestmark = pytest.mark.anyio


def ticking_clock(start=datetime(2026, 10, 22, 9, 30, tzinfo=UTC)):
    steps = count()
    return lambda: start + timedelta(milliseconds=next(steps) * 1001)


def _event(n: int = 0, **extra) -> AuditEvent:
    return AuditEvent(
        event="role.assigned",
        actor="alice@example.test",
        actor_kind="human",
        target=f"user:{n}",
        after={"role": "developer", "n": n},
        **extra,
    )


async def _append_n(engine, tenant, n, clock):
    records = []
    for i in range(n):
        async with tenant_tx(engine, tenant) as conn:
            records.append(await append(conn, _event(i), clock=clock))
    return records


async def test_append_chains_records_per_tenant(two_tenants):
    db, a, b = two_tenants
    clock = ticking_clock()
    records = await _append_n(db.app, a, 3, clock)
    assert [r.seq for r in records] == [1, 2, 3]
    assert records[0].prev == ""
    assert [r.prev for r in records[1:]] == [r.hash for r in records[:-1]]
    assert all(r.tenant == str(a) for r in records)
    assert records[0].at == "2026-10-22T09:30:00.000Z"
    async with tenant_tx(db.app, b) as conn:
        first_b = await append(conn, _event(), clock=clock)
    assert first_b.seq == 1 and first_b.prev == ""


async def test_append_without_tenant_context_is_refused(db):
    with pytest.raises(AuditError):
        async with db.app.begin() as conn:
            await append(conn, _event(), clock=ticking_clock())
    async with db.owner.begin() as conn:
        total = await conn.execute(select(func.count()).select_from(audit_log))
        assert total.scalar_one() == 0


async def test_record_hash_matches_ledger_formula(two_tenants):
    db, a, _ = two_tenants
    (record,) = await _append_n(db.app, a, 1, ticking_clock())
    body = {
        "seq": 1,
        "tenant": str(a),
        "team": None,
        "actor": "alice@example.test",
        "actor_kind": "human",
        "event": "role.assigned",
        "target": "user:0",
        "before": None,
        "after": {"role": "developer", "n": 0},
        "reason": "",
        "run_id": None,
        "trace_id": None,
        "policy_version": None,
        "at": "2026-10-22T09:30:00.000Z",
        "prev": "",
    }
    blob = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    assert record.hash == "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()
    assert record.entry() == body


def test_record_values_refuse_floats():
    with pytest.raises(AuditError, match="float"):
        AuditEvent(event="budget.raised", actor="a", actor_kind="human", before={"x": 1.5})
    with pytest.raises(AuditError, match="float"):
        AuditEvent(event="budget.raised", actor="a", actor_kind="human", after={"x": [1, 2.0]})


def test_audit_event_refuses_an_unknown_actor_kind():
    with pytest.raises(AuditError, match="actor_kind"):
        AuditEvent(event="x", actor="a", actor_kind="robot")


async def test_verify_is_clean_on_an_untouched_chain(two_tenants):
    db, a, _ = two_tenants
    await _append_n(db.app, a, 3, ticking_clock())
    async with tenant_tx(db.app, a) as conn:
        assert await verify(conn) == []
        assert await verify(conn, from_seq=2) == []


async def test_verify_reports_an_edited_record_by_seq(two_tenants):
    db, a, _ = two_tenants
    await _append_n(db.app, a, 3, ticking_clock())
    async with tenant_tx(db.owner, a) as conn:
        await conn.execute(
            update(audit_log).where(audit_log.c.seq == 2).values(reason="quietly changed")
        )
    async with tenant_tx(db.app, a) as conn:
        problems = await verify(conn)
    assert problems and all(p.startswith("seq 2") for p in problems), problems


async def test_verify_reports_a_deleted_record(two_tenants):
    db, a, _ = two_tenants
    await _append_n(db.app, a, 3, ticking_clock())
    async with tenant_tx(db.owner, a) as conn:
        await conn.execute(audit_log.delete().where(audit_log.c.seq == 2))
    async with tenant_tx(db.app, a) as conn:
        problems = await verify(conn)
    assert problems and all(p.startswith("seq 3") for p in problems), problems
    assert any("gap" in p for p in problems)


async def test_audit_chains_verify_independently_per_tenant(two_tenants):
    db, a, b = two_tenants
    clock = ticking_clock()
    await _append_n(db.app, a, 3, clock)
    await _append_n(db.app, b, 3, clock)
    async with tenant_tx(db.owner, b) as conn:
        await conn.execute(update(audit_log).where(audit_log.c.seq == 2).values(actor="mallory"))
    async with tenant_tx(db.app, a) as conn:
        assert await verify(conn) == []
    async with tenant_tx(db.app, b) as conn:
        problems = await verify(conn)
    assert problems and problems[0].startswith("seq 2")


async def test_concurrent_appends_keep_one_chain(two_tenants):
    db, a, b = two_tenants
    clock = ticking_clock()
    await asyncio.gather(_append_n(db.app, a, 50, clock), _append_n(db.app, a, 50, clock))
    async with tenant_tx(db.app, a) as conn:
        seqs = (await conn.execute(select(audit_log.c.seq).order_by(audit_log.c.seq))).all()
        assert [s[0] for s in seqs] == list(range(1, 101))
        assert await verify(conn) == []


async def test_one_tenants_lock_does_not_block_another(two_tenants):
    db, a, b = two_tenants
    clock = ticking_clock()
    async with tenant_tx(db.app, a) as conn:
        await append(conn, _event(), clock=clock)  # holds a's lock until commit

        async def append_b():
            async with tenant_tx(db.app, b) as other:
                return await append(other, _event(), clock=clock)

        record = await asyncio.wait_for(append_b(), timeout=10)
        assert record.seq == 1


async def _verify_command(slug: str) -> int:
    """`python -m agentic_cp admin audit verify` (in a thread: it runs its own loop)."""
    return await anyio.to_thread.run_sync(main, ["admin", "audit", "verify", "--tenant", slug])


async def test_operator_command_verifies_one_tenant(two_tenants, monkeypatch, capsys):
    db, a, _ = two_tenants
    await _append_n(db.app, a, 2, ticking_clock())
    monkeypatch.setenv("AGENTIC_CP_DATABASE_URL", db.app_dsn)
    monkeypatch.setenv("AGENTIC_CP_OWNER_DATABASE_URL", db.owner_dsn)
    code = await _verify_command("tenant-a")
    assert code == 0
    assert "2 records" in capsys.readouterr().out
    async with tenant_tx(db.owner, a) as conn:
        await conn.execute(update(audit_log).where(audit_log.c.seq == 1).values(event="x"))
    code = await _verify_command("tenant-a")
    assert code == 1
    assert "seq 1" in capsys.readouterr().out
    code = await _verify_command("nobody")
    assert code == 3
