"""Server entry points (SP15 Task 11): `serve` refuses an unenforced database (ND-5),
`migrate` re-grants, seeds every tenant and reports the database guarantees, `worker` leads
alone and loops over tenants, and `--help` lists every command."""

from __future__ import annotations

import asyncio

import anyio
import pytest
from sqlalchemy import update

from agentic_cp.__main__ import build_parser, main
from agentic_cp.app import utc_now
from agentic_cp.gitops import GitRunner
from agentic_cp.policy_sync import worker
from agentic_cp.tables import roles
from agentic_cp.tenancy.context import tenant_tx

pytestmark = pytest.mark.anyio


async def _run(*argv: str) -> int:
    return await anyio.to_thread.run_sync(main, list(argv))


@pytest.fixture
def operator_env(api, monkeypatch):
    monkeypatch.setenv("AGENTIC_CP_DATABASE_URL", api.db.app_dsn)
    monkeypatch.setenv("AGENTIC_CP_OWNER_DATABASE_URL", api.db.owner_dsn)
    return api


async def test_serve_refuses_unenforced_database(operator_env, capsys):
    async with operator_env.db.owner.begin() as conn:
        await conn.exec_driver_sql("CREATE TABLE stray (id int primary key)")
    assert await _run("serve", "--port", "1") == 2
    err = capsys.readouterr().err
    assert "AGENTIC_CP_REQUIRE_DB_ENFORCEMENT" in err
    assert "table stray is neither tenants nor a registered tenant table" in err


async def test_worker_refuses_unenforced_database(operator_env, capsys, monkeypatch):
    """DF-495: the worker refuses what `serve` refuses (exit 2), before it leads."""
    started: list[str] = []

    async def never(*args, **kwargs):
        started.append("ran")

    monkeypatch.setattr(worker, "run_forever", never)
    async with operator_env.db.owner.begin() as conn:
        await conn.exec_driver_sql("CREATE TABLE stray (id int primary key)")
    assert await _run("worker") == 2
    err = capsys.readouterr().err
    assert "refusing to run the worker" in err
    assert "table stray is neither tenants nor a registered tenant table" in err
    assert started == []


async def test_migrate_applies_grants_and_seeds_every_tenant(operator_env, capsys):
    api = operator_env
    async with tenant_tx(api.db.owner, api.a.id) as conn:
        await conn.execute(roles.delete().where(roles.c.name == "auditor"))
    async with tenant_tx(api.db.owner, api.b.id) as conn:
        await conn.execute(roles.delete().where(roles.c.name == "auditor"))
        await conn.execute(  # an edited builtin role is left alone
            update(roles).where(roles.c.name == "approver").values(permissions=["gate.approve"])
        )
    assert await _run("migrate") == 0
    out = capsys.readouterr().out
    assert "tenant-a: seeded roles auditor" in out and "tenant-b: seeded roles auditor" in out
    assert "database guarantees: enforced" in out
    async with tenant_tx(api.db.app, api.b.id) as conn:
        approver = (await conn.execute(roles.select().where(roles.c.name == "approver"))).one()
    assert list(approver.permissions) == ["gate.approve"]


def test_help_lists_commands():
    text = build_parser().format_help()
    for command in ("serve", "worker", "migrate", "admin", "db", "openapi"):
        assert command in text
    for line in (
        "admin tenant create",
        "admin tenant list",
        "admin tenant set-policy-repo",
        "admin issue-token",
        "admin audit verify",
    ):
        assert line in text


async def test_worker_leads_alone_and_stops(api, monkeypatch):
    passes: list[str] = []

    async def counting(engine, git, settings, clock):
        passes.append("pass")
        stop.set()
        return {}

    monkeypatch.setattr(worker, "run_pass", counting)
    stop = asyncio.Event()
    git = GitRunner(api.settings)
    await asyncio.wait_for(
        worker.run_forever(api.db.app, git, api.settings, utc_now, stop=stop), 30
    )
    assert passes == ["pass"]


async def test_a_second_worker_waits_for_the_leader(api, monkeypatch):
    started, release = asyncio.Event(), asyncio.Event()
    calls: list[str] = []

    async def leader_pass(engine, git, settings, clock):
        calls.append("leader")
        started.set()
        await release.wait()
        return {}

    monkeypatch.setattr(worker, "run_pass", leader_pass)
    stop_leader, stop_other = asyncio.Event(), asyncio.Event()
    settings = api.settings.model_copy(update={"policy_poll_seconds": 10})
    git = GitRunner(settings)
    leader = asyncio.create_task(
        worker.run_forever(api.db.app, git, settings, utc_now, stop=stop_leader)
    )
    await asyncio.wait_for(started.wait(), 30)
    other = asyncio.create_task(
        worker.run_forever(api.db.app, git, settings, utc_now, stop=stop_other)
    )
    await asyncio.sleep(0.5)
    assert calls == ["leader"]  # the second worker did not run a pass
    stop_other.set()
    await asyncio.wait_for(other, 30)
    stop_leader.set()
    release.set()
    await asyncio.wait_for(leader, 30)
