"""`python -m agentic_cp db backup-role` (M3-05, DF-891): the logical backup's login role,
BYPASSRLS + pg_read_all_data, made by a command instead of hand-written SQL. Every tenant
table forces row-level security on its owner too, so only such a role can dump."""

import secrets

import anyio
import asyncpg
import pytest
from pg import APP_ROLE, OWNER_ROLE
from sqlalchemy.engine import make_url

from agentic_cp.__main__ import main

pytestmark = pytest.mark.anyio


def _role() -> str:
    return "backup_" + secrets.token_hex(4)


async def _drop(db, role: str) -> None:
    admin = await asyncpg.connect(db.admin_dsn)
    try:
        await admin.execute(f"DROP ROLE IF EXISTS {role}")
    finally:
        await admin.close()


def _env(monkeypatch, db, password: str | None) -> None:
    monkeypatch.setenv("AGENTIC_CP_DATABASE_URL", db.app_dsn)
    monkeypatch.setenv("AGENTIC_CP_OWNER_DATABASE_URL", db.owner_dsn)
    monkeypatch.setenv("TEST_PG_ADMIN_DSN", db.admin_dsn)
    if password is None:
        monkeypatch.delenv("TEST_BACKUP_PASSWORD", raising=False)
    else:
        monkeypatch.setenv("TEST_BACKUP_PASSWORD", password)


def _argv(role: str, *extra: str) -> list[str]:
    return [
        "db",
        "backup-role",
        "--role",
        role,
        "--password-env",
        "TEST_BACKUP_PASSWORD",
        "--admin-dsn-env",
        "TEST_PG_ADMIN_DSN",
        *extra,
    ]


async def test_the_command_makes_a_role_that_reads_every_tenant_row(
    two_tenants, monkeypatch, capsys
):
    db, _, _ = two_tenants
    role, password = _role(), "bk-" + secrets.token_hex(12)
    _env(monkeypatch, db, password)
    try:
        assert await anyio.to_thread.run_sync(main, _argv(role)) == 0
        out = capsys.readouterr().out
        assert role in out and "created" in out and password not in out
        dsn = make_url(db.admin_dsn).set(username=role, password=password)
        backup = await asyncpg.connect(dsn.render_as_string(hide_password=False))
        try:
            # forced RLS stops the owner; the backup role bypasses it and reads everything
            assert await backup.fetchval("select count(*) from agentic.tenants") == 2
        finally:
            await backup.close()
        # run again: re-aligned, not an error, and the check passes
        assert await anyio.to_thread.run_sync(main, _argv(role)) == 0
        assert "re-aligned" in capsys.readouterr().out
        assert await anyio.to_thread.run_sync(main, _argv(role, "--check")) == 0
        assert "ready for the backup CronJob" in capsys.readouterr().out
    finally:
        await _drop(db, role)


async def test_check_names_what_a_hand_made_role_lacks(db, monkeypatch, capsys):
    role = _role()
    admin = await asyncpg.connect(db.admin_dsn)
    try:
        await admin.execute(f"CREATE ROLE {role} LOGIN")
    finally:
        await admin.close()
    _env(monkeypatch, db, None)
    try:
        assert await anyio.to_thread.run_sync(main, _argv(role, "--check")) == 1
        out = capsys.readouterr().out
        assert "BYPASSRLS" in out and "pg_read_all_data" in out
    finally:
        await _drop(db, role)


async def test_refusals(db, monkeypatch, capsys):
    _env(monkeypatch, db, None)
    # a new role needs a password, named by its variable (never the command line)
    role = _role()
    assert await anyio.to_thread.run_sync(main, _argv(role)) == 3
    assert "TEST_BACKUP_PASSWORD" in capsys.readouterr().err
    _env(monkeypatch, db, "bk-" + secrets.token_hex(12))
    for own in (OWNER_ROLE, APP_ROLE):  # never the control plane's own roles
        assert await anyio.to_thread.run_sync(main, _argv(own)) == 3
        assert "control plane's own role" in capsys.readouterr().err
    assert await anyio.to_thread.run_sync(main, _argv("Bad-Name")) == 3
    assert "not a role name" in capsys.readouterr().err
    # without the admin DSN variable: say which variable, exit 3
    monkeypatch.delenv("TEST_PG_ADMIN_DSN")
    assert await anyio.to_thread.run_sync(main, _argv(role)) == 3
    assert "TEST_PG_ADMIN_DSN" in capsys.readouterr().err


async def test_a_non_superuser_dsn_says_what_the_dba_runs(db, monkeypatch, capsys):
    """The owner role (CREATEROLE, no BYPASSRLS) may not grant BYPASSRLS: the refusal
    names the statements a DBA runs, and never carries the password's verifier."""
    role, password = _role(), "bk-" + secrets.token_hex(12)
    _env(monkeypatch, db, password)
    monkeypatch.setenv("TEST_PG_ADMIN_DSN", db.owner_dsn)
    try:
        assert await anyio.to_thread.run_sync(main, _argv(role)) == 2
        err = capsys.readouterr().err
        assert "superuser" in err and "GRANT pg_read_all_data" in err, err
        assert "SCRAM-SHA-256$" not in err and password not in err
    finally:
        await _drop(db, role)
