"""The operator commands that keep a deployment aligned: `migrate` re-grants the runtime
role and seeds a release's new builtin roles into every tenant; `db grant-runtime` is
idempotent."""

import anyio
import pytest
from sqlalchemy import select

from agentic_cp.__main__ import main
from agentic_cp.audit import read_records
from agentic_cp.db_roles import enforcement_status
from agentic_cp.tables import roles, tenants
from agentic_cp.tenancy.context import tenant_tx

pytestmark = pytest.mark.anyio


@pytest.fixture
def operator_env(api, monkeypatch):
    monkeypatch.setenv("AGENTIC_CP_DATABASE_URL", api.db.app_dsn)
    monkeypatch.setenv("AGENTIC_CP_OWNER_DATABASE_URL", api.db.owner_dsn)
    return api


async def _run(*argv: str) -> int:
    return await anyio.to_thread.run_sync(main, list(argv))


async def test_migrate_seeds_missing_builtin_roles_in_every_tenant(operator_env, capsys):
    api = operator_env
    async with tenant_tx(api.db.owner, api.a.id) as conn:
        await conn.execute(roles.delete().where(roles.c.name == "exec-viewer"))
    async with api.db.owner.begin() as conn:
        before = (
            await conn.execute(select(tenants.c.rbac_revision).where(tenants.c.id == api.a.id))
        ).scalar_one()
    assert await _run("migrate") == 0
    assert "tenant-a: added roles exec-viewer" in capsys.readouterr().out
    async with tenant_tx(api.db.app, api.a.id) as conn:
        names = set((await conn.execute(select(roles.c.name))).scalars())
        assert "exec-viewer" in names
        assert (await read_records(conn))[-1].event == "rbac.seeded"
    async with api.db.owner.begin() as conn:
        after = (
            await conn.execute(select(tenants.c.rbac_revision).where(tenants.c.id == api.a.id))
        ).scalar_one()
    assert after == before + 1
    async with tenant_tx(api.db.app, api.b.id) as conn:
        assert (await read_records(conn))[-1].event != "rbac.seeded"  # b lacked nothing


async def test_grant_runtime_command_is_idempotent(operator_env):
    api = operator_env
    assert await _run("db", "grant-runtime") == 0
    assert await _run("db", "grant-runtime") == 0
    async with api.db.app.connect() as conn:
        assert (await enforcement_status(conn)).enforced
    assert (await api.get("/v1/me", api.a.admin_token)).status_code == 200


async def test_commands_need_the_owner_dsn(api, monkeypatch, capsys):
    monkeypatch.setenv("AGENTIC_CP_DATABASE_URL", api.db.app_dsn)
    monkeypatch.delenv("AGENTIC_CP_OWNER_DATABASE_URL", raising=False)
    assert await _run("admin", "tenant", "list") == 3
    assert "AGENTIC_CP_OWNER_DATABASE_URL" in capsys.readouterr().err
