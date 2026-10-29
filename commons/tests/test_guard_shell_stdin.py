"""A script handed to a shell on its standard input is a command, wherever the
here-string, redirect or process substitution stands. Zyroflo reads these since
T697 (pin 96b3e53, M2-58); agentic keeps its own reading only for the shapes Zyroflo
still misses (DF-779, DF-780), and data that only looks like a command is allowed
(DF-723)."""

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
        "cat <<< 'rm -rf x'",  # a here-string fed to a program that reads data (DF-723)
        "grep -c rm <<< 'rm -rf x'",
        "bash run.sh <<< 'rm -rf x'",  # bash runs run.sh; the here-string is its data
        "cat <(echo rm -rf x)",
        'echo "{ rm -rf x; }"',  # quoted text is data
        "echo do rm -rf x",  # a keyword in argument position is a word
        "echo if rm -rf x",
        "del -Force x.txt",  # one file, not a tree
        "cat <<< 'rm -rf x' | grep bash",  # the pipe goes to grep, not a shell
        "cat <<< 'rm -rf x' || sh",  # `||` runs sh with nothing on its stdin
        'sudo "{ rm -rf x; }"',  # a program named "{ rm -rf x; }": nothing runs it
        'find "{ rm -rf x; }"',
        'powershell "{ rm -rf x; }"',  # a script block literal, printed, not run
    ],
)
def test_data_that_only_looks_like_a_command_is_allowed(guard, command):
    assert guard.command(command) == []
