"""`agentic init --repo` (M1-52): the shared files go on a new branch, the checkout is never
touched, and every guard refusal is exit 2 with nothing written. Temporary git repos."""

import json
from pathlib import Path

import pytest
from cli_helpers import FIXTURES, commit_bytes, commit_file, git, git_bytes, snapshot

from agentic.policy import load_policy
from agentic.repoconfig import RepoConfig, load_repo_config
from agentic_cli.exit_codes import ExitCode
from agentic_cli.main import main
from agentic_cli.repo_setup import (
    AGENTIC_BLOCK_BEGIN,
    AGENTIC_BLOCK_END,
    agents_md,
    commit_on_branch,
    merge_gitattributes,
    shared_files,
)

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


def test_standalone_also_offers_a_local_merge(repo, policy_repo, monkeypatch, capsys):
    """Trying agentic alone, the shared files can simply be merged into this clone
    (DF-284); the push and pull-request steps stay for sharing them."""
    monkeypatch.chdir(repo)
    assert main(["init", "--repo", "--standalone", "--policy", str(policy_repo)]) == 0
    printed = capsys.readouterr().out
    assert "git merge agentic/init" in printed and "git push -u origin agentic/init" in printed


def test_without_an_origin_no_pull_request_is_tried(tmp_path, policy_repo, monkeypatch, capsys):
    from cli_helpers import make_repo

    local = make_repo(tmp_path / "local-only", origin=None)
    assert init_repo(local, policy_repo, monkeypatch) == ExitCode.OK
    captured = capsys.readouterr()
    out = json.loads(captured.out)
    assert out["next"]["merge"] == "git merge agentic/init"
    assert out["pull_request"] == {"opened": False, "why": "no origin remote"}
    assert "git remote add origin" in out["next"]["push"]
    assert "warning" not in captured.err


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


@pytest.mark.parametrize("name", ["AGENTS.md", "agents.md", "Agents.MD"])
def test_an_existing_agents_md_is_never_replaced(repo, policy_repo, monkeypatch, capsys, name):
    """A curated AGENTS.md (in any case: one path on Windows and macOS) stays as it is."""
    commit_file(repo, name, "# Our curated agent guide\n")
    assert init_repo(repo, policy_repo, monkeypatch) == ExitCode.OK
    out = json.loads(capsys.readouterr().out)
    assert branch_files(repo) == ALLOWED - {"AGENTS.md"}
    assert "AGENTS.md" not in out["files"] and out["kept"] == [name]
    assert git(repo, "show", f"agentic/init:{name}") == "# Our curated agent guide"


def test_a_crlf_gitattributes_keeps_its_line_endings(repo, policy_repo, monkeypatch):
    commit_bytes(repo, ".gitattributes", b"*.png binary\r\n*.jpg binary\r\n")
    assert init_repo(repo, policy_repo, monkeypatch) == ExitCode.OK
    blob = git_bytes(repo, "cat-file", "blob", "agentic/init:.gitattributes")
    assert blob.startswith(b"*.png binary\r\n*.jpg binary\r\n")
    assert AGENTIC_BLOCK_BEGIN.encode() in blob and b"\n" not in blob.replace(b"\r\n", b"")


def test_a_non_utf8_gitattributes_is_kept_byte_for_byte(repo, policy_repo, monkeypatch):
    original = "# café assets\n*.png binary\n".encode("latin-1")
    commit_bytes(repo, ".gitattributes", original)
    assert init_repo(repo, policy_repo, monkeypatch) == ExitCode.OK
    blob = git_bytes(repo, "cat-file", "blob", "agentic/init:.gitattributes")
    assert blob.startswith(original) and AGENTIC_BLOCK_BEGIN.encode() in blob


def test_a_begin_marker_without_end_is_not_treated_as_a_block():
    text = f"*.png binary\n{AGENTIC_BLOCK_BEGIN}\n*.jpg binary\n"
    merged = merge_gitattributes(text)
    assert "*.png binary" in merged and "*.jpg binary" in merged
    assert merged.count(AGENTIC_BLOCK_END) == 1
    assert merge_gitattributes(merged) == merged  # the stray marker's lines survive a rerun


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


# --- M1 close-out (DF-210, DF-195) ---


def test_the_branch_is_created_in_a_sha256_repository(tmp_path: Path):
    """DF-210: the create-only update-ref must not use a 40-zero (SHA-1) old value."""
    repo = tmp_path / "sha256"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "a.py").write_text("x = 1\n", encoding="utf-8")
    git(repo, "init", "-q", "--object-format=sha256", "-b", "main")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "base")
    branch, commit = commit_on_branch(repo, {"AGENTS.md": "# AGENTS.md\n"}, "set up")
    assert branch == "agentic/init" and len(commit) == 64
    assert git(repo, "rev-parse", f"refs/heads/{branch}") == commit


def test_the_commit_is_signed_when_the_person_signs_commits(repo: Path, monkeypatch):
    """DF-210: `commit-tree` ignores commit.gpgSign (it is plumbing), so a repository
    whose branch protection wants signed commits would reject the init branch."""
    import agentic_cli.repo_setup as setup

    git(repo, "config", "commit.gpgSign", "true")
    seen: list[tuple[str, ...]] = []
    real = setup._git_run

    def spy(where, env, *args, data=None):
        seen.append(args)
        if args[0] == "commit-tree":  # no signing key in a test: sign nothing, record it
            args = tuple(a for a in args if a != "-S")
        return real(where, env, *args, data=data)

    monkeypatch.setattr(setup, "_git_run", spy)
    commit_on_branch(repo, {"AGENTS.md": "# AGENTS.md\n"}, "set up")
    assert any(args[0] == "commit-tree" and "-S" in args for args in seen)


def test_an_unsigning_person_gets_an_unsigned_commit(repo: Path, monkeypatch):
    import agentic_cli.repo_setup as setup

    seen: list[tuple[str, ...]] = []
    real = setup._git_run
    monkeypatch.setattr(
        setup, "_git_run", lambda w, e, *a, data=None: seen.append(a) or real(w, e, *a, data=data)
    )
    commit_on_branch(repo, {"AGENTS.md": "# AGENTS.md\n"}, "set up")
    assert all("-S" not in args for args in seen)


def test_the_agents_md_draft_reads_non_ascii_folders(repo: Path):
    """DF-195: git C-quotes such paths by default ("caf\\303\\251/x.py")."""
    commit_file(repo, "café/x.py", "x = 1\n")
    draft = agents_md(repo)
    assert "`café/`" in draft and '"' not in draft.split("## Layout")[1].split("##")[0]
