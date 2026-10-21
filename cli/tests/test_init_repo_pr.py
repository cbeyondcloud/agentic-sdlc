"""`agentic init --repo` opens its pull request through the hosting adapter (M1-58, DF-183):
the branch goes to a local bare remote and the pull request to a local fake GitHub API."""

import json
import subprocess
from pathlib import Path

import pytest
from cli_helpers import git, snapshot
from fake_github import FakeGitHub

from agentic_cli.exit_codes import ExitCode
from agentic_cli.main import main

TOKEN = "ghp_" + "InitRepo" + "Fake0123456789"  # gitleaks:allow - built from pieces
HOST = "github.example.test"


@pytest.fixture(autouse=True)
def identity(monkeypatch: pytest.MonkeyPatch, home: Path) -> None:
    for kind in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{kind}_NAME", "Repo Owner")
        monkeypatch.setenv(f"GIT_{kind}_EMAIL", "owner@example.invalid")


@pytest.fixture
def bare(tmp_path: Path, repo: Path) -> Path:
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(remote)], check=True)
    git(repo, "remote", "set-url", "origin", f"https://{HOST}/acme/app.git")
    git(repo, "remote", "set-url", "--push", "origin", str(remote))
    git(repo, "push", "-q", "origin", "main")
    return remote


@pytest.fixture
def github(home: Path, bare: Path):
    fake = FakeGitHub(TOKEN, bare).start()
    (home / ".agentic").mkdir(exist_ok=True)
    (home / ".agentic" / "config.yaml").write_text(
        f"git_hosts:\n  {HOST}:\n    kind: github\n    api_url: {fake.url}\n", encoding="utf-8"
    )
    yield fake
    fake.stop()


def init_repo(repo: Path, policy_repo: Path, monkeypatch, *extra: str) -> int:
    monkeypatch.chdir(repo)
    return main(["--json", "init", "--repo", "--standalone", "--policy", str(policy_repo), *extra])


def test_the_branch_is_pushed_and_the_pull_request_opened(
    repo, policy_repo, bare, github, monkeypatch, capsys
):
    monkeypatch.setenv("AGENTIC_GITHUB_TOKEN", TOKEN)
    before = snapshot(repo)
    assert init_repo(repo, policy_repo, monkeypatch) == ExitCode.OK
    shown = capsys.readouterr()
    out = json.loads(shown.out)
    assert git(bare, "rev-parse", "refs/heads/agentic/init") == out["commit"]
    [pull] = github.pulls
    assert (pull["head"]["ref"], pull["base"]["ref"]) == ("agentic/init", "main")
    assert pull["title"] == "Set up agentic for this repository"
    for path in out["files"]:
        assert f"`{path}`" in pull["body"]
    assert out["pull_request"] == {
        "opened": True,
        "created": True,
        "number": 1,
        "url": pull["html_url"],
    }
    assert TOKEN not in shown.out + shown.err
    assert snapshot(repo) == before  # the checkout is still never touched


def test_without_a_token_the_manual_steps_are_given_and_nothing_is_pushed(
    repo, policy_repo, bare, github, monkeypatch, capsys
):
    assert init_repo(repo, policy_repo, monkeypatch) == ExitCode.OK
    out = json.loads(capsys.readouterr().out)
    assert out["pull_request"]["opened"] is False
    assert "AGENTIC_GITHUB_TOKEN" in out["pull_request"]["why"]
    assert out["next"]["push"] == "git push -u origin agentic/init"
    assert git(bare, "branch", "--list", "agentic/init") == "" and github.requests == []


def test_without_a_token_a_person_sees_a_warning(
    repo, policy_repo, bare, github, monkeypatch, capsys
):
    monkeypatch.chdir(repo)
    code = main(["init", "--repo", "--standalone", "--policy", str(policy_repo)])
    assert code == ExitCode.OK
    shown = capsys.readouterr()
    assert "warning" in shown.err and "agentic login --for github" in shown.err
    assert "git push -u origin agentic/init" in shown.out


def test_no_pr_keeps_everything_local(repo, policy_repo, bare, github, monkeypatch, capsys):
    monkeypatch.setenv("AGENTIC_GITHUB_TOKEN", TOKEN)
    assert init_repo(repo, policy_repo, monkeypatch, "--no-pr") == ExitCode.OK
    out = json.loads(capsys.readouterr().out)
    assert out["pull_request"] == {"opened": False, "why": "--no-pr"}
    assert git(bare, "branch", "--list", "agentic/init") == "" and github.requests == []


def test_a_failed_push_is_an_error_that_keeps_the_branch_and_the_manual_steps(
    repo, policy_repo, bare, github, monkeypatch, capsys, tmp_path
):
    monkeypatch.setenv("AGENTIC_GITHUB_TOKEN", TOKEN)
    git(repo, "remote", "set-url", "--push", "origin", str(tmp_path / "missing.git"))
    assert init_repo(repo, policy_repo, monkeypatch) == ExitCode.ERROR
    shown = capsys.readouterr()
    out, error = json.loads(shown.out), json.loads(shown.err)
    assert out["branch"] == "agentic/init" and out["pull_request"]["opened"] is False
    assert "git push" in error["error"]
    assert "git push -u origin agentic/init" in error["hint"]
    assert git(repo, "rev-parse", "--verify", "-q", "refs/heads/agentic/init")  # kept
    assert github.pulls == []
