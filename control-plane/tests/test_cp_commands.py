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
    assert "tenant-a: seeded roles exec-viewer" in capsys.readouterr().out
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


async def test_set_policy_repo_sets_one_tenant_and_audits_it(operator_env, capsys):
    api = operator_env
    repo = "https://git.example.test/acme/policy.git"
    argv = ("admin", "tenant", "set-policy-repo", "--tenant", "tenant-a", "--repo", repo)
    assert await _run(*argv, "--branch", "prod") == 0
    async with api.db.owner.begin() as conn:
        rows = dict((await conn.execute(select(tenants.c.slug, tenants.c.policy_repo_url))).all())
    assert rows == {"tenant-a": repo, "tenant-b": None}
    async with tenant_tx(api.db.app, api.a.id) as conn:
        record = (await read_records(conn))[-1]
    assert record.event == "tenant.policy_source_changed" and record.actor == "system:operator"
    assert record.after == {"repo": repo, "branch": "prod"}
    secret = "https://u:" + "p4ss" + "@git.example.test/x.git"
    assert (
        await _run("admin", "tenant", "set-policy-repo", "--tenant", "tenant-a", "--repo", secret)
        == 3
    )
    assert "p4ss" not in capsys.readouterr().err
    local = ("admin", "tenant", "set-policy-repo", "--tenant", "tenant-a", "--repo", "/srv/x")
    assert await _run(*local) == 3
    assert await _run(*argv, "--branch=-bad") == 3


async def test_commands_need_the_owner_dsn(api, monkeypatch, capsys):
    monkeypatch.setenv("AGENTIC_CP_DATABASE_URL", api.db.app_dsn)
    monkeypatch.delenv("AGENTIC_CP_OWNER_DATABASE_URL", raising=False)
    assert await _run("admin", "tenant", "list") == 3
    assert "AGENTIC_CP_OWNER_DATABASE_URL" in capsys.readouterr().err


async def test_set_mail_from_sets_one_tenants_sender_and_audits_it(operator_env):
    api = operator_env
    argv = ("admin", "tenant", "set-mail-from", "--tenant", "tenant-a")
    assert await _run(*argv, "--address", "tools@a.example.test", "--name", "A tools") == 0
    async with api.db.owner.begin() as conn:
        rows = {
            slug: (address, name)
            for slug, address, name in await conn.execute(
                select(tenants.c.slug, tenants.c.mail_from, tenants.c.mail_from_name)
            )
        }
    assert rows == {"tenant-a": ("tools@a.example.test", "A tools"), "tenant-b": (None, None)}
    async with tenant_tx(api.db.app, api.a.id) as conn:
        record = (await read_records(conn))[-1]
    assert record.event == "tenant.mail_from_changed"
    assert record.after == {"address": "tools@a.example.test", "name": "A tools"}
    assert await _run(*argv, "--address", "not-an-address") == 3
    assert await _run(*argv, "--clear") == 0
    async with api.db.owner.begin() as conn:
        found = select(tenants.c.mail_from).where(tenants.c.slug == "tenant-a")
        assert (await conn.execute(found)).scalar_one() is None


async def test_enable_brings_an_offboarded_person_back_for_a_new_invite(operator_env, capsys):
    """Ruling P10: offboarding is not undone through the API; an operator re-enables the
    person, whose old tokens stay revoked, and an admin then invites them again."""
    from apiclient import member

    api = operator_env
    token, _ = await member(api, api.a, "dev@a.example.test", "developer", "payments")
    reply = await api.post(
        "/v1/users/dev@a.example.test/offboard", api.a.admin_token, {"reason": "left"}
    )
    assert reply.status_code == 200
    argv = ("admin", "user", "enable", "--tenant", "tenant-a", "--email", "dev@a.example.test")
    assert await _run(*argv, "--reason", "rehired") == 0
    assert "killswitch off" in capsys.readouterr().out  # the user switch is still on
    assert (await api.get("/v1/me", token)).status_code == 401  # old tokens stay revoked
    async with tenant_tx(api.db.app, api.a.id) as conn:
        record = (await read_records(conn))[-1]
    assert record.event == "user.enabled" and record.reason == "rehired"
    assert await _run(*argv, "--reason", "again") == 2  # already active
    missing = ("admin", "user", "enable", "--tenant", "tenant-a", "--email", "x@a.example.test")
    assert await _run(*missing, "--reason", "r") == 3


async def test_suspend_refuses_a_tenants_tokens_and_resume_restores_them(operator_env):
    """DF-408: a suspended tenant's tokens answer 401 like any invalid token."""
    api = operator_env
    assert await _run("admin", "tenant", "suspend", "--tenant", "tenant-a", "--reason", "x") == 0
    assert (await api.get("/v1/me", api.a.admin_token)).status_code == 401
    assert (await api.get("/v1/me", api.b.admin_token)).status_code == 200
    assert await _run("admin", "tenant", "suspend", "--tenant", "tenant-a", "--reason", "x") == 2
    assert await _run("admin", "tenant", "resume", "--tenant", "tenant-a", "--reason", "ok") == 0
    assert (await api.get("/v1/me", api.a.admin_token)).status_code == 200
    async with tenant_tx(api.db.app, api.a.id) as conn:
        events = [r.event for r in await read_records(conn)]
    assert events[-2:] == ["tenant.suspended", "tenant.resumed"]
    assert await _run("admin", "tenant", "resume", "--tenant", "tenant-a", "--reason", "x") == 2


async def test_set_policy_repo_follows_the_environment_branch(operator_env, monkeypatch, capsys):
    # M3-04: a staging control plane tracks `staging` only
    api = operator_env
    monkeypatch.setenv("AGENTIC_CP_POLICY_BRANCH", "staging")
    repo = "https://git.example.test/acme/policy.git"
    argv = ("admin", "tenant", "set-policy-repo", "--tenant", "tenant-a", "--repo", repo)
    assert await _run(*argv) == 0
    async with api.db.owner.begin() as conn:
        branch = (
            await conn.execute(select(tenants.c.policy_branch).where(tenants.c.slug == "tenant-a"))
        ).scalar_one()
    assert branch == "staging"
    capsys.readouterr()
    assert await _run(*argv, "--branch", "main") == 3
    assert "tracks the policy branch 'staging'" in capsys.readouterr().err
