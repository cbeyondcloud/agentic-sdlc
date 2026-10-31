"""The M2-58 review of dropping agentic's spellings loop on Zyroflo 0ea0477: each shape
here was refused by the loop, allowed once it was gone, and runs a destroying command in
a real shell (I1-I3, M1-M3). DF-723's harmless shapes stay allowed
(test_guard_shell_stdin.py)."""

import subprocess

import pytest

from sdlc_commons.guards import Guard


@pytest.fixture
def guard(tmp_path) -> Guard:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    return Guard(repo, "r-m2-58-review")


def _refused(guard: Guard, command: str) -> bool:
    return "destructive" in {block.wall for block in guard.command(command)}


@pytest.mark.parametrize(
    "command",
    [
        "Remove-It`em -Recurse x",  # PowerShell's escape character inside a word (I1)
        'powershell -c "Remove-It`em -Recurse x"',
        "git pus`h -f",
        "git res`et --hard",
        "f`ind . -delete",
        "r`d /s /q x",
        "d`el -Recurse x",
        'cmd /c "git pus`h -f"',
        'pwsh -NoProfile -Command "& { r`m -rf x }"',
        "flock f -c 'git re`set --hard'",
        "tmux new 'git re`set --hard'",
        "Start-Process git re`set --hard",
    ],
)
def test_a_backtick_escape_is_refused(guard, command):
    assert _refused(guard, command)


@pytest.mark.parametrize(
    "command",
    [
        "env -S 'rm -\"\"rf x'",  # a quote inside a word in text a carrier runs (I2)
        'echo "$(git pus""h -f)"',
        'echo "`git re""set --hard`"',
        "trap 'git re\"\"set --hard' EXIT",
        "alias a='git re\"\"set --hard'; a",
        'osascript -e \'do shell script "git re""set --hard"\'',
        "Start-Process powershell -ArgumentList 'git re\"\"set --hard'",
        'python -c \'import os; os.system("git r""eset --hard")\'',
    ],
)
def test_a_quote_inside_a_word_in_carried_text_is_refused(guard, command):
    assert _refused(guard, command)


@pytest.mark.parametrize(
    "command",
    [
        "cat <(watch '{ rm -rf x; }')",  # any command in a process substitution runs (M1)
        "cat <(env -S '{ rm -rf x; }')",
        "cat <(script -qc '{ rm -rf x; }' /dev/null)",
        "cat <(flock f -c '{ rm -rf x; }')",
        "cat <(tmux new '{ rm -rf x; }')",
        "ls | tee >(ssh h '{ rm -rf x; }')",
    ],
)
def test_any_carrier_in_a_process_substitution_is_refused(guard, command):
    assert _refused(guard, command)


@pytest.mark.parametrize(
    "command",
    [
        "find . -exec bash -O extglob \\; <<< 'rm -rf x'",  # an option's value (M2)
        "find . -exec bash -o pipefail \\; <<< 'rm -rf x'",
        "find . -exec bash --rcfile f \\; <<< 'rm -rf x'",
        "find . -exec bash --init-file f \\; <<< 'rm -rf x'",
        "find . -exec zsh -o errexit \\; <<< 'rm -rf x'",
        "find . -exec powershell -NoProfile \\; <<< 'Remove-Item -Recurse x'",
        "find . -exec pwsh -nop \\; <<< 'Remove-Item -Recurse x'",
        "sh -c 'sudo --login' <<< 'rm -rf x'",
        "sh -c 'sudo --shell' <<< 'rm -rf x'",
    ],
)
def test_a_shell_option_value_is_not_its_script(guard, command):
    assert _refused(guard, command)


@pytest.mark.parametrize(
    "carrier",
    [
        "faketime x bash",  # programs that run a shell reading the outer stdin (M3)
        "dbus-launch bash",
        "login",
        "newgrp",
        "nsenter -t 1 bash",
        "setpriv bash",
        "taskset 1 bash",
        "systemd-run --pty bash",
        "script -q /dev/null",
        "ksh93",
        "time bash",
        "numactl -C 0 bash",
        "sg grp bash",
        "expect",
        "tmux",
        "screen",
    ],
)
def test_a_shell_behind_any_carrier_reads_the_outer_stdin(guard, carrier):
    assert _refused(guard, f"sh -c '{carrier}' <<< 'rm -rf x'")


@pytest.mark.parametrize(
    "command",
    [
        "parallel <<< 'rm -rf x'",  # indirection (I3)
        "while read l; do $l; done <<< 'rm -rf x'",
        "read c <<< 'rm -rf x'; $c",
        "mapfile -t a <<< 'rm -rf x'; \"${a[@]}\"",
        "bash -c \"$(cat <<< 'rm -rf x')\"",
        "python -c 'import os; os.system(input())' <<< 'rm -rf x'",
        "cat <<< 'rm -rf x' > s.sh; sh s.sh",
    ],
)
def test_text_reaching_a_program_by_indirection_is_refused(guard, command):
    assert _refused(guard, command)


@pytest.mark.parametrize(
    "command",
    [
        "find . -exec sh -c 'busybox sh' \\; <<< 'rm -rf x'",
        "sh -c 'sg x' <<< 'rm -rf x'",
        "find . -exec sh -c 'sg x' \\; <<< 'rm -rf x'",
        "xargs <<< 'rm -rf x'",
        "xargs -n1 -a /dev/stdin sh -c <<< 'rm -rf x'",
        "find . -exec mosh h \\; <<< 'rm -rf x'",
        "sh -c 'mosh h' <<< 'rm -rf x'",
        "find . -exec telnet h \\; <<< 'rm -rf x'",
        "sh -c 'telnet h' <<< 'rm -rf x'",
        "find . -exec nc h 22 \\; <<< 'rm -rf x'",
        "sh -c 'nc h 22' <<< 'rm -rf x'",
        "find . -exec kubectl exec -i p -- sh \\; <<< 'rm -rf x'",
        "find . -exec lima sh \\; <<< 'rm -rf x'",
        "sh -c 'lima sh' <<< 'rm -rf x'",
        "find . -exec colima ssh \\; <<< 'rm -rf x'",
        "sh -c 'colima ssh' <<< 'rm -rf x'",
        "find . -exec vagrant ssh \\; <<< 'rm -rf x'",
        "sh -c 'vagrant ssh' <<< 'rm -rf x'",
        "find . -exec multipass shell \\; <<< 'rm -rf x'",
        "sh -c 'multipass shell' <<< 'rm -rf x'",
        "find . -exec gdb \\; <<< 'rm -rf x'",
        "sh -c 'gdb' <<< 'rm -rf x'",
        "find . -exec irb \\; <<< 'rm -rf x'",
        "sh -c 'irb' <<< 'rm -rf x'",
        "xargs -0 sh -c <<< 'rm -rf x'",
    ],
)
def test_any_program_that_may_run_its_stdin_is_judged_whole(guard, command):
    """A remote, emulated or interactive shell, a REPL, a debugger or xargs may run
    what reaches its stdin; no list of them is complete, so a stdin source with any
    program that is not a known data reader is judged in agentic's own spellings (I3)."""
    assert _refused(guard, command)


@pytest.mark.parametrize(
    "command",
    [
        # PowerShell's argument array: each element is a word of the command it starts;
        # each deleted the folder when run on Windows (refused by neither guard before)
        "Start-Process -Wait powershell -ArgumentList '-NoProfile','-c','rm -r x'",
        "Start-Process powershell -ArgumentList '-c','Remove-Item x -Recurse -Force'",
        "Start-Process powershell -ArgumentList '-c','Re`move-Item x -Recurse -Force'",
        "Start-Process powershell -ArgumentList '-NoProfile','-c','del -Rec\"\"urse x'",
    ],
)
def test_a_powershell_argument_array_is_read_as_words(guard, command):
    assert _refused(guard, command)


@pytest.mark.parametrize(
    "command",
    [
        "strace 2>&1 rm -fr x",  # a redirect between a wrapper Zyroflo does not list
        'strace 2>&1 r""m -fr x',  # and the program it runs
        "ltrace >log rm -rf x",
        "nsenter -t 1 2>/dev/null rm -rf x",
    ],
)
def test_a_redirect_between_a_wrapper_and_its_program_is_refused(guard, command):
    assert _refused(guard, command)


@pytest.mark.parametrize(
    "command",
    [
        "strace rm -fr x",  # wrappers agentic lists and Zyroflo does not
        "ltrace -f rm -rf x",
        "nsenter -t 1 rm -rf x",
        "faketime x rm -rf x",
        "bash -c 'strace 2>&1 rm -rf x'",
    ],
)
def test_a_program_behind_a_wrapper_zyroflo_does_not_list_is_judged(guard, command):
    assert _refused(guard, command)


@pytest.mark.parametrize(
    "command", ['strace git commit -m "rm -rf x"', "nsenter -t 1 git log --grep 'rm -rf x'"]
)
def test_a_quoted_argument_behind_a_wrapper_stays_data(guard, command):
    assert guard.command(command) == []


def test_a_command_too_deep_to_read_is_refused_unread(guard):
    """Zyroflo's reader recurses once per nested command string; a command deeper
    than Python's stack is refused unread instead of raising (DF-932)."""
    blocks = guard.command(("bash -c '" * 2000)[:16384])
    assert [block.wall for block in blocks] == ["framework"]
    assert "refused unread" in blocks[0].why
