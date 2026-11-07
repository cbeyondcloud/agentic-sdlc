"""The git host allow-list at work (DF-484, DF-503; owner decision 2026-11-07): repo
registration, the policy source and every fetch refuse a host the org policy does not
allow, and git's own error line reaches the log, never the caller.

Remotes are realistic; `LocalHosts` maps them onto local repos and `resolve` is faked, so
git really runs and no network or DNS is used."""

from __future__ import annotations

import logging

import pytest
from policy_repos import ORG_YAML, LocalHosts, commit, make_app_repo, make_policy_repo, url, write
from sqlalchemy import update

from agentic_cp.gitops import GIT_FAILED, GitError, GitRunner
from agentic_cp.policy_sync.service import sync_once
from agentic_cp.tables import tenants
from agentic_cp.tenancy.context import tenant_tx

pytestmark = pytest.mark.anyio

GITHUB_APP = "https://github.com/acme/app.git"
INTERNAL_APP = "https://git.example.test/acme/app.git"
POLICY_REMOTE = "https://git.example.test/acme/policy.git"
#: Patterns that allow every acme repo on any host: only the host list decides here.
ANY_HOST = "allowed_repo_patterns: ['*/acme/*']\nallow_repo_pipelines: false\n"


class FakeDns(LocalHosts, GitRunner):
    """Mapped remotes, and a resolver answering from `answers` (host -> addresses)."""

    def __init__(self, settings, hosts, answers):
        super().__init__(settings, hosts)
        self.answers = answers
        self.resolved: list[str] = []
        self.fetched: list[str] = []

    def resolve(self, host):
        self.resolved.append(host)
        return self.answers.get(host, [])

    def _spawn(self, argv, env, cwd, timeout):
        if "fetch" in argv:
            self.fetched.append(argv[-2] if argv[-1] == "HEAD" else argv[-1])
        return super()._spawn(argv, env, cwd, timeout)


def policy_repo(path, org_yaml: str):
    repo = make_policy_repo(path, teams=("payments",))
    write(repo, "policy/org.yaml", org_yaml)
    commit(repo, "org policy")
    return repo


@pytest.fixture
def app(tmp_path):
    return make_app_repo(tmp_path / "app", "/.agentic/ @alice\n")


async def _source(api, repo_url):
    reply = await api.put(
        "/v1/tenant/policy-source", api.a.admin_token, {"repo": repo_url, "branch": "main"}
    )
    assert reply.status_code == 200, reply.text
    git = api.app.state.git
    if isinstance(git, FakeDns):  # what the policy sync fetched is not what a test looks at
        git.fetched.clear()
    return reply.json()["sync"]


async def _register(api, remote):
    return await api.post("/v1/repos", api.a.admin_token, {"remote": remote, "team": "payments"})


async def test_registration_refuses_a_host_the_policy_does_not_list(api, tmp_path, app):
    git = FakeDns(api.settings, {INTERNAL_APP: app}, {"git.example.test": ["10.0.0.7"]})
    api.app.state.git = git
    policy = policy_repo(tmp_path / "policy", ANY_HOST + "allowed_git_hosts: [github.com]\n")
    assert (await _source(api, url(policy)))["kind"] == "activated"
    reply = await _register(api, INTERNAL_APP)
    assert reply.status_code == 422 and reply.json()["code"] == "repo.host_not_allowed"
    assert "allowed_git_hosts" in reply.json()["detail"]
    assert git.resolved == [] and git.fetched == []  # refused before any lookup or fetch


async def test_a_listed_host_is_fetched_whatever_it_resolves_to(api, tmp_path, app):
    git = FakeDns(api.settings, {INTERNAL_APP: app}, {"git.example.test": ["10.0.0.7"]})
    api.app.state.git = git
    policy = policy_repo(tmp_path / "policy", ANY_HOST + "allowed_git_hosts: [git.example.test]\n")
    await _source(api, url(policy))
    reply = await _register(api, INTERNAL_APP)
    assert reply.status_code == 201, reply.text
    assert git.resolved == []


async def test_by_default_github_is_allowed_on_public_addresses_only(api, tmp_path, app):
    """No `allowed_git_hosts` (every policy before the rule): github.com and the hosts of
    repos already registered, while they resolve to public addresses."""
    answers = {"github.com": ["140.82.112.3"], "git.example.test": ["140.82.112.4"]}
    git = FakeDns(api.settings, {GITHUB_APP: app, INTERNAL_APP: app}, answers)
    api.app.state.git = git
    await _source(api, url(policy_repo(tmp_path / "policy", ANY_HOST)))
    unlisted = await _register(api, INTERNAL_APP)
    assert unlisted.status_code == 422 and unlisted.json()["code"] == "repo.host_not_allowed"
    assert (await _register(api, GITHUB_APP)).status_code == 201
    # DNS changed: the next fetch is refused before git looks the host up
    answers["github.com"] = ["::ffff:127.0.0.1"]
    git.fetched.clear()
    refreshed = await api.post("/v1/repos/github.com/acme/app/refresh", api.a.admin_token)
    assert refreshed.status_code == 502 and refreshed.json()["code"] == "repo.fetch_failed"
    assert "allowed_git_hosts" in refreshed.json()["detail"]
    assert "127.0.0.1" not in refreshed.text
    assert git.fetched == []


async def test_by_default_a_private_github_answer_is_refused_at_registration(api, tmp_path, app):
    git = FakeDns(api.settings, {GITHUB_APP: app}, {"github.com": ["10.1.2.3"]})
    api.app.state.git = git
    await _source(api, url(policy_repo(tmp_path / "policy", ANY_HOST)))
    reply = await _register(api, GITHUB_APP)
    assert reply.status_code == 422 and reply.json()["code"] == "repo.host_not_allowed"
    assert "10.1.2.3" not in reply.text
    assert git.fetched == []


async def test_an_empty_list_allows_no_default_host(api, tmp_path, app):
    git = FakeDns(api.settings, {GITHUB_APP: app}, {"github.com": ["140.82.112.3"]})
    api.app.state.git = git
    policy = policy_repo(tmp_path / "policy", ANY_HOST + "allowed_git_hosts: []\n")
    await _source(api, url(policy))
    reply = await _register(api, GITHUB_APP)
    assert reply.status_code == 422 and reply.json()["code"] == "repo.host_not_allowed"


async def test_the_policy_source_must_name_an_allowed_host(api, tmp_path):
    git = FakeDns(api.settings, {}, {})
    api.app.state.git = git
    policy = policy_repo(tmp_path / "policy", ANY_HOST + "allowed_git_hosts: [github.com]\n")
    await _source(api, url(policy))
    reply = await api.put(
        "/v1/tenant/policy-source",
        api.a.admin_token,
        {"repo": "https://169.254.169.254/acme/policy.git", "branch": "main"},
    )
    assert reply.status_code == 422 and reply.json()["code"] == "repo.host_not_allowed"


async def _point_at(api, remote):
    """As the operator's `admin tenant set-policy-repo` does: straight into the row."""
    async with tenant_tx(api.db.owner, api.a.id) as conn:
        await conn.execute(
            update(tenants).where(tenants.c.id == api.a.id).values(policy_repo_url=remote)
        )


async def test_the_policy_repos_own_host_is_allowed_by_default(api, tmp_path):
    """An existing install whose policy repo is on the org's own (private) git server keeps
    syncing without listing anything."""
    policy = policy_repo(tmp_path / "policy", ORG_YAML.replace("allowed_git_hosts", "# x"))
    git = FakeDns(api.settings, {POLICY_REMOTE: policy}, {"git.example.test": ["10.0.0.7"]})
    await _point_at(api, POLICY_REMOTE)
    outcome = await sync_once(api.db.app, git, api.a.id, api.settings, api.clock)
    assert outcome.kind == "activated", outcome
    assert git.resolved == []


async def test_the_policy_repo_never_locks_itself_out(api, tmp_path):
    """A list that leaves out the policy repo's host still lets it sync: the operator (or
    an org admin, under the list then active) chose that host."""
    policy = policy_repo(tmp_path / "policy", ANY_HOST + "allowed_git_hosts: [github.com]\n")
    git = FakeDns(api.settings, {POLICY_REMOTE: policy}, {"git.example.test": ["10.0.0.7"]})
    await _point_at(api, POLICY_REMOTE)
    for _ in range(2):  # the second sync reads the list the first one activated
        write(policy, "NOTES.md", f"version {_}\n")
        commit(policy, "another version")
        outcome = await sync_once(api.db.app, git, api.a.id, api.settings, api.clock)
        assert outcome.kind == "activated", outcome
    assert git.resolved == []


class LoudOfflineGit(GitRunner):
    """Fails as git does when an internal host refuses the connection."""

    def _spawn(self, argv, env, cwd, timeout):
        if "fetch" in argv:
            raise GitError("fatal: unable to access 'https://10.9.8.7/': Connection refused")
        return super()._spawn(argv, env, cwd, timeout)


async def test_git_error_lines_are_logged_not_returned(api, tmp_path, caplog):
    api.app.state.git = LoudOfflineGit(api.settings)
    with caplog.at_level(logging.WARNING):
        sync = await _source(api, url(policy_repo(tmp_path / "policy", ANY_HOST)))
    assert sync["kind"] == "failed" and sync["problems"] == [GIT_FAILED]
    assert any("Connection refused" in record.getMessage() for record in caplog.records)
