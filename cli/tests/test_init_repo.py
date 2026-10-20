"""`agentic init --repo` (M1-52): the shared files go on a new branch, the checkout is never
touched, and every guard refusal is exit 2 with nothing written. Temporary git repos."""

import json
from pathlib import Path

import pytest
from cli_helpers import FIXTURES, commit_file, git, snapshot

from agentic.policy import load_policy
from agentic.repoconfig import RepoConfig, load_repo_config
from agentic_cli.exit_codes import ExitCode
from agentic_cli.main import main
from agentic_cli.repo_setup import AGENTIC_BLOCK_BEGIN, merge_gitattributes, shared_files

ALLOWED = {".agentic/config.yaml", "AGENTS.md", ".gitattributes"}
WORKFLOW = ".github/workflows/agentic-index-export.yml"


@pytest.fixture(autouse=True)
def identity(monkeypatch: pytest.MonkeyPatch, home: Path) -> None:
    for kind in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{kind}_NAME", "Repo Owner")
        monkeypatch.setenv(f"GIT_{kind}_EMAIL", "owner@example.invalid")


def init_repo(repo: Path, policy_repo: Path, monkeypatch, *extra: str) -> int:
    monkeypatch.chdir(repo)
    return main(["--json", "init", "--repo", "--standalone", "--policy", str(policy_repo), *extra])


def branch_files(repo: Path, branch: str = "agentic/init") -> set[str]:
    return set(git(repo, "diff", "--name-only", "main", branch).splitlines())


def test_the_branch_holds_exactly_the_allowed_files(repo, policy_repo, monkeypatch, capsys):
    assert init_repo(repo, policy_repo, monkeypatch) == ExitCode.OK
    out = json.loads(capsys.readouterr().out)
    assert out["branch"] == "agentic/init" and set(out["files"]) == ALLOWED
    assert branch_files(repo) == ALLOWED
    config = load_repo_config(git(repo, "show", "agentic/init:.agentic/config.yaml"))
    assert config.classification == "internal"  # the policy's default class
    assert "git push -u origin agentic/init" in out["next"]["push"]
    assert out["next"]["pull_request"] == (
        "https://github.com/acme/app/compare/main...agentic/init?expand=1"
    )


def test_the_checkout_and_current_branch_never_change(repo, policy_repo, monkeypatch):
    before = snapshot(repo)
    assert init_repo(repo, policy_repo, monkeypatch) == ExitCode.OK
    assert snapshot(repo) == before
    assert git(repo, "status", "--porcelain") == ""
    assert git(repo, "symbolic-ref", "--short", "HEAD") == "main"


def test_files_on_the_branch_have_lf_endings(repo, policy_repo, monkeypatch):
    init_repo(repo, policy_repo, monkeypatch)
    for path in ALLOWED:
        assert "\r" not in git(repo, "show", f"agentic/init:{path}")


def test_team_classification_and_pipelines_are_written(repo, policy_repo, monkeypatch):
    extra = ("--classification", "restricted", "--team", "payments", "--pipeline", "fd")
    assert init_repo(repo, policy_repo, monkeypatch, *extra) == ExitCode.OK
    config = load_repo_config(git(repo, "show", "agentic/init:.agentic/config.yaml"))
    assert (config.classification, config.team, config.pipelines) == (
        "restricted",
        "payments",
        ["fd"],
    )


def test_an_unknown_classification_is_invalid(repo, policy_repo, monkeypatch):
    code = init_repo(repo, policy_repo, monkeypatch, "--classification", "nope")
    assert code == ExitCode.INVALID
    assert git(repo, "branch", "--list", "agentic/init") == ""


def test_the_workflow_only_when_policy_says_so(repo, policy_repo, monkeypatch):
    org = policy_repo / "policy" / "org.yaml"
    org.write_text(org.read_text(encoding="utf-8") + "index_export_workflow: true\n", "utf-8")
    assert init_repo(repo, policy_repo, monkeypatch) == ExitCode.OK
    assert branch_files(repo) == ALLOWED | {WORKFLOW}


def test_an_existing_gitattributes_is_kept(repo, policy_repo, monkeypatch):
    commit_file(repo, ".gitattributes", "*.png binary\n")
    assert init_repo(repo, policy_repo, monkeypatch) == ExitCode.OK
    text = git(repo, "show", "agentic/init:.gitattributes")
    assert text.startswith("*.png binary\n") and AGENTIC_BLOCK_BEGIN in text


def test_the_gitattributes_block_is_replaced_not_repeated():
    once = merge_gitattributes("*.png binary\n")
    assert merge_gitattributes(once) == once and once.count(AGENTIC_BLOCK_BEGIN) == 1


def test_no_repo_pipelines_folder_in_org_mode(repo: Path):
    policy = load_policy(FIXTURES / "policy" / "policy")
    assert policy.allow_repo_pipelines is False
    files = shared_files(repo, policy, RepoConfig(classification="internal"))
    assert set(files) == ALLOWED
    assert not any(path.startswith(".agentic/pipelines/") for path in files)


def test_a_second_branch_name_when_the_first_is_taken(repo, policy_repo, monkeypatch, capsys):
    git(repo, "branch", "agentic/init")
    assert init_repo(repo, policy_repo, monkeypatch) == ExitCode.OK
    assert json.loads(capsys.readouterr().out)["branch"] == "agentic/init-2"


def test_agents_md_describes_the_repo(repo, policy_repo, monkeypatch):
    init_repo(repo, policy_repo, monkeypatch)
    agents = git(repo, "show", "agentic/init:AGENTS.md")
    assert "src/" in agents and "Python" in agents


# --- refusals: exit 2, nothing written, no branch ---


def refused(repo: Path, policy_repo: Path, monkeypatch, capsys, cwd: Path | None = None) -> str:
    before = snapshot(repo)
    monkeypatch.chdir(cwd or repo)
    code = main(["--json", "init", "--repo", "--standalone", "--policy", str(policy_repo)])
    assert code == ExitCode.REFUSED
    assert snapshot(repo) == before
    assert git(repo, "branch", "--list", "agentic/*") == ""
    return json.loads(capsys.readouterr().err)["error"]


def test_refused_from_a_subfolder(repo, policy_repo, monkeypatch, capsys):
    assert "repo root" in refused(repo, policy_repo, monkeypatch, capsys, cwd=repo / "src")


def test_refused_with_uncommitted_changes(repo, policy_repo, monkeypatch, capsys):
    (repo / "README.md").write_text("changed\n", encoding="utf-8")
    assert "uncommitted" in refused(repo, policy_repo, monkeypatch, capsys)


def test_refused_off_the_default_branch(repo, policy_repo, monkeypatch, capsys):
    git(repo, "switch", "-q", "-c", "topic")
    assert "default branch" in refused(repo, policy_repo, monkeypatch, capsys)


def test_refused_when_already_initialised(repo, policy_repo, monkeypatch, capsys):
    commit_file(repo, ".agentic/config.yaml", "version: 1\nclassification: internal\n")
    assert "already" in refused(repo, policy_repo, monkeypatch, capsys)


def test_refused_outside_git(tmp_path, policy_repo, monkeypatch, capsys):
    plain = tmp_path / "plain"
    plain.mkdir()
    monkeypatch.chdir(plain)
    code = main(["init", "--repo", "--standalone", "--policy", str(policy_repo)])
    assert code == ExitCode.REFUSED and list(plain.iterdir()) == []


def test_org_mode_is_refused_until_roles_can_be_checked(repo, policy_repo, monkeypatch, capsys):
    from agentic_cli.config import agentic_home

    git(policy_repo, "init", "-q", "-b", "main")
    git(policy_repo, "add", "-A")
    git(policy_repo, "commit", "-qm", "policy")
    home = agentic_home()
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(
        f"org: acme\npolicy_repo: {policy_repo.as_posix()}\n", encoding="utf-8"
    )
    before = snapshot(repo, home)
    monkeypatch.chdir(repo)
    assert main(["--json", "init", "--repo"]) == ExitCode.REFUSED
    assert "agent.edit.repo" in json.loads(capsys.readouterr().err)["error"]
    assert snapshot(repo, home) == before
