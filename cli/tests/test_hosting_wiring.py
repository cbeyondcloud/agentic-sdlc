"""The host registry (M1-58): which provider serves `origin`, with which API URL and token."""

from pathlib import Path

import pytest
from cli_helpers import git, make_repo

from agentic.auth import Credentials
from agentic.hosting import RepoRef
from agentic_cli.errors import CliError
from agentic_cli.exit_codes import ExitCode
from agentic_cli.hosting import connect, host_configs

TOKEN = "ghp_" + "wiring" + "Fake0123456789"  # gitleaks:allow - built from pieces
OTHER = "ghp_" + "keyring" + "Fake0123456789"  # gitleaks:allow - built from pieces


class Ring:
    def __init__(self, stored: dict[str, str] | None = None) -> None:
        self.stored = stored or {}

    def get_password(self, service, username):
        return self.stored.get(username)

    def set_password(self, service, username, password):
        self.stored[username] = password


def creds(env: dict | None = None, ring: dict | None = None) -> Credentials:
    return Credentials(environ=env or {}, keyring=Ring(ring))


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return make_repo(tmp_path / "app")


def test_github_com_is_built_in(repo):
    with connect(repo, {}, credentials=creds({"AGENTIC_GITHUB_TOKEN": TOKEN})) as found:
        assert (found.kind, found.repo) == ("github", RepoRef("github.com", "acme", "app"))
        assert found.provider.api_url == "https://api.github.com"
        assert found.token_source == "env"
        assert TOKEN not in repr(found)


def test_an_enterprise_host_comes_from_the_user_config(repo):
    git(repo, "remote", "set-url", "origin", "git@ghe.example.com:team/svc.git")
    settings = {"git_hosts": {"GHE.example.com": {"kind": "github"}}}
    with connect(repo, settings, credentials=creds({"AGENTIC_GITHUB_TOKEN": TOKEN})) as found:
        assert found.provider.api_url == "https://ghe.example.com/api/v3"
        assert found.repo == RepoRef("ghe.example.com", "team", "svc")


def test_an_enterprise_host_may_name_its_api_url_and_credential(repo):
    git(repo, "remote", "set-url", "origin", "https://ghe.example.com/team/svc.git")
    settings = {
        "git_hosts": {
            "ghe.example.com": {
                "kind": "github",
                "api_url": "https://api.ghe.example.com/",
                "credential": "ghe_token",
            }
        }
    }
    found = connect(repo, settings, credentials=creds({"AGENTIC_GHE_TOKEN": TOKEN}))
    assert found.provider.api_url == "https://api.ghe.example.com"
    assert found.token == TOKEN
    found.close()


def test_the_environment_wins_over_the_keyring(repo):
    env, ring = {"AGENTIC_GITHUB_TOKEN": TOKEN}, {"github_token": OTHER}
    with connect(repo, {}, credentials=creds(env, ring)) as found:
        assert (found.token, found.token_source) == (TOKEN, "env")
    with connect(repo, {}, credentials=creds({}, ring)) as found:
        assert (found.token, found.token_source) == (OTHER, "keyring")


def test_an_unknown_host_is_refused_and_no_token_is_read(repo):
    git(repo, "remote", "set-url", "origin", "https://git.example.com/a/b.git")
    with pytest.raises(CliError) as raised:
        connect(repo, {}, credentials=creds({"AGENTIC_GITHUB_TOKEN": TOKEN}))
    assert raised.value.code == ExitCode.REFUSED
    assert "git.example.com" in str(raised.value) and "git_hosts" in raised.value.hint


def test_a_missing_token_says_how_to_provide_one(repo):
    with pytest.raises(CliError) as raised:
        connect(repo, {}, credentials=creds())
    assert raised.value.code == ExitCode.REFUSED
    assert "AGENTIC_GITHUB_TOKEN" in str(raised.value) + raised.value.hint
    assert "agentic login --for github" in raised.value.hint
    # the message alone (all an engine pause reason keeps) names the right login too
    assert "agentic login --for github" in str(raised.value)
    assert "`agentic login` " not in str(raised.value)


def test_a_missing_enterprise_token_names_its_host_login(repo):
    git(repo, "remote", "set-url", "origin", "https://ghe.example.com/team/svc.git")
    settings = {"git_hosts": {"ghe.example.com": {"kind": "github", "credential": "ghe_token"}}}
    with pytest.raises(CliError) as raised:
        connect(repo, settings, credentials=creds())
    assert "agentic login --for ghe.example.com" in str(raised.value)
    assert "AGENTIC_GHE_TOKEN" in str(raised.value)


def test_a_local_origin_is_not_a_hosted_repository(repo, tmp_path):
    git(repo, "remote", "set-url", "origin", str(tmp_path / "bare.git"))
    with pytest.raises(CliError, match="local path") as raised:
        connect(repo, {}, credentials=creds({"AGENTIC_GITHUB_TOKEN": TOKEN}))
    assert raised.value.code == ExitCode.REFUSED


def test_no_origin_is_refused(tmp_path):
    bare = make_repo(tmp_path / "solo", origin=None)
    with pytest.raises(CliError, match="no remote"):
        connect(bare, {}, credentials=creds({"AGENTIC_GITHUB_TOKEN": TOKEN}))


@pytest.mark.parametrize(
    "hosts",
    [
        {"ghe.example.com": {"kind": "svn"}},
        {"ghe.example.com": "github"},
        {"ghe.example.com": {"kind": "github", "colour": "red"}},
        {"ghe.example.com": {"kind": "github", "credential": "Not A Name"}},
        ["ghe.example.com"],
    ],
)
def test_a_bad_git_hosts_entry_is_invalid(hosts):
    with pytest.raises(CliError) as raised:
        host_configs({"git_hosts": hosts})
    assert raised.value.code == ExitCode.INVALID


def test_the_push_url_does_not_change_which_repository_is_meant(repo, tmp_path):
    git(repo, "remote", "set-url", "--push", "origin", str(tmp_path / "bare.git"))
    with connect(repo, {}, credentials=creds({"AGENTIC_GITHUB_TOKEN": TOKEN})) as found:
        assert found.repo == RepoRef("github.com", "acme", "app")
