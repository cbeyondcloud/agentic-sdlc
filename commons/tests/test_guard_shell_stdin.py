"""A script handed to a shell on its standard input is a command, wherever the
here-string, redirect or process substitution stands. These refusals come from
agentic's own spellings until Zyroflo reads them (M2-58, DF-681): removing that
reader must not allow any of them."""

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
