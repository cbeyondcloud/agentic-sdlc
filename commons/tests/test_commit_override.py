"""The commit-command override (M2-61, spec §9.2): an in-place component's own commit,
rollback and push commands - allowed by org policy, checked by the guard, run as argv lists,
recorded in the run folder."""

import json
import os
import subprocess
from pathlib import Path

import pytest
from in_place_support import (
    ALLOW,
    COMMIT,
    PUSH,
    PY,
    ROLLBACK,
    ROLLBACKS,
    RUN,
    TOOL_PREFIX,
    git,
    make_in_place_repo,
    open_ledger,
)

from sdlc_commons.commit_override import CommitOverride, OverrideError, split_command
from sdlc_commons.worktrees import RunWorktrees, kept_override, override_homes


@pytest.fixture
def repo(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setenv("VCS_TOOL_LOG", str(tmp_path / "tool.log"))
    return make_in_place_repo(tmp_path / "repo")


def in_place(repo: Path):
    open_ledger(repo)
    return RunWorktrees(repo, RUN).create()


def override(repo: Path, **kwargs) -> CommitOverride:
    settings = {"allow": ALLOW, "rollback_commands": ROLLBACKS} | kwargs
    return CommitOverride(repo, RUN, **settings)


def tool_calls(repo: Path) -> list[str]:
    log = repo.parent / "tool.log"
    return log.read_text(encoding="utf-8").split() if log.is_file() else []


# ── splitting ─────────────────────────────────────────────────────────────────


def test_a_command_splits_into_words_keeping_windows_paths_and_quotes():
    assert split_command(r'"C:\Program Files\tool.exe" commit -m {message}') == (
        r"C:\Program Files\tool.exe",
        "commit",
        "-m",
        "{message}",
    )
    assert split_command("tools/vcs  'a b'") == ("tools/vcs", "a b")


@pytest.mark.parametrize("text", ["", "   ", "tool 'unclosed"])
def test_an_unusable_command_is_refused(text):
    with pytest.raises(OverrideError):
        split_command(text)


# ── what is accepted ──────────────────────────────────────────────────────────


def test_an_allowed_override_is_accepted_and_kept_for_the_run(repo):
    trees = in_place(repo)
    accepted = override(repo).resolve(trees)
    [(name, cmds)] = accepted.items()
    assert cmds.checkout == repo and cmds.base == git(repo, "rev-parse", "HEAD")
    assert cmds.commit == (*split_command(TOOL_PREFIX), "commit", "{message_file}")
    assert cmds.rollback == split_command(ROLLBACK) and cmds.push == split_command(PUSH)
    assert kept_override(repo, RUN) == accepted
    assert override_homes(repo, RUN) == [repo]


def test_without_org_allow_nothing_runs_and_the_setting_is_named(repo):
    trees = in_place(repo)
    with pytest.raises(OverrideError, match=r"commit_override\.allow"):
        override(repo, allow=()).resolve(trees)
    assert kept_override(repo, RUN) is None


def test_a_command_outside_the_allow_list_is_refused(repo):
    trees = in_place(repo)
    with pytest.raises(OverrideError, match="not allowed by org policy"):
        override(repo, allow=(f"{TOOL_PREFIX} commit",)).resolve(trees)  # restore is not


def test_without_a_rollback_command_the_setting_is_named(repo):
    trees = in_place(repo)
    with pytest.raises(OverrideError, match=r"rollback_commands\['\.'\]"):
        override(repo, rollback_commands={}).resolve(trees)


def test_without_a_commit_command_the_setting_is_named(tmp_path, monkeypatch):
    repo = make_in_place_repo(tmp_path / "bare-profile", commit="")
    trees = in_place(repo)
    with pytest.raises(OverrideError, match=r"vcs\.commit_command"):
        override(repo).resolve(trees)


def test_raw_git_is_never_an_override(tmp_path):
    repo = make_in_place_repo(tmp_path / "git-profile", commit="git commit -m {message}")
    trees = in_place(repo)
    with pytest.raises(OverrideError, match="raw git"):
        override(repo, allow=("git commit", *ALLOW)).resolve(trees)


def test_a_denied_command_is_refused_by_the_guard(tmp_path):
    repo = make_in_place_repo(tmp_path / "rm-profile", commit="rm -rf build {message_file}")
    trees = in_place(repo)
    with pytest.raises(OverrideError, match="refused by the guard"):
        override(repo, allow=("rm", *ALLOW)).resolve(trees)


def test_an_org_deny_rule_refuses_the_command_too(repo):
    trees = in_place(repo)
    program = Path(PY).name.removesuffix(".exe")  # the guard matches the program's name
    with pytest.raises(OverrideError, match="deny rule"):
        override(repo, deny_shell=(program,)).resolve(trees)


@pytest.mark.parametrize(
    "commit",
    [
        f"{TOOL_PREFIX} commit -m{{message}}",  # a placeholder inside a word
        f"{TOOL_PREFIX} commit {{commit}}",  # another action's placeholder
        f"{TOOL_PREFIX} commit",  # no message at all
    ],
)
def test_placeholders_are_whole_words_of_the_right_action(tmp_path, commit):
    repo = make_in_place_repo(tmp_path / "ph", commit=commit)
    trees = in_place(repo)
    with pytest.raises(OverrideError, match="placeholder|message"):
        override(repo).resolve(trees)


def test_a_dirty_checkout_is_refused_before_the_first_step(repo):
    (repo / "app.py").write_text("x = 'the developer is still typing'\n", encoding="utf-8")
    trees = in_place(repo)
    with pytest.raises(OverrideError, match="uncommitted"):
        override(repo).resolve(trees)


def test_the_kept_override_outlives_an_edit_of_the_profile(repo):
    trees = in_place(repo)
    first = override(repo).resolve(trees)
    (repo / ".sdlc" / "profile.yml").write_text("vcs:\n  deny_raw_git: true\n", encoding="utf-8")
    assert override(repo).resolve(trees) == first


# ── running ───────────────────────────────────────────────────────────────────


def test_commit_runs_the_tool_with_the_message_as_one_argument(repo):
    cmds = override(repo).resolve(in_place(repo))["repo"]
    (repo / "app.py").write_text("x = 2\n", encoding="utf-8")
    message = 'step: build; rm -rf / && echo "quoted" $HOME\n\nTrailer: yes'
    override(repo).commit(cmds, message)
    assert git(repo, "log", "-1", "--format=%B") == message.rstrip("\n")
    assert git(repo, "status", "--porcelain") == ""
    assert tool_calls(repo) == ["commit"]


def test_rollback_puts_the_checkout_back(repo):
    cmds = override(repo).resolve(in_place(repo))["repo"]
    base = git(repo, "rev-parse", "HEAD")
    (repo / "app.py").write_text("x = 2\n", encoding="utf-8")
    override(repo).commit(cmds, "step one")
    (repo / "new.py").write_text("y = 1\n", encoding="utf-8")
    override(repo).rollback(cmds, base)
    assert git(repo, "rev-parse", "HEAD") == base
    assert not (repo / "new.py").exists()
    assert (repo / "app.py").read_text(encoding="utf-8") == "x = 1\n"


@pytest.mark.parametrize("value", ["HEAD", "--hard", "abc", "a" * 7 + ";x"])
def test_rollback_takes_only_a_commit_id(repo, value):
    cmds = override(repo).resolve(in_place(repo))["repo"]
    with pytest.raises(OverrideError, match="commit id"):
        override(repo).rollback(cmds, value)
    assert tool_calls(repo) == []


@pytest.mark.parametrize("value", ["-f", "a b", "x;y", "../up", ""])
def test_push_takes_only_a_branch_name(repo, value):
    cmds = override(repo).resolve(in_place(repo))["repo"]
    with pytest.raises(OverrideError, match="branch name"):
        override(repo).push(cmds, value)


def test_a_failed_command_names_its_exit_code(repo):
    cmds = override(repo).resolve(in_place(repo))["repo"]
    with pytest.raises(OverrideError, match="exit 1"):
        override(repo).rollback(cmds, "0" * 40)  # not a commit of this repository


def test_a_commit_script_the_step_changed_is_refused(repo):
    cmds = override(repo).resolve(in_place(repo))["repo"]
    tool = repo / "tools" / "vcs.py"
    tool.write_text(tool.read_text(encoding="utf-8") + "\n# an agent was here\n", "utf-8")
    with pytest.raises(OverrideError, match="tools/vcs.py"):
        override(repo).commit(cmds, "step")
    assert tool_calls(repo) == []


def test_org_policy_revoked_mid_run_stops_the_next_command(repo):
    cmds = override(repo).resolve(in_place(repo))["repo"]
    with pytest.raises(OverrideError, match="not allowed by org policy"):
        override(repo, allow=()).rollback(cmds, cmds.base)


def test_every_command_is_recorded_in_the_run_folder(repo):
    cmds = override(repo).resolve(in_place(repo))["repo"]
    (repo / "app.py").write_text("x = 2\n", encoding="utf-8")
    override(repo).commit(cmds, "secret-looking message text")
    override(repo).rollback(cmds, cmds.base)
    log = repo / ".agentic" / "local" / "runs" / RUN / "vcs-commands.jsonl"
    rows = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert [(r["component"], r["action"], r["exit_code"]) for r in rows] == [
        ("repo", "commit", 0),
        ("repo", "rollback", 0),
    ]
    assert rows[0]["command"] == list(split_command(COMMIT))  # as configured, unfilled
    assert "secret-looking" not in log.read_text(encoding="utf-8")
    assert all(isinstance(r["duration_ms"], int) and r["at"] for r in rows)


def test_the_framework_credentials_are_not_passed_to_the_tool(repo, monkeypatch):
    monkeypatch.setenv("AGENTIC_GITHUB_TOKEN", "never-" + "passed")
    cmds = override(repo).resolve(in_place(repo))["repo"]
    seen = {}
    real = subprocess.run

    def spy(argv, **kwargs):
        if Path(argv[0]).name.lower().removesuffix(".exe") == "git":
            return real(argv, **kwargs)  # the framework's own git reads
        seen.update(kwargs)
        raise OSError("stop here")

    monkeypatch.setattr("sdlc_commons.commit_override.subprocess.run", spy)
    with pytest.raises(OverrideError):
        override(repo).rollback(cmds, cmds.base)
    assert "AGENTIC_GITHUB_TOKEN" not in seen["env"] and seen["env"]["GIT_TERMINAL_PROMPT"] == "0"
    assert seen.get("shell") in (None, False)
    assert os.environ["AGENTIC_GITHUB_TOKEN"]  # still set for the framework itself
