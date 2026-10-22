"""Repos and CODEOWNERS maintainers per tenant (SP15 Task 8, M2-03; DF-405, DF-418).

Remotes are realistic (`https://git.example.test/acme/...`); `LocalHosts` maps them onto
local repos, so git really runs and no network is used."""

from __future__ import annotations

import logging
from datetime import timedelta

import pytest
from apiclient import check, member
from policy_repos import (
    APP_KEY,
    APP_REMOTE,
    LocalHosts,
    commit,
    make_app_repo,
    make_org_policy_repo,
    url,
    write,
)
from sqlalchemy import select

from agentic_cp.gitops import GitError, GitRunner
from agentic_cp.policy_sync.worker import run_pass
from agentic_cp.tables import audit_log, repo_maintainers, tenants
from agentic_cp.tenancy.context import tenant_tx

pytestmark = pytest.mark.anyio


class MappedGit(LocalHosts, GitRunner):
    pass


@pytest.fixture
async def repos(api, tmp_path):
    """Tenant a with an active policy allowing acme repos, and the app repo on disk."""
    policy = make_org_policy_repo(tmp_path / "policy-a")
    app = make_app_repo(tmp_path / "app", "* @carol\n/.agentic/ @alice\n")
    git = MappedGit(api.settings, {APP_REMOTE: app})
    api.app.state.git = git
    source = {"repo": url(policy), "branch": "main"}
    reply = await api.put("/v1/tenant/policy-source", api.a.admin_token, source)
    assert reply.json()["sync"]["kind"] == "activated", reply.text
    return policy, app, git


async def _handles(api, tenant, email, handles):
    reply = await api.put(f"/v1/users/{email}/handles", tenant.admin_token, {"handles": handles})
    assert reply.status_code == 200, reply.text
    return reply


async def _register(api, token, remote=APP_REMOTE, team="payments"):
    return await api.post("/v1/repos", token, {"remote": remote, "team": team})


async def test_repo_registration_requires_agent_edit_repo_and_allowed_pattern(api, repos):
    developer, _ = await member(api, api.a, "dev@a.example.test", "developer", "payments")
    approver, _ = await member(api, api.a, "ap@a.example.test", "approver", "payments")
    assert (await _register(api, approver)).status_code == 403
    other_team = await _register(api, developer, team="billing")
    assert other_team.status_code in (403, 422)
    outside = await _register(api, developer, "https://git.example.test/other/app.git")
    assert outside.status_code == 422 and outside.json()["code"] == "repo.not_allowed"
    done = await _register(api, developer)
    assert done.status_code == 201, done.text
    body = done.json()
    assert body["repo_key"] == APP_KEY and body["team"] == "payments"
    assert body["codeowners_path"] == ".github/CODEOWNERS"
    again = await _register(api, developer)
    assert again.status_code == 200  # idempotent
    moved = await _register(api, api.a.admin_token, team="billing")
    assert moved.status_code in (409, 422)


async def test_registration_needs_an_active_policy(api):
    reply = await _register(api, api.a.admin_token)
    assert reply.status_code == 409 and reply.json()["code"] == "policy.none"


async def test_repo_registration_refuses_local_urls(api, repos):
    strict = api.with_settings(allow_local_repos=False)
    reply = await _register(strict, api.a.admin_token, "file:///srv/agentic-cp/policy/x")
    assert reply.status_code == 422 and reply.json()["code"] == "repo.bad_url"
    await strict.http.aclose()


async def test_handles_map_host_qualified(api, repos):
    alice, _ = await member(api, api.a, "alice@a.example.test", "developer", "payments")
    await member(api, api.a, "other@a.example.test", "developer", "payments")
    await _handles(api, api.a, "alice@a.example.test", ["git.example.test:alice"])
    await _handles(api, api.a, "other@a.example.test", ["gitlab.com:alice"])
    await _register(api, api.a.admin_token)
    found = (await api.get(f"/v1/repos/{APP_KEY}/maintainers", api.a.admin_token)).json()
    assert found["users"] == ["alice@a.example.test"]
    assert found["commit"]


async def test_handles_resolve_only_within_the_tenant(api, repos, tmp_path):
    for tenant in (api.a, api.b):
        await member(api, tenant, "alice@shared.example.test", "developer", "payments")
        await _handles(api, tenant, "alice@shared.example.test", ["git.example.test:alice"])
    await _register(api, api.a.admin_token)
    async with tenant_tx(api.db.owner, api.b.id) as conn:
        assert (await conn.execute(select(repo_maintainers))).all() == []
    # b's alice is no maintainer of anything: the repo is a's only
    reply = await api.get(f"/v1/repos/{APP_KEY}/maintainers", api.b.admin_token)
    assert reply.status_code == 404


async def test_same_repo_in_two_tenants_has_separate_maintainers(api, repos, tmp_path):
    policy_b = make_org_policy_repo(tmp_path / "policy-b")
    source = {"repo": url(policy_b), "branch": "main"}
    await api.put("/v1/tenant/policy-source", api.b.admin_token, source)
    await member(api, api.a, "alice@a.example.test", "developer", "payments")
    await _handles(api, api.a, "alice@a.example.test", ["git.example.test:alice"])
    await member(api, api.b, "alice@b.example.test", "developer", "payments")
    await _handles(api, api.b, "alice@b.example.test", ["git.example.test:alice"])
    assert (await _register(api, api.a.admin_token)).status_code == 201
    assert (await _register(api, api.b.admin_token)).status_code == 201
    a_found = (await api.get(f"/v1/repos/{APP_KEY}/maintainers", api.a.admin_token)).json()
    b_found = (await api.get(f"/v1/repos/{APP_KEY}/maintainers", api.b.admin_token)).json()
    assert a_found["users"] == ["alice@a.example.test"]
    assert b_found["users"] == ["alice@b.example.test"]


async def test_codeowners_change_updates_maintainers(api, repos):
    _, app, _ = repos
    alice, _ = await member(api, api.a, "alice@a.example.test", "approver", "billing")
    await _handles(api, api.a, "alice@a.example.test", ["git.example.test:alice"])
    await _register(api, api.a.admin_token)
    allowed = await check(api, alice, "agent.approve.repo", repo=APP_KEY)
    assert allowed["allowed"] and allowed["role"] == "repo-maintainer"
    write(app, ".github/CODEOWNERS", "* @carol\n/.agentic/ @dave\n")
    commit(app, "alice leaves")
    refreshed = await api.post(f"/v1/repos/{APP_KEY}/refresh", api.a.admin_token)
    assert refreshed.status_code == 200, refreshed.text
    assert refreshed.json()["maintainers"] == []
    assert refreshed.json()["unresolved"] == []
    denied = await check(api, alice, "agent.approve.repo", repo=APP_KEY)
    assert not denied["allowed"]
    records = (await api.get("/v1/audit", api.a.admin_token)).json()
    synced = [r for r in records if r["event"] == "maintainers.synced"]
    assert synced[-1]["before"] == {"users": ["alice@a.example.test"]}
    assert synced[-1]["after"]["users"] == []


async def test_maintainer_allows_repo_permissions_only_for_that_repo(api, repos):
    alice, _ = await member(api, api.a, "alice@a.example.test", "approver", "billing")
    await _handles(api, api.a, "alice@a.example.test", ["git.example.test:alice"])
    await _register(api, api.a.admin_token)
    assert (await check(api, alice, "agent.approve.repo", repo=APP_KEY))["allowed"]
    other = await check(api, alice, "agent.approve.repo", repo="git.example.test/acme/other")
    assert not other["allowed"]
    team = await check(api, alice, "agent.publish.team", team="payments")
    assert not team["allowed"]  # a maintainership grants repo permissions only


async def test_check_takes_a_registered_repos_team_from_the_table(api, repos):
    developer, _ = await member(api, api.a, "dev@a.example.test", "developer", "payments")
    await _register(api, api.a.admin_token)
    mismatch = await api.post(
        "/v1/rbac/check",
        developer,
        {"permission": "run.trigger", "repo": APP_KEY, "team": "billing"},
    )
    assert mismatch.status_code == 422 and mismatch.json()["code"] == "rbac.repo_team_mismatch"
    found = await check(api, developer, "run.trigger", repo=APP_KEY)
    assert found["allowed"] and found["scope"] == "team:payments"
    unregistered = await check(
        api, developer, "agent.edit.repo", repo="git.example.test/acme/new", team="payments"
    )
    assert unregistered["allowed"]  # an unregistered repo takes the claimed team


async def test_handles_require_rbac_assign_and_rebuild_maintainers(api, repos):
    developer, _ = await member(api, api.a, "dev@a.example.test", "developer", "payments")
    alice, _ = await member(api, api.a, "alice@a.example.test", "approver", "billing")
    await _register(api, api.a.admin_token)
    assert not (await check(api, alice, "agent.approve.repo", repo=APP_KEY))["allowed"]
    refused = await api.put(
        "/v1/users/alice@a.example.test/handles", developer, {"handles": ["x.test:alice"]}
    )
    assert refused.status_code == 403
    bad = await api.put(
        "/v1/users/alice@a.example.test/handles", api.a.admin_token, {"handles": ["alice"]}
    )
    assert bad.status_code == 422
    await _handles(api, api.a, "alice@a.example.test", ["git.example.test:alice"])
    assert (await check(api, alice, "agent.approve.repo", repo=APP_KEY))["allowed"]


async def test_me_lists_derived_maintainerships(api, repos):
    alice, _ = await member(api, api.a, "alice@a.example.test", "approver", "billing")
    await _handles(api, api.a, "alice@a.example.test", ["git.example.test:alice"])
    await _register(api, api.a.admin_token)
    me = (await api.get("/v1/me", alice)).json()
    derived = [a for a in me["assignments"] if a.get("derived")]
    assert derived == [
        {"role": "repo-maintainer", "scope": "repo", "team": None, "repo": APP_KEY, "derived": True}
    ]
    assert "agent.approve.repo" in me["permissions"]["repos"][APP_KEY]
    assert me["permissions"]["repo_teams"][APP_KEY] == "payments"


class LeakyOfflineGit(GitRunner):
    """Fails like a host that echoes the URL, credentials included."""

    def _spawn(self, argv, env, cwd, timeout):
        secret = "tok" + "123"
        raise GitError(f"fatal: unable to access 'https://u:{secret}@git.example.test/x.git/'")


async def test_git_errors_never_show_credentials(api, repos, caplog):
    api.app.state.git = LeakyOfflineGit(api.settings)
    with caplog.at_level(logging.DEBUG):
        reply = await _register(api, api.a.admin_token)
    assert reply.status_code == 502 and reply.json()["code"] == "repo.fetch_failed"
    secret = "tok" + "123"
    assert secret not in reply.text
    assert all(secret not in record.getMessage() for record in caplog.records)
    async with tenant_tx(api.db.owner, api.a.id) as conn:
        assert all(secret not in str(row) for row in await conn.execute(select(audit_log)))


async def test_worker_refreshes_due_repos(api, repos):
    _, app, git = repos
    alice, _ = await member(api, api.a, "alice@a.example.test", "approver", "billing")
    await _handles(api, api.a, "alice@a.example.test", ["git.example.test:alice"])
    await _register(api, api.a.admin_token)
    write(app, ".github/CODEOWNERS", "/.agentic/ @dave\n")
    commit(app, "alice leaves")
    await run_pass(api.db.app, git, api.settings, api.clock)
    assert (await check(api, alice, "agent.approve.repo", repo=APP_KEY))["allowed"]  # not due
    api.clock.advance(timedelta(minutes=api.settings.codeowners_refresh_minutes + 1))
    await run_pass(api.db.app, git, api.settings, api.clock)
    assert not (await check(api, alice, "agent.approve.repo", repo=APP_KEY))["allowed"]


async def test_tenant_row_keeps_no_repo_state(api, repos):
    """Repos live in tenant-owned tables only (the tenants row is unchanged)."""
    async with api.db.owner.begin() as conn:
        before = (await conn.execute(select(tenants).where(tenants.c.id == api.b.id))).one()
    await _register(api, api.a.admin_token)
    async with api.db.owner.begin() as conn:
        after = (await conn.execute(select(tenants).where(tenants.c.id == api.b.id))).one()
    assert before == after
