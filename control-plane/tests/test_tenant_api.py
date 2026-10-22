"""`/v1/tenant` (SP15 Task 7): the caller's tenant and its policy source. A source URL with
credentials (ND-15) or one that reaches the server's own disk (R12) is refused."""

from __future__ import annotations

import logging

import pytest
from apiclient import member
from policy_repos import make_policy_repo, url
from sqlalchemy import select

from agentic_cp.gitops import GitError, GitRunner
from agentic_cp.tables import audit_log, tenants
from agentic_cp.tenancy.context import tenant_tx

pytestmark = pytest.mark.anyio

SECRET = "tok" + "123secret"  # built from pieces: not a real token shape


async def test_tenant_info_names_the_callers_tenant_only(api):
    reply = await api.get("/v1/tenant", api.a.admin_token)
    assert reply.status_code == 200
    assert reply.json() == {
        "slug": "tenant-a",
        "name": "Tenant A",
        "policy_repo": None,
        "policy_branch": "main",
        "webhook_configured": False,
    }
    assert (await api.get("/v1/tenant", api.b.admin_token)).json()["slug"] == "tenant-b"


async def test_policy_source_change_requires_policy_edit_org_and_is_audited(api, tmp_path):
    repo = make_policy_repo(tmp_path / "policy-a")
    developer, _ = await member(api, api.a, "dev@a.example.test", "developer", "payments")
    body = {"repo": url(repo), "branch": "main"}
    refused = await api.put("/v1/tenant/policy-source", developer, body)
    assert refused.status_code == 403
    done = await api.put("/v1/tenant/policy-source", api.a.admin_token, body)
    assert done.status_code == 200, done.text
    assert done.json()["sync"]["kind"] == "activated"
    records = (await api.get("/v1/audit", api.a.admin_token)).json()
    changed = [r for r in records if r["event"] == "tenant.policy_source_changed"]
    assert changed and changed[0]["before"] == {"repo": None, "branch": "main"}
    assert changed[0]["after"] == body
    assert (await api.get("/v1/tenant", api.b.admin_token)).json()["policy_repo"] is None


@pytest.mark.parametrize(
    "bad",
    [
        f"https://user:{SECRET}@git.example.test/org/policy.git",
        f"https://{SECRET}@git.example.test/org/policy.git",
    ],
)
async def test_policy_source_refuses_credentials_in_url(api, bad, caplog):
    with caplog.at_level(logging.DEBUG):
        reply = await api.put(
            "/v1/tenant/policy-source", api.a.admin_token, {"repo": bad, "branch": "main"}
        )
    assert reply.status_code == 422
    assert reply.json()["code"] == "policy.credentials_in_url"
    assert SECRET not in reply.text
    assert all(SECRET not in record.getMessage() for record in caplog.records)
    async with tenant_tx(api.db.owner, api.a.id) as conn:
        stored = select(tenants.c.policy_repo_url).where(tenants.c.id == api.a.id)
        assert (await conn.execute(stored)).scalar_one() is None
        texts = [str(row) for row in await conn.execute(select(audit_log))]
    assert all(SECRET not in text for text in texts)


@pytest.mark.parametrize(
    "bad",
    [
        "file:///srv/agentic-cp/policy/other-tenant",
        "/srv/agentic-cp/policy/other-tenant",
        "C:\\agentic\\policy",
        "--upload-pack=touch /tmp/x",
        "ext::sh -c touch% /tmp/x",
        "http://git.example.test/org/policy.git",
        "ssh://-oProxyCommand=touch/x",
        "git@-oProxyCommand:x",
    ],
)
async def test_policy_source_refuses_local_and_option_urls(api, bad):
    strict = api.with_settings(allow_local_repos=False)
    reply = await strict.put(
        "/v1/tenant/policy-source", api.a.admin_token, {"repo": bad, "branch": "main"}
    )
    assert reply.status_code == 422, reply.text
    assert reply.json()["code"] == "repo.bad_url"
    await strict.http.aclose()


@pytest.mark.parametrize("branch", ["-x", "main..x", "a b", "refs/../x", ""])
async def test_policy_source_refuses_a_bad_branch(api, branch):
    reply = await api.put(
        "/v1/tenant/policy-source",
        api.a.admin_token,
        {"repo": "https://git.example.test/org/policy.git", "branch": branch},
    )
    assert reply.status_code == 422, reply.text


class OfflineGit(GitRunner):
    """Every git command fails as if the host were unreachable (no network in tests)."""

    def _spawn(self, argv, env, cwd, timeout):
        raise GitError("could not resolve host")


async def test_remote_urls_are_accepted_when_local_repos_are_not(api):
    strict = api.with_settings(allow_local_repos=False)
    strict.app.state.git = OfflineGit(strict.settings)
    for good in ("https://git.example.test/org/policy.git", "git@git.example.test:org/p.git"):
        reply = await strict.put(
            "/v1/tenant/policy-source", api.a.admin_token, {"repo": good, "branch": "main"}
        )
        assert reply.status_code == 200, reply.text
        assert reply.json()["sync"]["kind"] == "failed"  # nothing listens there
    await strict.http.aclose()
