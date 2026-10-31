"""A script handed to a shell on its standard input is a command, wherever the
here-string, redirect or process substitution stands. Zyroflo reads these since T697 and
T703 (pin 0ea0477, M2-58), so agentic asks Zyroflo alone. The shapes a review of the first
attempt to drop agentic's own spellings found (decision 108) stay pinned here, and data
that only looks like a command is allowed (DF-723)."""

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
    """Zyroflo follows text through any program into a shell since T703 (DF-779)."""
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
    """Zyroflo reads a brace group in the text an interpreter runs since T703 (DF-780)."""
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
def test_shapes_the_m2_58_review_found_are_refused(guard, command):
    """The M2-58 review found each of these allowed once agentic's spellings were
    dropped, and a real shell runs it (decision 108). Zyroflo reads them since T703
    (DF-779, DF-780, DF-782)."""
    assert guard.command(command)


@pytest.mark.parametrize(
    "command",
    [
        "find . -exec bash \\; <<< 'rm -rf x'",  # find hands its stdin to the shell it runs
        "find . -exec sh -c 'unshare sh -c bash' \\; <<< 'rm -rf x'",
        "echo 'rm -rf x' | find . -exec bash \\;",
        "sh -c 'unshare sh -c bash' <<< 'rm -rf x'",  # a shell with no script, carried
        "echo 'rm -rf x' | sh -c 'unshare sh -c bash'",
        "sh -c 'sudo -s' <<< 'rm -rf x'",
        "find . -exec ssh h \\; <<< 'rm -rf x'",
        "find . -exec at now \\; <<< 'rm -rf x'",
        # a command string run inside a process substitution
        'cat <(sh -c "{ rm -rf x; }")',
        'cat <(echo ls; sh -c "{ rm -rf x; }")',
        "sudo bash -c 'cat <(echo ls; sh -c \"{ rm -rf x; }\")'",
    ],
)
def test_a_shell_zyroflo_does_not_follow_is_refused(guard, command):
    """Zyroflo 0ea0477 follows neither a script on the stdin of a command into a shell
    with no script of its own that a carrier runs, nor a command string inside a process
    substitution; a real bash runs each. agentic judges such a command in its own
    quote-free spellings too (M2-58)."""
    assert "destructive" in {block.wall for block in guard.command(command)}


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


@pytest.mark.parametrize(
    "command",
    [
        "echo 'ls; GIT_DIR=x git status' | sh",  # a script a shell takes from its stdin
        "cat <<< 'ls; GIT_DIR=x git status' | sh",
        "sh <<< 'ls; GIT_DIR=x git status'",
        "echo 'ls; export GIT_SSH_COMMAND=x; git fetch' | bash",
    ],
)
def test_a_git_variable_in_a_script_on_a_shells_input_is_refused(guard, command):
    """Zyroflo's spellings carry a command string's text, not a stdin script, so the
    GIT_* wall also reads each spelling with its quotes removed (M2-58)."""
    assert "framework" in {block.wall for block in guard.command(command)}


@pytest.mark.parametrize("command", ["echo 'ls; PATH=/tmp ls' | sh", "bash <<< 'ls; PATH=/tmp ls'"])
def test_a_program_search_change_on_a_shells_input_is_refused(tmp_path, command):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    allow = ("bash", "sh", "echo", "ls")
    assert Guard(repo, "r-shell-stdin", allow_shell=allow).command(command)


@pytest.mark.parametrize(
    "command",
    [
        "cat <<< 'rm -rf x'",  # a here-string fed to a program that reads data (DF-723)
        "grep -c rm <<< 'rm -rf x'",
        "bash run.sh <<< 'rm -rf x'",  # bash runs run.sh; the here-string is its data
        "cat <(echo rm -rf x)",
        'echo "{ rm -rf x; }"',  # quoted text is data
        "echo do rm -rf x",  # a keyword in argument position is a word
        "echo if rm -rf x",
        "echo ! rm -rf x",
        "cat <<< 'rm -rf x' | grep bash",  # the pipe goes to grep, not a shell
        'sudo "{ rm -rf x; }"',  # a program named "{ rm -rf x; }": nothing runs it
        'find "{ rm -rf x; }"',
    ],
)
def test_data_that_only_looks_like_a_command_is_allowed(guard, command):
    """Zyroflo Z10 (pin 0ea0477) reads every shape agentic's own spellings were kept
    for, so those spellings are gone and their over-refusals with them (DF-723)."""
    assert guard.command(command) == []


def test_a_one_file_delete_is_allowed(guard):
    assert guard.command("del -Force x.txt") == []
