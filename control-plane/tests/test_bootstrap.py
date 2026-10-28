"""Operator commands (ND-4): `admin tenant create` bootstraps a tenant and its first org
admin; `admin issue-token` recovers access within one named tenant. Each acts inside the
affected tenant's context and is audited there as `system:operator`."""

import re

import anyio
import pytest
from sqlalchemy import func, select

from agentic.orgapi import TOKEN_PREFIX
from agentic_cp.__main__ import main
from agentic_cp.audit import read_records, verify
from agentic_cp.tables import roles, tenants
from agentic_cp.tenancy.context import tenant_tx

pytestmark = pytest.mark.anyio

TOKEN_LINE = re.compile(rf"^{TOKEN_PREFIX}\S+$", re.MULTILINE)


async def _run(*argv: str) -> int:
    return await anyio.to_thread.run_sync(main, list(argv))


@pytest.fixture
def operator_env(db, monkeypatch):
    monkeypatch.setenv("AGENTIC_CP_DATABASE_URL", db.app_dsn)
    monkeypatch.setenv("AGENTIC_CP_OWNER_DATABASE_URL", db.owner_dsn)
    return db


async def _create(slug: str, email: str, *extra: str) -> int:
    return await _run(
        "admin",
        "tenant",
        "create",
        "--slug",
        slug,
        "--name",
        slug.title(),
        "--admin-email",
        email,
        *extra,
    )


async def test_tenant_create_bootstraps_tenant_and_first_admin(operator_env, capsys, api_for):
    db = operator_env
    assert await _create("acme", "Root@Acme.example.test") == 0
    out = capsys.readouterr().out
    found = TOKEN_LINE.findall(out)
    assert len(found) == 1, "the token is printed exactly once"
    token = found[0]
    client = api_for(db)
    me = (await client.get("/v1/me", token)).json()
    assert me["user"]["email"] == "root@acme.example.test"
    assert me["tenant"]["slug"] == "acme"
    admin = {"role": "org-admin", "scope": "org", "team": None, "repo": None, "derived": False}
    assert admin in me["assignments"]
    async with db.owner.begin() as conn:
        tenant_id = (
            await conn.execute(select(tenants.c.id).where(tenants.c.slug == "acme"))
        ).scalar_one()
    async with tenant_tx(db.app, tenant_id) as conn:
        records = await read_records(conn)
        assert [r.event for r in records[:2]] == ["tenant.created", "rbac.bootstrap"]
        assert all(r.actor == "system:operator" for r in records)
        assert await verify(conn) == []
        builtin = (await conn.execute(select(func.count()).select_from(roles))).scalar_one()
        assert builtin >= 8
    assert token not in str(records)


async def test_tenant_create_refuses_a_used_slug(operator_env, capsys):
    db = operator_env
    assert await _create("acme", "one@acme.example.test") == 0
    capsys.readouterr()
    assert await _create("acme", "two@acme.example.test") == 2
    captured = capsys.readouterr()
    assert TOKEN_LINE.findall(captured.out) == []
    async with db.owner.begin() as conn:
        count = await conn.execute(select(func.count()).select_from(tenants))
        assert count.scalar_one() == 1


async def test_tenant_create_refuses_credentials_in_the_policy_repo_url(operator_env, capsys):
    url = "https://user:" + "pw" + "@git.example.test/org/policy.git"
    assert await _create("acme", "one@acme.example.test", "--policy-repo", url) == 3
    assert "pw@" not in capsys.readouterr().err


async def test_tenant_list_shows_tenants(operator_env, capsys):
    await _create("acme", "one@acme.example.test", "--policy-repo", "https://git.example.test/p")
    await _create("beta", "one@beta.example.test")
    capsys.readouterr()
    assert await _run("admin", "tenant", "list") == 0
    out = capsys.readouterr().out
    assert "acme" in out and "beta" in out and "https://git.example.test/p" in out


async def test_issue_token_is_scoped_to_the_named_tenant(operator_env, capsys, api_for):
    db = operator_env
    await _create("acme", "same@example.test")
    await _create("beta", "same@example.test")
    capsys.readouterr()
    assert (
        await _run("admin", "issue-token", "--tenant", "beta", "--email", "same@example.test") == 0
    )
    (token,) = TOKEN_LINE.findall(capsys.readouterr().out)
    me = (await api_for(db).get("/v1/me", token)).json()
    assert me["tenant"]["slug"] == "beta"
    assert await _run("admin", "issue-token", "--tenant", "beta", "--email", "nobody@x.test") == 3
