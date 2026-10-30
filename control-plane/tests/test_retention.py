"""Retention (M3-06, OR-01, owner decision 115): the job deletes what its tenant's
`retention.yaml` says has expired, keeps the audit chain longest and verifiable, stops at
legal holds, writes one audit event with the counts per run, and runs once at a time."""

from __future__ import annotations

from datetime import timedelta

import anyio
import pytest
from policy_repos import commit, make_policy_repo, url, write
from sqlalchemy import func, select

from agentic_cp.__main__ import main
from agentic_cp.audit import read_records, verify
from agentic_cp.retention import LEADER_KEY, gateway_config, prune_tenant, run_retention
from agentic_cp.retention.tenants import deleted_slug
from agentic_cp.tables import (
    audit_log,
    feedback,
    feedback_events,
    notifications,
    run_events,
    runs,
    tenants,
    users,
)
from agentic_cp.tenancy.context import tenant_tx

pytestmark = pytest.mark.anyio

DAY = timedelta(days=1)


async def sync_policy(api, tmp_path, retention: str | None = None) -> str:
    repo = make_policy_repo(tmp_path / "policy", teams=("payments", "billing"))
    if retention is not None:
        write(repo, "policy/retention.yaml", retention)
        commit(repo, "retention")
    made = await api.put(
        "/v1/tenant/policy-source", api.a.admin_token, {"repo": url(repo), "branch": "main"}
    )
    assert made.status_code == 200, made.text
    sync = made.json()["sync"]
    assert sync["kind"] == "activated", sync
    return sync["version"]


async def add_run(api, run_id: str, team: str, at, *, events: int = 2) -> None:
    async with tenant_tx(api.db.owner, api.a.id) as conn:
        await conn.execute(
            runs.insert().values(
                tenant_id=api.a.id,
                run_id=run_id,
                team=team,
                started_by=api.a.admin_id,
                policy_version="sha256:" + "0" * 64,
                policy_known=False,
                status="closed",
                opened_at=at,
                last_event_at=at,
            )
        )
        for seq in range(1, events + 1):
            await conn.execute(
                run_events.insert().values(
                    tenant_id=api.a.id,
                    run_id=run_id,
                    seq=seq,
                    hash=f"h{seq}",
                    prev=f"h{seq - 1}" if seq > 1 else "",
                    event="step_finished",
                    step="build",
                    at=at.isoformat(),
                    entry={"seq": seq},
                    received_at=at,
                )
            )


async def add_feedback(api, run_id: str, team: str, at) -> None:
    async with tenant_tx(api.db.owner, api.a.id) as conn:
        await conn.execute(
            feedback.insert().values(
                tenant_id=api.a.id,
                id=f"fb-{run_id}",
                run_id=run_id,
                team=team,
                signal="gate-edit",
                actor="developer",
                given_at=at,
                author_id=api.a.admin_id,
                record={},
                captured_at=at,
            )
        )
        await conn.execute(
            feedback_events.insert().values(
                tenant_id=api.a.id,
                run_id=run_id,
                seq=1,
                hash="f1",
                prev="",
                event="learning.feedback_captured",
                entry={},
                received_at=at,
            )
        )


async def add_mail(api, key: str, status: str, at) -> None:
    async with tenant_tx(api.db.owner, api.a.id) as conn:
        await conn.execute(
            notifications.insert().values(
                tenant_id=api.a.id,
                kind="budget",
                recipient="someone@a.example.test",
                subject="s",
                body_text="" if status != "pending" else "body",
                body_html="",
                dedupe_key=key,
                status=status,
                created_at=at,
                sent_at=at if status == "sent" else None,
            )
        )


async def names(api, table, column) -> list[str]:
    async with tenant_tx(api.db.owner, api.a.id) as conn:
        return sorted(row[0] for row in await conn.execute(select(column)))


async def prune_events(api) -> list:
    async with tenant_tx(api.db.owner, api.a.id) as conn:
        return [r for r in await read_records(conn) if r.event == "retention.pruned"]


def at_day(api, days: int):
    start = api.clock.now
    return lambda: start + days * DAY


async def test_expired_rows_are_removed_and_young_rows_kept(api, tmp_path):
    await sync_policy(api, tmp_path)
    t0 = api.clock.now
    await add_run(api, "r-old", "payments", t0)
    await add_run(api, "r-young", "payments", t0 + 450 * DAY)
    await add_mail(api, "sent-old", "sent", t0)
    await add_mail(api, "sent-young", "sent", t0 + 490 * DAY)
    await add_mail(api, "pending-old", "pending", t0)
    async with tenant_tx(api.db.owner, api.a.id) as conn:
        audit_before = len(await read_records(conn))

    results = await run_retention(api.db.owner, at_day(api, 500))

    report = results["tenant-a"]
    assert report.counts["runs"] == 1 and report.counts["run_events"] == 2
    assert report.counts["notifications"] == 1
    assert report.counts["audit"] == 0  # audit is kept longest: 7 years
    assert await names(api, runs, runs.c.run_id) == ["r-young"]
    assert await names(api, run_events, run_events.c.run_id) == ["r-young", "r-young"]
    assert await names(api, notifications, notifications.c.dedupe_key) == [
        "pending-old",  # a mail not sent yet is never deleted
        "sent-young",
    ]
    (event,) = await prune_events(api)
    assert event.actor == "system:retention" and event.actor_kind == "system"
    assert event.after["counts"]["runs"] == 1 and event.after["counts"]["run_events"] == 2
    assert event.after["days"]["runs"] == 400 and event.after["audit"] is None
    async with tenant_tx(api.db.owner, api.a.id) as conn:
        assert len(await read_records(conn)) == audit_before + 1


async def test_audit_is_pruned_only_after_its_period_and_still_verifies(api, tmp_path):
    await sync_policy(api, tmp_path)
    await run_retention(api.db.owner, at_day(api, 500))
    async with tenant_tx(api.db.owner, api.a.id) as conn:
        before = await read_records(conn)
    old = [r for r in before if r.event != "retention.pruned"]

    results = await run_retention(api.db.owner, at_day(api, 3000))

    assert results["tenant-a"].counts["audit"] == len(old)
    async with tenant_tx(api.db.owner, api.a.id) as conn:
        kept = await read_records(conn)
        assert await verify(conn) == []
    assert [r.event for r in kept] == ["retention.pruned", "retention.pruned"]
    checkpoint = kept[-1].after["audit"]
    assert checkpoint == {"through_seq": old[-1].seq, "through_hash": old[-1].hash}
    assert kept[0].prev == old[-1].hash


async def test_a_gap_without_a_checkpoint_is_reported(api, tmp_path):
    async with tenant_tx(api.db.owner, api.a.id) as conn:
        first = (await read_records(conn))[0]
        await conn.execute(audit_log.delete().where(audit_log.c.seq == first.seq))
        problems = await verify(conn)
    assert problems and "retention checkpoint" in problems[0]


async def test_a_legal_hold_keeps_its_scope(api, tmp_path):
    await sync_policy(
        api,
        tmp_path,
        "days: { runs: 400 }\n"
        "legal_holds:\n"
        "  - { id: LH-1, reason: case 1, data: [runs], teams: [payments] }\n"
        "  - { id: LH-2, reason: case 2, data: [audit] }\n",
    )
    t0 = api.clock.now
    await add_run(api, "r-pay", "payments", t0)
    await add_run(api, "r-bill", "billing", t0)

    results = await run_retention(api.db.owner, at_day(api, 3000))

    assert await names(api, runs, runs.c.run_id) == ["r-pay"]
    assert results["tenant-a"].counts["audit"] == 0  # the whole chain is on hold
    (event,) = await prune_events(api)
    assert event.after["holds"] == ["LH-1", "LH-2"]


async def test_an_audit_hold_for_a_team_stops_at_its_first_record(api, tmp_path):
    await sync_policy(
        api, tmp_path, "legal_holds: [ { id: LH-1, reason: x, data: [audit], teams: [billing] } ]\n"
    )
    async with tenant_tx(api.db.owner, api.a.id) as conn:
        from agentic_cp.audit import AuditEvent, append

        await append(
            conn,
            AuditEvent(event="x.held", actor="a", actor_kind="human", team="billing"),
            clock=lambda: api.clock.now,
        )
        await append(
            conn,
            AuditEvent(event="x.after", actor="a", actor_kind="human", team="payments"),
            clock=lambda: api.clock.now,
        )
        records = await read_records(conn)
    held = next(r for r in records if r.event == "x.held")

    results = await run_retention(api.db.owner, at_day(api, 3000))

    assert results["tenant-a"].counts["audit"] == held.seq - 1
    async with tenant_tx(api.db.owner, api.a.id) as conn:
        kept = await read_records(conn)
        assert await verify(conn) == []
    assert kept[0].seq == held.seq


async def test_feedback_expires_first_and_then_its_run(api, tmp_path):
    await sync_policy(api, tmp_path)
    t0 = api.clock.now
    await add_run(api, "r-fb", "payments", t0)
    await add_feedback(api, "r-fb", "payments", t0 + 100 * DAY)

    first = await run_retention(api.db.owner, at_day(api, 450))
    # the run is past 400 days but its feedback (365) is not: the run waits for it
    assert first["tenant-a"].counts["feedback"] == 0
    assert await names(api, runs, runs.c.run_id) == ["r-fb"]

    second = await run_retention(api.db.owner, at_day(api, 470))
    assert second["tenant-a"].counts["feedback"] == 1
    assert second["tenant-a"].counts["feedback_events"] == 1
    assert second["tenant-a"].counts["runs"] == 1
    assert await names(api, runs, runs.c.run_id) == []


async def test_null_keeps_a_kind_forever(api, tmp_path):
    await sync_policy(api, tmp_path, "days: { audit: null, runs: null }\n")
    await add_run(api, "r-old", "payments", api.clock.now)
    results = await run_retention(api.db.owner, at_day(api, 9000))
    assert results["tenant-a"].counts["runs"] == 0 and results["tenant-a"].counts["audit"] == 0
    assert await names(api, runs, runs.c.run_id) == ["r-old"]


async def test_a_dry_run_deletes_and_records_nothing(api, tmp_path):
    await sync_policy(api, tmp_path)
    await add_run(api, "r-old", "payments", api.clock.now)
    results = await run_retention(api.db.owner, at_day(api, 3000), dry_run=True)
    assert results["tenant-a"].counts["runs"] == 1 and results["tenant-a"].dry_run
    assert results["tenant-a"].counts["audit"] > 0
    assert await names(api, runs, runs.c.run_id) == ["r-old"]
    assert await prune_events(api) == []


async def test_a_tenant_without_a_policy_is_untouched(api, tmp_path):
    results = await run_retention(api.db.owner, at_day(api, 9000))
    assert results["tenant-b"].skipped == "no active policy"
    async with tenant_tx(api.db.owner, api.b.id) as conn:
        assert await read_records(conn) != []
        assert [r for r in await read_records(conn) if r.event == "retention.pruned"] == []


async def test_one_runner_at_a_time(api, tmp_path):
    await sync_policy(api, tmp_path)
    await add_run(api, "r-old", "payments", api.clock.now)
    async with api.db.owner.connect() as other:
        key = func.hashtextextended(LEADER_KEY, 0)
        assert (await other.execute(select(func.pg_try_advisory_lock(key)))).scalar()
        assert await run_retention(api.db.owner, at_day(api, 3000)) is None
        await other.execute(select(func.pg_advisory_unlock(key)))
    assert await names(api, runs, runs.c.run_id) == ["r-old"]


async def test_prune_tenant_batches(api, tmp_path):
    await sync_policy(api, tmp_path)
    for n in range(3):
        await add_run(api, f"r-{n}", "payments", api.clock.now, events=1)
    async with tenant_tx(api.db.owner, api.a.id) as conn:
        report = await prune_tenant(conn, now=api.clock.now + 500 * DAY, batch=2)
    assert report.counts["runs"] == 2 and report.more
    assert len(await names(api, runs, runs.c.run_id)) == 1


# ── operator commands ────────────────────────────────────────────────────────


@pytest.fixture
def operator_env(api, monkeypatch):
    monkeypatch.setenv("AGENTIC_CP_DATABASE_URL", api.db.app_dsn)
    monkeypatch.setenv("AGENTIC_CP_OWNER_DATABASE_URL", api.db.owner_dsn)
    return api


async def _run(*argv: str) -> int:
    return await anyio.to_thread.run_sync(main, list(argv))


async def test_retention_run_dry_run_prints_counts(operator_env, tmp_path, capsys):
    api = operator_env
    await sync_policy(api, tmp_path)
    assert await _run("retention", "run", "--dry-run", "--tenant", "tenant-a") == 0
    out = capsys.readouterr().out
    assert "tenant tenant-a: would delete feedback 0" in out
    assert "tenant-b" not in out
    assert await prune_events(api) == []
    assert await _run("retention", "run", "--tenant", "nobody") == 3


async def test_retention_run_records_one_event_per_tenant(operator_env, tmp_path, capsys):
    api = operator_env
    await sync_policy(api, tmp_path)
    assert await _run("retention", "run") == 0
    out = capsys.readouterr().out
    assert "tenant tenant-a: deleted" in out
    assert "tenant tenant-b: skipped (no active policy)" in out
    assert len(await prune_events(api)) == 1


async def test_gateway_config_carries_the_tenants_periods(operator_env, tmp_path, capsys):
    api = operator_env
    await sync_policy(api, tmp_path, "days: { gateway_usage: 500 }\n")
    assert await _run("retention", "gateway-config", "--tenant", "tenant-a") == 0
    out = capsys.readouterr().out
    assert "retention:\n  enabled: true\n" in out
    assert "  usage_days: 500\n" in out and "  audit_days: 2555\n" in out
    assert await _run("retention", "gateway-config", "--tenant", "tenant-b") == 2


def test_gateway_config_keeps_held_usage_forever():
    stored = {
        "days": {"audit": None, "gateway_usage": 400},
        "legal_holds": [{"id": "LH-1", "reason": "x", "data": ["gateway_usage"], "teams": []}],
    }
    block = gateway_config(stored)["retention"]
    assert block["usage_days"] == 0 and block["audit_days"] == 0 and block["enabled"] is False


# ── tenant deletion (DF-568) ─────────────────────────────────────────────────


async def _delete(slug: str, *, confirm: str | None = None) -> int:
    return await _run(
        "admin", "tenant", "delete", "--tenant", slug, "--confirm", confirm or slug, "--reason", "r"
    )


async def test_only_a_suspended_tenant_is_deleted(operator_env, capsys):
    assert await _delete("tenant-a") == 2
    assert "suspend it first" in capsys.readouterr().err
    assert await _run("admin", "tenant", "suspend", "--tenant", "tenant-a", "--reason", "x") == 0
    assert await _delete("tenant-a", confirm="tenant-b") == 2


async def test_delete_erases_all_but_the_audit_chain(operator_env, tmp_path):
    api = operator_env
    await sync_policy(api, tmp_path)
    await add_run(api, "r-1", "payments", api.clock.now)
    assert await _run("admin", "tenant", "suspend", "--tenant", "tenant-a", "--reason", "x") == 0
    assert await _delete("tenant-a") == 0
    async with tenant_tx(api.db.owner, api.a.id) as conn:
        assert await verify(conn) == []
        records = await read_records(conn)
        assert (await conn.execute(select(func.count()).select_from(users))).scalar_one() == 0
        found = select(tenants.c.status).where(tenants.c.id == api.a.id)
        status = (await conn.execute(found)).scalar_one()
    assert status == "deleted"
    assert records[-1].event == "tenant.deleted"
    assert records[-1].after["counts"]["runs"] == 1
    assert records[-1].after["audit_days"] == 2555
    assert (await api.get("/v1/me", api.a.admin_token)).status_code == 401
    assert (await api.get("/v1/me", api.b.admin_token)).status_code == 200
    # the slug is free again: the deleted tenant is `deleted-<id>` and cannot be resumed
    assert await _run("admin", "tenant", "resume", "--tenant", "tenant-a", "--reason", "x") == 3

    # the chain stays for its period, then the retention run removes it and the tenant
    deleted = deleted_slug(api.a.id)
    kept = await run_retention(api.db.owner, at_day(api, 100))
    assert "kept 2555 days" in kept[deleted].skipped
    gone = await run_retention(api.db.owner, at_day(api, 2600))
    assert gone[deleted].counts["audit"] == len(records)
    async with api.db.owner.begin() as conn:
        slugs = [row[0] for row in await conn.execute(select(tenants.c.slug))]
    assert slugs == ["tenant-b"]


async def test_a_held_tenant_is_not_deleted(operator_env, tmp_path, capsys):
    api = operator_env
    await sync_policy(api, tmp_path, "legal_holds: [ { id: LH-9, reason: case } ]\n")
    assert await _run("admin", "tenant", "suspend", "--tenant", "tenant-a", "--reason", "x") == 0
    assert await _delete("tenant-a") == 2
    assert "legal hold (LH-9)" in capsys.readouterr().err


async def test_a_run_catches_up_on_a_backlog_in_several_passes(api, tmp_path):
    await sync_policy(api, tmp_path)
    for n in range(5):
        await add_run(api, f"r-{n}", "payments", api.clock.now, events=1)
    results = await run_retention(api.db.owner, at_day(api, 500), batch=2)
    report = results["tenant-a"]
    assert report.counts["runs"] == 5 and not report.more
    assert await names(api, runs, runs.c.run_id) == []
    assert len(await prune_events(api)) == 3  # one event per pass (2 + 2 + 1)


async def test_a_deleted_tenant_frees_its_slug_and_refuses_other_commands(operator_env):
    api = operator_env
    assert await _run("admin", "tenant", "suspend", "--tenant", "tenant-a", "--reason", "x") == 0
    assert await _delete("tenant-a") == 0
    async with api.db.owner.begin() as conn:
        found = select(tenants.c.slug, tenants.c.status).where(tenants.c.id == api.a.id)
        slug, status = (await conn.execute(found)).one()
    assert status == "deleted" and slug.startswith("deleted-")
    assert await _run("admin", "audit", "verify", "--tenant", slug) == 0
    command = ("admin", "tenant", "set-mail-from", "--tenant", slug, "--address", "a@b.c")
    assert await _run(*command) == 2
    assert await _run("admin", "tenant", "resume", "--tenant", slug, "--reason", "x") == 2


def test_gateway_config_keeps_held_audit_forever():
    stored = {
        "days": {"audit": 2555, "gateway_usage": 400},
        "legal_holds": [{"id": "LH-1", "reason": "x", "data": ["audit"], "teams": ["payments"]}],
    }
    block = gateway_config(stored)["retention"]
    assert block["audit_days"] == 0 and block["usage_days"] == 400
