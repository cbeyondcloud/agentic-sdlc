"""A script handed to a shell on its standard input is a command, wherever the
here-string, redirect or process substitution stands. Zyroflo reads many of these since
T697 (pin 96b3e53, M2-58); agentic keeps its own spellings on top, because a review of
the attempt to drop them found shapes only they refuse (decision 108). Data that only
looks like a command stays refused until Zyroflo reads every shape (DF-723)."""

import subprocess

import pytest

from sdlc_commons.guards import Guard


@pytest.fixture
def guard(tmp_path) -> Guard:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    return Guard(repo, "r-shell-stdin")


@pytest.mark.parametrize(
    "command",
    [
        "bash <<< 'rm -rf x'",
        "<<< 'rm -rf x' bash",  # the here-string before the program
        "<<< 'rm -rf x' 2>&1 bash",
        "2>&1 <<< 'rm -rf x' bash",
        "bash 2>&1 <<< 'rm -rf x'",  # a redirect before the here-string
        "bash >log <<< 'rm -rf x'",
        "bash < <(echo rm -rf x)",  # a process substitution as its input
    ],
)
def test_a_script_on_a_shells_input_is_refused(guard, command):
    assert "destructive" in {block.wall for block in guard.command(command)}


@pytest.mark.parametrize(
    "command",
    [
        "echo 'rm -rf x' | sh",  # a printer piped into a shell (DF-681)
        "printf 'rm -rf x' | bash",
        "bash -c \"echo 'rm -rf x' | sh\"",  # the same, inside a command string
        "del -Recurse x",  # PowerShell aliases of Remove-Item (DF-722)
        "erase -r x",
        "del -rec x",
    ],
)
def test_a_script_piped_into_a_shell_and_a_recursive_del_are_refused(guard, command):
    assert "destructive" in {block.wall for block in guard.command(command)}


@pytest.mark.parametrize(
    "command",
    [
        "cat <<< 'rm -rf x' | sh",  # a program passing its stdin on into a shell
        "<<< 'rm -rf x' cat | sh",
        "cat <(echo rm -rf x) | sh",
        "cat < <(echo rm -rf x) | sh",
        "head <<< 'rm -rf x' | sudo sh",
        "tr a a <<< 'rm -rf x' | bash -s",
        "cat <<< 'rm -rf x' |& sh",
        "cat <<< 'rm -rf x' | /bin/sh",
        "cat <<< 'rm -rf x' | env sh",
        "cat <<< 'rm -rf x' | 2>&1 sh",
        "cat <<< 'rm -rf x' | (sh)",  # a subshell or a group reads the pipe too
        "cat <<< 'rm -rf x' | { sh; }",
        "cat <<< 'rmdir /s /q x' | cmd",
        "cat <<< 'Remove-Item -Recurse x' | powershell -",
    ],
)
def test_text_passed_through_a_program_into_a_shell_is_refused(guard, command):
    """Zyroflo reads what echo/printf print into a shell, not what cat, head or tr
    pass on; agentic's own spellings refuse these until it does (M2-58, DF-779)."""
    assert "destructive" in {block.wall for block in guard.command(command)}


@pytest.mark.parametrize(
    "command",
    [
        'bash -c "{ rm -rf x; }"',  # a group in the text an interpreter is told to run
        "sh -c '{ rm -rf x; }' # why",
        'watch "{ rm -rf x; }"',  # watch runs its arguments with sh -c
        "watch -n 1 '{ rm -rf x; }'",
        'watch "{ rm -rf x; }" # why',
        "bash -c \"cat <<< 'rm -rf x' | sh\"",  # a pipe into a shell, in a command string
    ],
)
def test_a_group_or_a_pipe_in_a_command_string_is_refused(guard, command):
    """Zyroflo reads `bash -c 'rm -rf x'` but not a brace group in that text; agentic
    judges the text an interpreter runs as a command of its own (M2-58, DF-780)."""
    assert "destructive" in {block.wall for block in guard.command(command)}


@pytest.mark.parametrize(
    "command",
    [
        # a GIT_* variable set after a separator inside a command string
        "bash -c 'ls; GIT_SSH_COMMAND=\"touch MARK #\" git ls-remote ssh://h/x'",
        "sh -c 'ls | GIT_EXTERNAL_DIFF=x git diff'",
        "sh -c 'export GIT_SSH_COMMAND=x; git fetch'",
        "env sh -c 'ls; GIT_DIR=x git status'",
        "ssh h 'ls; GIT_DIR=x git status'",
        'cmd /c "set GIT_SSH_COMMAND=x && git fetch"',
        # text passed into a shell past a line break, a comment, a group or a compound
        "cat <<< 'rm -rf x' |\n sh",
        "cat <<< 'rm -rf x' |\r\n sh",
        "cat <<< 'rm -rf x' | # why\n sh",
        "cat <<< 'rm -rf x' | (cd /tmp; sh)",
        "cat <<< 'rm -rf x' | { cd /tmp; sh; }",
        "cat <<< 'rm -rf x' | if true; then sh; fi",
        "cat <<< 'rm -rf x' | tee >(sh)",
        "cat <<< 'rm -rf x' | source /dev/stdin",
        "cat <<< 'rm -rf x' | csh",
        "bash /dev/stdin <<< 'rm -rf x'",
        "bash <(cat <<< 'rm -rf x')",
        # a command string behind a redirect, an unlisted carrier or a remote shell
        "2>&1 bash -c '{ rm -rf x; }'",
        "2>&1 sh -c 'r''m -r x'",
        "unshare sh -c '{ rm -rf x; }'",
        "ssh host '{ rm -rf x; }'",
        "at now <<< 'rm -rf x'",
    ],
)
def test_shapes_only_agentics_own_spellings_refuse(guard, command):
    """The M2-58 review found each of these allowed once agentic's spellings were
    dropped, and a real shell runs it (decision 108). They stay refused until Zyroflo
    reads them (DF-779, DF-780)."""
    assert guard.command(command)


@pytest.mark.parametrize(
    "command",
    ["watch 'ls; env PATH=/tmp ls'", 'cmd /c "ls & set PATH=x & ls"'],
)
def test_a_program_search_change_in_a_command_string_is_refused(tmp_path, command):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    allow = ("bash", "sh", "env", "ls", "pytest", "watch", "cmd")
    assert Guard(repo, "r-shell-stdin", allow_shell=allow).command(command)


def test_a_one_file_delete_is_allowed(guard):
    assert guard.command("del -Force x.txt") == []
