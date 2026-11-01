"""The second M2-58 review, of a1eef0e: a brace glued to a command or a `${x}` prefix
(I-A), Start-Process with its parameters in another order (I-B), a line continuation
(I-C), a script piped into a program that reads it (I-D), and the minor findings."""

import subprocess

import pytest

from sdlc_commons.guards import Guard


@pytest.fixture
def guard(tmp_path) -> Guard:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    return Guard(repo, "r-m2-58-review2")


def _refused(guard: Guard, command: str) -> bool:
    return "destructive" in {block.wall for block in guard.command(command)}


@pytest.mark.parametrize(
    "command",
    [
        # PowerShell script blocks glued to the command (each deleted files on Windows)
        "if($true){Remove-Item -LiteralPath f1 -Recurse}",
        "& {Remove-Item -Recurse x}",
        "try{ri -r x}catch{}",
        "if (Test-Path x) {Remove-Item x -Recurse -Force}",
        "&{rm -r x}",
        "icm {rm -r x}",
        "%{ri -r x}",
        "foreach($i in 1){Remove-Item -Recurse x}",
        "$sb={ri -r x};& $sb",
        'powershell -c "if($true){Remove-Item -Recurse x}"',
        # bash: a function body or group glued to its brace, a `${x}` prefix
        "f(){ rm -rf x;};f",
        "{rm -rf x;}",
        "${IFS}rm -rf x",
        "${x}rm -rf x",
        "sudo ${x}rm -rf x",
    ],
)
def test_a_glued_brace_or_a_parameter_prefix_hides_no_program(guard, command):
    assert _refused(guard, command)


@pytest.mark.parametrize(
    "command",
    [
        "Remove-Item `\n-Recurse x",  # a line continuation inside the command (I-C)
        "Remove-Item -Path x `\n-Recurse -Force",
        "Remove-Item -Path x `\r\n-Recurse -Force",
        "ri `\n-r x",
        "find . `\n-delete",
        'powershell -c "Remove-Item `\n-Recurse x"',
        "rmdir /s ^\n/q x",
        "rm -r\\\n -f x",
    ],
)
def test_a_line_continuation_is_joined(guard, command):
    assert _refused(guard, command)


@pytest.mark.parametrize(
    "command",
    [
        "Start-Process -ArgumentList '-rf','x' -FilePath rm",  # parameters reordered (I-B)
        "Start-Process -ArgumentList '/c','rd /s /q x' -FilePath cmd",
        "Start-Process -ArgumentList '-c','Remove-Item -Recurse x' -FilePath powershell",
        "Start-Process -ArgumentList '-Command','Remove-Item -Recurse x' -FilePath pwsh",
        "Start-Process rm '-rf','x'",  # program and arguments by position
        "Start-Process -Wait -FilePath rm -ArgumentList '-rf', 'x'",
        "saps -Args '-rf','x' -Path rm",
    ],
)
def test_start_process_reads_its_program_and_arguments_in_any_order(guard, command):
    assert _refused(guard, command)


def test_start_process_with_a_harmless_command_stays_allowed(guard):
    assert guard.command("Start-Process pwsh -ArgumentList '-Command','Remove-Item x.txt'") == []


@pytest.mark.parametrize(
    "command",
    [
        "echo 'rm -rf x' | sudo -s",  # a script piped into a program that runs it (I-D)
        "echo 'rm -rf x' | parallel",
        "echo 'rm -rf x' | irb",
        "echo 'rm -rf x' | strace sh",
        "echo 'rm -rf x' | xargs -0 sh -c",
        "echo 'rm -rf x' | bash -c \"$(cat)\"",
        "echo 'rm -rf x' | ksh93",
        "ksh93 -c 'rm -rf x'",  # shells Zyroflo does not know
        "yash -c 'rm -rf x'",
        "rbash -c 'rm -rf x'",
        "oksh -c 'rm -rf x'",
        'sg docker -c "rm -rf x"',  # minor: sg's command string
    ],
)
def test_a_script_piped_into_a_program_that_runs_it_is_refused(guard, command):
    assert _refused(guard, command)


@pytest.mark.parametrize(
    "command",
    [
        "ls | xargs echo rm -r",  # xargs runs echo: it prints, nothing is removed
        "echo a | xargs echo rm -rf",
        "xargs echo rm -rf < f",
        "echo 'rm -rf x' | parallel --dry-run",
    ],
)
def test_xargs_or_parallel_running_a_printer_stays_allowed(guard, command):
    assert guard.command(command) == []


@pytest.mark.parametrize(
    "command",
    [
        "echo 'rm -rf x' | ssh host cat",  # each pipes data into a program of its own
        "echo 'rm -rf x' | python3 script.py --flag",
        "echo 'rm -rf x' | psql",
        "echo 'rm -rf x' | tee note.txt",
        "echo 'rm -rf x' | sudo tee f",
        "echo 'rm -rf x' | docker exec -i c cat",
        "echo 'rm -rf x' | gh pr comment 1 -F -",
    ],
)
def test_data_piped_into_a_program_of_its_own_stays_allowed(guard, command):
    assert guard.command(command) == []
