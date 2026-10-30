"""The restore drill (M3-02): a restored database is checked for its migration revision,
the runtime role's guarantees and every tenant's audit chain (`agentic_cp.restore`,
`python -m agentic_cp db verify-restore`), and `scripts/restore_drill.py` restores a
`pg_dump -Fc --schema=agentic` file into a fresh Postgres and runs those checks.

The dump is taken with pg_dump inside the test server's own container natively, or with the
local pg_dump in the Linux check image; the drill restores into a fresh pinned container
natively, or into a new database on the check run's server."""

from __future__ import annotations

import asyncio
import importlib.util
import os
import shutil
import subprocess
import sys
import uuid

import asyncpg
import pytest
import sqlalchemy.exc
from apiclient import FakeClock, bootstrap
from pg import APP_ROLE, REPO_ROOT, SCHEMA, connect_admin, unavailable
from sqlalchemy.engine import make_url

from agentic_cp import __main__ as main
from agentic_cp.restore import RestoreReport, filtered_toc, schema_owner, verify_restored

pytestmark = pytest.mark.anyio

TOC = """;
; Archive created at 2026-10-29 10:00:00 UTC
;
3601; 2615 16385 SCHEMA - agentic cp_owner
3602; 1259 16400 TABLE agentic tenants cp_owner
3603; 1259 16410 VIEW agentic usage_source cp_owner
3604; 1259 16420 MATERIALIZED VIEW agentic slow_view cp_owner
3605; 0 16400 TABLE DATA agentic tenants cp_owner
3606; 1259 16430 VIEW agentic dash_runs cp_owner
"""


def _drill_module():
    name = "_agentic_restore_drill"
    if name not in sys.modules:
        path = REPO_ROOT / "scripts" / "restore_drill.py"
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


def test_filtered_toc_leaves_the_views_to_this_release():
    lines = filtered_toc(TOC).splitlines()
    assert "3602; 1259 16400 TABLE agentic tenants cp_owner" in lines
    assert "3605; 0 16400 TABLE DATA agentic tenants cp_owner" in lines
    assert ";3603; 1259 16410 VIEW agentic usage_source cp_owner" in lines
    assert ";3604; 1259 16420 MATERIALIZED VIEW agentic slow_view cp_owner" in lines
    assert ";3606; 1259 16430 VIEW agentic dash_runs cp_owner" in lines
    assert "3601; 2615 16385 SCHEMA - agentic cp_owner" in lines


def test_schema_owner_comes_from_the_schema_entry():
    assert schema_owner(TOC, "agentic") == "cp_owner"
    assert schema_owner(TOC, "other") is None


def test_report_lines_name_each_finding():
    report = RestoreReport(revision="0001", head="m9", enforced=False, reasons=["no rls"])
    text = "\n".join(report.lines())
    assert "older" in text and "migrate" in text and "MISSING" in text and "no rls" in text
    assert not report.ok and "NOT usable" in text


def test_a_restore_with_no_tenants_is_not_usable_unless_allowed():
    # review F2: an empty or wrong dump must not pass as "restore verified"
    report = RestoreReport(revision="m9", head="m9", enforced=True)
    text = "\n".join(report.lines())
    assert not report.ok
    assert "NOT usable" in text and "restore verified" not in text
    assert "--allow-empty" in text
    allowed = RestoreReport(revision="m9", head="m9", enforced=True, allow_empty=True)
    assert allowed.ok and "restore verified" in "\n".join(allowed.lines())


async def _bootstrapped(db) -> None:
    clock = FakeClock()
    await bootstrap(db, clock, "tenant-a", "Tenant A")
    await bootstrap(db, clock, "tenant-b", "Tenant B")


async def test_verify_a_healthy_database(db):
    await _bootstrapped(db)
    report = await verify_restored(db.owner, SCHEMA, APP_ROLE)
    assert report.revision_state == "current" and report.enforced, report.lines()
    assert [t.slug for t in report.tenants] == ["tenant-a", "tenant-b"]
    assert all(t.records > 0 and not t.problems for t in report.tenants)
    assert report.ok


async def test_verify_finds_a_broken_audit_chain(db):
    await _bootstrapped(db)
    admin = await asyncpg.connect(db.admin_dsn)
    try:
        # a superuser with triggers off: what a damaged restore could look like
        await admin.execute("SET session_replication_role = replica")
        await admin.execute(f"UPDATE {SCHEMA}.audit_log SET reason = 'tampered' WHERE seq = 1")
    finally:
        await admin.close()
    report = await verify_restored(db.owner, SCHEMA, APP_ROLE)
    assert not report.ok
    assert all(t.problems for t in report.tenants)


async def test_verify_restore_command(db, monkeypatch, capsys):
    await _bootstrapped(db)
    monkeypatch.setenv("AGENTIC_CP_DATABASE_URL", db.app_dsn)
    monkeypatch.setenv("AGENTIC_CP_OWNER_DATABASE_URL", db.owner_dsn)
    args = main.build_parser().parse_args(["db", "verify-restore"])
    assert await _in_thread(args.run, args) == 0
    out = capsys.readouterr().out
    assert "restore verified" in out and "tenant tenant-a: audit chain intact" in out
    assert db.owner_dsn not in out


async def test_verify_restore_command_refuses_an_empty_database(db, monkeypatch, capsys):
    monkeypatch.setenv("AGENTIC_CP_DATABASE_URL", db.app_dsn)
    monkeypatch.setenv("AGENTIC_CP_OWNER_DATABASE_URL", db.owner_dsn)
    args = main.build_parser().parse_args(["db", "verify-restore"])
    assert await _in_thread(args.run, args) == 1
    assert "restore NOT usable" in capsys.readouterr().out
    args = main.build_parser().parse_args(["db", "verify-restore", "--allow-empty"])
    assert await _in_thread(args.run, args) == 0
    assert "restore verified" in capsys.readouterr().out


def test_the_drill_passes_allow_empty_through(monkeypatch, tmp_path):
    drill = _drill_module()
    seen = {}

    def fake_run_drill(dump, target, **kwargs):
        seen.update(kwargs)
        return RestoreReport(revision="m9", head="m9", enforced=True, allow_empty=True)

    monkeypatch.setattr(drill, "run_drill", fake_run_drill)
    monkeypatch.setattr(drill, "FreshContainer", lambda: object())
    dump = tmp_path / "agentic.dump"
    dump.write_bytes(b"PGDMP")
    assert drill.main(["--dump", str(dump), "--allow-empty"]) == 0
    assert seen["allow_empty"] is True
    assert drill.main(["--dump", str(dump)]) == 0
    assert seen["allow_empty"] is False


async def _in_thread(func, *args):
    import anyio.to_thread

    return await anyio.to_thread.run_sync(func, *args)


# ── the drill end to end ──────────────────────────────────────────────────────


def _dump(pg_server, database: str) -> bytes:
    drill = _drill_module()
    url = make_url(pg_server.admin_dsn)
    if pg_server.container:
        tools = drill.ContainerTools(pg_server.engine, pg_server.container, url.password)
    else:
        if shutil.which("pg_dump") is None:
            if os.environ.get("AGENTIC_CHECK_IMAGE") == "1":
                pytest.fail("the Linux check image must carry pg_dump (postgresql-client)")
            unavailable("pg_dump is not on PATH and the test server is not a container")
        tools = drill.LocalTools(pg_server.admin_dsn)
    return tools.run(["pg_dump", "-Fc", "--schema", SCHEMA], database=database)


async def test_the_drill_restores_a_dump_into_a_fresh_target(db, pg_server):
    await _bootstrapped(db)
    source = await verify_restored(db.owner, SCHEMA, APP_ROLE)
    dump = _dump(pg_server, db.name)
    assert dump.startswith(b"PGDMP")
    drill = _drill_module()
    if pg_server.container:
        target = drill.FreshContainer(
            pg_server.engine, name=f"agentic-m3-helm-drill-{uuid.uuid4().hex[:8]}"
        )
    else:
        target = drill.ExistingServer(pg_server.admin_dsn)
    report = await _in_thread(drill.run_drill, dump, target)
    assert report.ok, report.lines()
    assert [t.slug for t in report.tenants] == ["tenant-a", "tenant-b"]
    assert [t.records for t in report.tenants] == [t.records for t in source.tenants]
    assert all(t.records > 0 for t in report.tenants)
    # the source is untouched and still verifies
    after = await verify_restored(db.owner, SCHEMA, APP_ROLE)
    assert after.ok and after.tenants == source.tenants
    if not pg_server.container:  # the drill's database and role are gone again
        admin = await connect_admin(pg_server.admin_dsn)
        try:
            leftovers = await admin.fetch(
                "select datname from pg_database where datname like 'agentic_drill_%'"
            )
            roles = await admin.fetch(
                "select rolname from pg_roles where rolname like 'agentic_drill_%'"
            )
        finally:
            await admin.close()
        assert not leftovers and not roles


def test_the_drill_refuses_a_file_that_is_not_a_dump():
    drill = _drill_module()
    with pytest.raises(drill.DrillError, match="custom-format"):
        drill.check_dump(b"-- plain SQL\nselect 1;\n")


# ── DF-806: exit codes, identifiers, cleanup ──────────────────────────────────


@pytest.mark.parametrize(
    "error",
    [
        asyncpg.PostgresError("connection lost"),
        TimeoutError("timed out"),
        subprocess.TimeoutExpired(["pg_restore"], 3600),
        sqlalchemy.exc.OperationalError("select 1", {}, Exception("gone")),
    ],
    ids=["asyncpg", "timeout", "subprocess-timeout", "sqlalchemy"],
)
def test_a_drill_that_cannot_run_exits_2(monkeypatch, tmp_path, capsys, error):
    # (a) a scheduled drill tells "could not run" (2) from "not usable" (1)
    drill = _drill_module()

    def broken(dump, target, **kwargs):
        raise error

    monkeypatch.setattr(drill, "run_drill", broken)
    monkeypatch.setattr(drill, "FreshContainer", lambda: object())
    dump = tmp_path / "agentic.dump"
    dump.write_bytes(b"PGDMP")
    assert drill.main(["--dump", str(dump)]) == 2
    assert "could not run" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("name", "quoted"),
    [
        ("cp_owner", '"cp_owner"'),
        ("app-owner", '"app-owner"'),
        ("ops@db", '"ops@db"'),
        ('a"b', '"a""b"'),
    ],
)
def test_identifiers_are_quoted_not_refused(name, quoted):
    # (b) owner roles such as app-owner can be drilled
    assert _drill_module()._ident(name) == quoted


@pytest.mark.parametrize("name", ["", "a\x00b", "x" * 64])
def test_identifiers_postgres_cannot_hold_are_refused(name):
    drill = _drill_module()
    with pytest.raises(drill.DrillError, match="identifier"):
        drill._ident(name)


@pytest.mark.parametrize("created", [True, False])
def test_the_drill_drops_an_owner_role_only_when_it_created_it(monkeypatch, created):
    # (c) a scratch server keeps no role the drill added, and loses none it had
    drill = _drill_module()
    stopped = {}

    class Tools:
        def put(self, name, data):
            return f"/tmp/{name}"

        def run(self, argv, *, database):
            return TOC.encode() if "-l" in argv else b""

    class Target:
        admin_dsn = "postgresql://postgres@scratch/postgres"

        def start(self):
            return Tools()

        def stop(self, database, runtime_role, owner_role=None):
            stopped.update(database=database, owner_role=owner_role)

    async def prepare(admin_dsn, database, owner):
        return created

    async def finish(*args):
        return RestoreReport(revision="m9", head="m9", enforced=True, allow_empty=True)

    monkeypatch.setattr(drill, "_prepare", prepare)
    monkeypatch.setattr(drill, "_finish", finish)
    drill.run_drill(b"PGDMP", Target())
    assert stopped["database"].startswith("agentic_drill_")
    assert stopped["owner_role"] == ("cp_owner" if created else None)


def test_the_scratch_server_drops_the_owner_role_after_the_database(monkeypatch):
    drill = _drill_module()
    statements = []

    class Admin:
        async def execute(self, sql):
            statements.append(sql)

        async def close(self):
            pass

    async def connect(dsn, timeout=60.0):
        return Admin()

    monkeypatch.setattr(drill, "_connect", connect)
    asyncio.run(
        drill._drop("postgresql://x", "agentic_drill_1", "agentic_drill_1_app", "app-owner")
    )
    assert statements == [
        'DROP DATABASE IF EXISTS "agentic_drill_1" WITH (FORCE)',
        'DROP ROLE IF EXISTS "agentic_drill_1_app"',
        'DROP ROLE IF EXISTS "app-owner"',
    ]


def test_a_failed_database_create_drops_the_owner_role_it_made(monkeypatch):
    # review: the role must not leak when CREATE DATABASE fails after CREATE ROLE
    drill = _drill_module()
    statements = []

    class Admin:
        async def fetchval(self, sql, *args):
            return None  # the server lacks the owner role

        async def execute(self, sql):
            statements.append(sql)
            if sql.startswith("CREATE DATABASE"):
                raise asyncpg.PostgresError("disk full")

        async def close(self):
            pass

    async def connect(dsn, timeout=60.0):
        return Admin()

    monkeypatch.setattr(drill, "_connect", connect)
    with pytest.raises(asyncpg.PostgresError):
        asyncio.run(drill._prepare("postgresql://x", "agentic_drill_1", "app-owner"))
    assert statements[-1] == 'DROP ROLE IF EXISTS "app-owner"'


def test_a_failed_cleanup_does_not_replace_the_verdict(monkeypatch, capsys):
    drill = _drill_module()

    async def broken(*args):
        raise asyncpg.PostgresError("server gone")

    monkeypatch.setattr(drill, "_drop", broken)
    server = drill.ExistingServer("postgresql://postgres@scratch/postgres")
    server.start()
    server.stop("agentic_drill_1", "agentic_drill_1_app", "app-owner")
    err = capsys.readouterr().err
    assert "could not clean up" in err and "agentic_drill_1" in err
