"""Where a command's inputs come from: repository, policy repo, team, user (M1-54)."""

import json
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


def test_the_org_cache_is_read_where_init_writes_it(repo, tmp_path):
    home = tmp_path / "home"
    write(home / "config.yaml", "org: acme\n")
    (home / "policy" / "acme" / "repo" / "policy").mkdir(parents=True)
    assert Workspace.load(repo, env={}, home=home).policy_repo == home / "policy" / "acme" / "repo"


def test_the_installed_standalone_policy_comes_last(repo, tmp_path):
    home = tmp_path / "home"
    installed = home / "policy" / "standalone"
    installed.mkdir(parents=True)
    assert Workspace.load(repo, env={}, home=home).policy_repo == installed
    local = repo / ".agentic" / "local" / "policy"
    local.mkdir(parents=True)
    assert Workspace.load(repo, env={}, home=home).policy_repo == local


def test_an_org_user_never_falls_back_to_a_personal_policy(repo, tmp_path):
    """With `org:` set and its cache missing, the template policy (no org governance)
    is never used in its place, silently (review F3): the org policy is reported missing."""
    home = tmp_path / "home"
    write(home / "config.yaml", "org: acme\n")
    (home / "policy" / "standalone" / "policy").mkdir(parents=True)
    (repo / ".agentic" / "local" / "policy").mkdir(parents=True)
    ws = Workspace.load(repo, env={}, home=home)
    assert ws.policy_repo == home / "policy" / "acme" / "repo"
    with pytest.raises(CliError) as caught:
        ws.policy()
    assert caught.value.code == ExitCode.REFUSED
    assert "no org policy found" in str(caught.value) and "agentic init" in caught.value.hint


def test_an_org_users_policy_repo_url_is_the_source_and_runs_read_the_cache(repo, tmp_path):
    """The documented org config names the repo init clones from (a URL); later commands
    read init's cache, never the URL as a folder (DF-274)."""
    home = tmp_path / "home"
    write(
        home / "config.yaml",
        "org: acme\npolicy_repo: https://github.com/acme/agentic-org-policy.git\n",
    )
    (home / "policy" / "acme" / "repo" / "policy").mkdir(parents=True)
    assert Workspace.load(repo, env={}, home=home).policy_repo == home / "policy" / "acme" / "repo"


def test_the_standalone_policy_setting_init_reads_is_read_here_too(repo, policy_repo, tmp_path):
    """`policy:` is the key `init --standalone` reads; both keys name a local folder."""
    home = tmp_path / "home"
    write(home / "config.yaml", f"policy: {policy_repo.as_posix()}\n")
    assert Workspace.load(repo, env={}, home=home).policy_repo == policy_repo


def record_init(repo: Path, source: str, mode: str = "standalone") -> None:
    write(
        repo / ".agentic" / "local" / "init.json",
        json.dumps({"mode": mode, "policy_source": source}),
    )


def test_the_policy_init_used_for_this_repo_is_remembered(repo, policy_repo, tmp_path):
    home = tmp_path / "home"
    (home / "policy" / "standalone" / "policy").mkdir(parents=True)
    record_init(repo, str(policy_repo / "policy"))  # --policy may name the policy/ folder
    assert Workspace.load(repo, env={}, home=home).policy_repo == policy_repo

    other = tmp_path / "other"
    env = {"AGENTIC_POLICY_REPO": str(other)}  # an explicit setting still wins
    assert Workspace.load(repo, env=env, home=home).policy_repo == other


def test_a_recorded_policy_inside_the_repository_is_ignored(repo, tmp_path):
    """A repository could commit its own init.json pointing at its own files: the policy
    must never come from the code it governs."""
    home = tmp_path / "home"
    (home / "policy" / "standalone" / "policy").mkdir(parents=True)
    (repo / "evil" / "policy").mkdir(parents=True)
    record_init(repo, str(repo / "evil"))
    assert Workspace.load(repo, env={}, home=home).policy_repo == home / "policy" / "standalone"
    record_init(repo, "evil")  # a relative path is ignored too
    assert Workspace.load(repo, env={}, home=home).policy_repo == home / "policy" / "standalone"


def test_a_recorded_policy_that_is_gone_is_refused_not_replaced(repo, tmp_path):
    """A custom policy moved, deleted or on an unplugged drive must not be swapped for
    the standalone template without a word: the person believes theirs applies."""
    home = tmp_path / "home"
    (home / "policy" / "standalone" / "policy").mkdir(parents=True)
    gone = tmp_path / "my-strict-policy"
    for recorded in (gone, gone / "policy"):
        record_init(repo, str(recorded))
        with pytest.raises(CliError) as caught:
            Workspace.load(repo, env={}, home=home).policy()
        assert caught.value.code == ExitCode.REFUSED
        assert str(gone) in str(caught.value)
        assert "agentic init --standalone --policy" in caught.value.hint
    (gone / "policy").mkdir(parents=True)  # back again (the drive is plugged in)
    assert Workspace.load(repo, env={}, home=home).policy_repo == gone


def test_an_org_init_record_is_not_a_local_policy(repo, tmp_path):
    home = tmp_path / "home"
    record_init(repo, "https://github.com/acme/agentic-org-policy.git", mode="org")
    assert Workspace.load(repo, env={}, home=home).policy_repo is None


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


def test_a_committed_init_record_is_ignored(repo, policy_repo, tmp_path):
    """DF-353: `.agentic/local/` is git-ignored, so a committed init.json came with a cloned
    repository, not from this person's `init`: it never chooses the policy."""
    home = tmp_path / "home"
    (home / "policy" / "standalone" / "policy").mkdir(parents=True)
    record_init(repo, str(policy_repo))
    git(repo, "add", "-f", ".agentic/local/init.json")
    git(repo, "commit", "-qm", "ship our own policy choice")
    assert Workspace.load(repo, env={}, home=home).policy_repo == home / "policy" / "standalone"


def test_a_committed_standalone_policy_folder_is_ignored(repo, tmp_path):
    """DF-353: the same for a committed `.agentic/local/policy/` folder."""
    home = tmp_path / "home"
    (home / "policy" / "standalone" / "policy").mkdir(parents=True)
    write(repo / ".agentic" / "local" / "policy" / "policy" / "tiers.yaml", "tiers: {}\n")
    assert (
        Workspace.load(repo, env={}, home=home).policy_repo
        == repo / ".agentic" / "local" / "policy"
    )
    git(repo, "add", "-f", ".agentic/local/policy")
    git(repo, "commit", "-qm", "ship a policy")
    assert Workspace.load(repo, env={}, home=home).policy_repo == home / "policy" / "standalone"


@pytest.mark.parametrize("share", ["\\\\files.example\\policies", "//files.example/policies"])
def test_a_recorded_policy_on_a_network_share_is_refused(repo, tmp_path, share):
    """DF-353: a UNC path is never followed from a record (on Windows, touching it sends the
    user's credentials to that server); an explicit setting still may name one."""
    record_init(repo, share)
    with pytest.raises(CliError) as caught:
        Workspace.load(repo, env={}, home=tmp_path / "home").policy_repo  # noqa: B018
    assert caught.value.code == ExitCode.REFUSED and "network share" in str(caught.value)
