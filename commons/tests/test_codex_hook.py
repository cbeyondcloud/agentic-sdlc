"""Tests for the guard hook Codex runs before each shell command and patch (M2-40, DF-116):
the guard's deny rules, allow-list (an unlisted program is refused - the default-deny
execpolicy lacks), redirect-aware splitting and protected paths, in Codex's PreToolUse
protocol. Codex treats a crashed hook as passed, so every error here must refuse."""

import io
import json
import subprocess
import sys
from pathlib import Path

import pytest

from sdlc_commons.codex_hook import decide, hook_argv, load_guard, main, patch_paths, write_spec
from sdlc_commons.guards import Guard
from sdlc_commons.ledger import RunIdentity, RunLedger
from sdlc_commons.worktrees import RunWorktrees

RUN = "r-hook-1"
GIT = ["-c", "user.email=dev@example.invalid", "-c", "user.name=Dev"]
PATCH = "*** Begin Patch\n*** Add File: {path}\n+x\n*** End Patch\n"


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-q", "-b", "main"], check=True)
    (repo / "app.py").write_text("x = 1\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), *GIT, "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), *GIT, "commit", "-qm", "base"], check=True)
    RunLedger(repo, RUN).open(
        identity=RunIdentity(team="platform", policy_version="git:" + "a" * 40),
        kind="feature",
        project="demo",
        owner="platform",
        criticality="low",
    )
    (made,) = RunWorktrees(repo, RUN).create().values()
    return made.path


@pytest.fixture
def guard(tree: Path) -> Guard:
    repo = tree
    while not (repo / ".git").is_dir():
        repo = repo.parent
    return Guard(
        repo,
        RUN,
        deny_shell=("git push",),
        deny_paths=("infra/**",),
        allow_shell=("echo", "git status", "uv run pytest", "npm"),
        ask_shell=("npm publish",),
    )


def call(tool: str, tool_input: dict, cwd: Path) -> dict:
    return {
        "hook_event_name": "PreToolUse",
        "tool_name": tool,
        "cwd": str(cwd),
        "tool_input": tool_input,
    }


def bash(command: str, cwd: Path) -> dict:
    return call("Bash", {"command": command}, cwd)


def patch(path: str, cwd: Path) -> dict:
    return call("apply_patch", {"command": PATCH.format(path=path)}, cwd)


def test_blocked_command_is_refused_with_the_reason(guard, tree):
    reason = decide(bash("git push origin main", tree), guard, tree)
    assert reason and "git push" in reason


def test_a_redirect_chain_hiding_a_denied_command_is_refused(guard, tree):  # S3 run O
    reason = decide(bash("echo ok > f && git push origin main", tree), guard, tree)
    assert reason and "git push" in reason


def test_an_unlisted_program_is_refused(guard, tree):  # the default-deny Codex lacks
    reason = decide(bash("curl https://example.com", tree), guard, tree)
    assert reason and "curl" in reason


def test_an_allowed_command_passes(guard, tree):
    assert decide(bash("git status", tree), guard, tree) is None
    assert decide(bash("uv run pytest -q", tree), guard, tree) is None


def test_an_ask_command_is_refused(guard, tree):
    reason = decide(bash("npm publish", tree), guard, tree)
    assert reason and "approval" in reason


def test_a_patch_to_a_denied_path_is_refused_and_a_normal_one_passes(guard, tree):
    reason = decide(patch("infra/main.tf", tree), guard, tree)
    assert reason and "infra" in reason
    assert decide(patch("src/new.py", tree), guard, tree) is None


def test_a_patch_escaping_the_tree_is_refused(guard, tree):
    assert decide(patch("../../outside.txt", tree), guard, tree)


def test_other_tools_pass(guard, tree):
    assert decide(call("view_image", {"path": "x.png"}, tree), guard, tree) is None


def test_patch_paths_reads_every_header():
    text = (
        "*** Begin Patch\n*** Update File: a.py\n@@\n-x\n+y\n*** Move to: b.py\n"
        "*** Delete File: c.py\n*** Add File: d/e.py\n+z\n*** End Patch\n"
    )
    assert patch_paths(text) == ["a.py", "b.py", "c.py", "d/e.py"]


INDENTED = (
    "*** Begin Patch\n*** Add File: ok.txt\n+a\n{indent}*** Add File: {path}\n+x\n*** End Patch\n"
)


@pytest.mark.parametrize("indent", [" ", "\t", "  "])
def test_an_indented_header_is_read_as_codex_reads_it(indent):  # review I1
    # Codex trims each hunk header before matching it: an indented one still writes
    text = INDENTED.format(indent=indent, path="infra/evil.tf")
    assert patch_paths(text) == ["ok.txt", "infra/evil.tf"]


def test_an_indented_header_to_a_denied_path_is_refused(guard, tree):  # review I1
    text = INDENTED.format(indent=" ", path="infra/evil.tf")
    reason = decide(call("apply_patch", {"command": text}, tree), guard, tree)
    assert reason and "infra" in reason


@pytest.mark.parametrize(
    "header", ["*** Copy File: infra/evil.tf", "*** Add File:infra/evil.tf", " *** Rename: x"]
)
def test_an_unknown_marker_refuses_the_whole_patch(guard, tree, tmp_path, header):
    # a marker this reader does not know may be one a later Codex writes through
    text = f"*** Begin Patch\n*** Add File: ok.txt\n+a\n{header}\n+x\n*** End Patch\n"
    with pytest.raises(ValueError, match="marker"):
        patch_paths(text)
    spec = write_spec(tmp_path / "guard.json", guard, tree)
    code, err = run_main(spec, json.dumps(call("apply_patch", {"command": text}, tree)))
    assert code == 2 and "refused" in err


def test_patch_content_lines_are_not_markers():
    text = (
        "*** Begin Patch\n*** Add File: notes.md\n+*** not a header\n+text\n"
        "*** Update File: a.md\n@@\n-*** old\n+*** new\n*** End of File\n*** End Patch\n"
    )
    assert patch_paths(text) == ["notes.md", "a.md"]


def test_spec_round_trip(guard, tree, tmp_path):
    spec = write_spec(tmp_path / "guard.json", guard, tree)
    loaded, loaded_tree = load_guard(spec)
    assert loaded_tree == tree
    assert (loaded.deny_shell, loaded.allow_shell, loaded.ask_shell) == (
        guard.deny_shell,
        guard.allow_shell,
        guard.ask_shell,
    )
    assert decide(bash("git push", tree), loaded, tree)


def run_main(spec: Path, payload: str) -> tuple[int, str]:
    err = io.StringIO()
    code = main([str(spec)], stdin=io.StringIO(payload), stderr=err)
    return code, err.getvalue()


def test_main_answers_exit_2_with_the_reason_on_stderr(guard, tree, tmp_path):
    spec = write_spec(tmp_path / "guard.json", guard, tree)
    code, err = run_main(spec, json.dumps(bash("git push origin main", tree)))
    assert code == 2 and "git push" in err
    assert run_main(spec, json.dumps(bash("echo hi", tree))) == (0, "")


@pytest.mark.parametrize("payload", ["not json", "[]", "{}", json.dumps({"tool_name": "Bash"})])
def test_malformed_input_is_refused_not_passed(guard, tree, tmp_path, payload):
    spec = write_spec(tmp_path / "guard.json", guard, tree)
    code, err = run_main(spec, payload)
    assert code == 2 and "refused" in err


def test_a_missing_spec_is_refused(tmp_path):
    code, err = run_main(tmp_path / "missing.json", json.dumps({"tool_name": "Bash"}))
    assert code == 2 and "refused" in err


def test_hook_argv_runs_python_isolated(tmp_path):
    argv = hook_argv(tmp_path / "guard.json")
    assert argv[:4] == (sys.executable, "-I", "-m", "sdlc_commons.codex_hook")
    assert argv[4] == str(tmp_path / "guard.json")


def test_the_module_runs_as_a_hook_process(guard, tree, tmp_path):
    spec = write_spec(tmp_path / "guard.json", guard, tree)
    done = subprocess.run(
        list(hook_argv(spec)),
        input=json.dumps(bash("git push origin main", tree)),
        capture_output=True,
        text=True,
        cwd=tree,
        check=False,
    )
    assert done.returncode == 2 and "git push" in done.stderr
