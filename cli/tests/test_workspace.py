"""Where a command's inputs come from: repository, policy repo, team, user (M1-54)."""

from pathlib import Path

import pytest
from _cli_support import USER, git, write

from agentic_cli.errors import CliError
from agentic_cli.exit_codes import ExitCode
from agentic_cli.workspace import Workspace, find_repo


def test_the_repository_is_found_from_a_subfolder(repo: Path):
    assert find_repo(repo / "src") == repo


def test_outside_a_repository_is_refused(tmp_path: Path):
    with pytest.raises(CliError) as caught:
        find_repo(tmp_path)
    assert caught.value.code == ExitCode.REFUSED and "git repository" in str(caught.value)


def test_policy_repo_comes_from_the_environment_first(repo, policy_repo, tmp_path):
    env = {"AGENTIC_POLICY_REPO": str(policy_repo)}
    ws = Workspace.load(repo, env=env, home=tmp_path / "home")
    assert ws.policy_repo == policy_repo
    assert ws.policy().version.startswith(("git:", "sha256:"))
    assert sorted(ws.definitions().agents) == ["architect", "ba"]


def test_policy_repo_from_user_config_then_org_cache_then_standalone(repo, policy_repo, tmp_path):
    home = tmp_path / "home"
    write(home / "config.yaml", f"policy_repo: {policy_repo.as_posix()}\n")
    assert Workspace.load(repo, env={}, home=home).policy_repo == policy_repo

    write(home / "config.yaml", "org: acme\n")
    cached = home / "policy" / "acme"
    cached.mkdir(parents=True)
    assert Workspace.load(repo, env={}, home=home).policy_repo == cached

    write(home / "config.yaml", "")
    local = repo / ".agentic" / "local" / "policy"
    local.mkdir(parents=True)
    assert Workspace.load(repo, env={}, home=home).policy_repo == local


def test_no_policy_is_refused_with_a_hint(repo, tmp_path):
    with pytest.raises(CliError) as caught:
        Workspace.load(repo, env={}, home=tmp_path / "home").policy()
    assert caught.value.code == ExitCode.REFUSED
    assert "agentic init" in caught.value.hint


def test_team_layer_and_repo_layer_are_read(repo, policy_repo, tmp_path):
    write(
        policy_repo / "teams" / "payments" / "agents" / "ba.md",
        "---\nname: ba\ntype: analyst\nversion: 9.0.0\ndescription: Team BA.\n---\nTeam.\n",
    )
    write(
        repo / ".agentic" / "agents" / "helper.md",
        "---\nname: helper\ntype: general\nversion: 0.1.0\ndescription: Repo agent.\n---\nHi.\n",
    )
    env = {"AGENTIC_POLICY_REPO": str(policy_repo), "AGENTIC_TEAM": "payments"}
    ws = Workspace.load(repo, env=env, home=tmp_path / "home")
    definitions = ws.definitions()
    assert definitions.agent("ba").version == "9.0.0"
    assert definitions.agent_source("helper") == "repo"
    assert ws.context().team == "payments"


def test_classification_comes_from_the_repo_config(repo, policy_repo, tmp_path):
    write(repo / ".agentic" / "config.yaml", "classification: restricted\n")
    env = {"AGENTIC_POLICY_REPO": str(policy_repo)}
    assert Workspace.load(repo, env=env, home=tmp_path / "h").context().repo_classification == (
        "restricted"
    )


def test_a_broken_policy_is_invalid_naming_the_file(repo, policy_repo, tmp_path):
    write(policy_repo / "policy" / "tiers.yml", "tiers: {}\n")
    env = {"AGENTIC_POLICY_REPO": str(policy_repo)}
    with pytest.raises(CliError) as caught:
        Workspace.load(repo, env=env, home=tmp_path / "home").policy()
    assert caught.value.code == ExitCode.INVALID and "tiers.yml" in str(caught.value)


def test_user_from_env_then_config_then_git(repo, policy_repo, tmp_path):
    home = tmp_path / "home"
    assert Workspace.load(repo, env={"AGENTIC_USER": "a@x.example"}, home=home).user() == (
        "a@x.example"
    )
    write(home / "config.yaml", "user: b@x.example\n")
    assert Workspace.load(repo, env={}, home=home).user() == "b@x.example"
    write(home / "config.yaml", "")
    assert Workspace.load(repo, env={}, home=home).user() == USER


def test_no_user_is_refused(repo, tmp_path, monkeypatch):
    git(repo, "config", "--unset", "user.email")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "no-global"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    with pytest.raises(CliError) as caught:
        Workspace.load(repo, env={}, home=tmp_path / "home").user()
    assert caught.value.code == ExitCode.REFUSED and "user.email" in caught.value.hint
