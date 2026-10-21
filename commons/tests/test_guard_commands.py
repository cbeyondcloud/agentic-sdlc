"""The command guard as a table: what must be refused, what must stay allowed.

Every command here is DATA handed to `Guard.command` - none is ever run. The
rows come from the three SP2 review rounds (Sonnet 5.5): each refused row is a
bypass that was found, each allowed row a false refusal that was found.
"""

import pytest

from sdlc_commons.guards import Guard

RUN = "r-table-1"
DENY_SHELL = (
    "kubectl delete",
    "curl",
    "sudo",
    "terraform destroy",
    "aws s3 rm",
    "docker run --privileged",
)

#: (command, the wall that must refuse it)
MUST_REFUSE = [
    # R3-1: a newline (or CRLF) separates commands
    ("ls\ngit push origin main", "framework"),
    ("git status\r\ngit push origin main", "framework"),
    ("echo hi\nkubectl delete ns prod", "denied-command"),
    ("true\n  sudo ls", "denied-command"),
    # R3-2: a short-flag cluster ending in c runs its argument
    ("bash -lc 'curl x'", "denied-command"),
    ('bash -lc "kubectl delete ns"', "denied-command"),
    ("sh -ec 'curl x'", "denied-command"),
    ('sh -xc "curl x"', "denied-command"),
    ("cmd /c curl x", "denied-command"),
    ('powershell -Command "curl x"', "denied-command"),
    ("find . -exec curl x ;", "denied-command"),
    # R3-3: control keywords
    ("if curl x; then echo; fi", "denied-command"),
    ("while curl x; do :; done", "denied-command"),
    ("until curl x; do :; done", "denied-command"),
    ("if true; then git push origin main; fi", "framework"),
    # R3-4: options with values before the subcommand
    ("kubectl --as admin delete ns", "denied-command"),
    ("kubectl --server https://x delete ns", "denied-command"),
    ("kubectl -s https://x delete ns", "denied-command"),
    ("kubectl -l app=x delete pod", "denied-command"),
    ("kubectl -v 9 delete ns x", "denied-command"),
    ("terraform -chdir x destroy", "denied-command"),
    ("aws --profile p s3 rm s3://b --recursive", "denied-command"),
    ("aws --region us-east-1 s3 rm s3://b", "denied-command"),
    ("git --attr-source x push", "framework"),
    ("git -c alias.nuke=!rm nuke", "framework"),
    ("git -C repo -c core.pager=x log", "framework"),
    ("docker -H tcp://x run --privileged alpine", "denied-command"),
    # R3-5: programs that run their arguments
    ("ssh host curl x", "denied-command"),
    ("npx curl x", "denied-command"),
    ("uv run curl x", "denied-command"),
    ("strace curl x", "denied-command"),
    ("flock /tmp/l curl x", "denied-command"),
    ("wsl curl x", "denied-command"),
    ("wsl -e curl x", "denied-command"),
    ("docker exec c curl x", "denied-command"),
    ("docker run --rm alpine curl x", "denied-command"),
    ("timeout 5 kubectl delete ns", "denied-command"),
    ("sudo -u root kubectl delete ns", "denied-command"),
    ("nice -n 5 curl x", "denied-command"),
    ("& { curl x }", "denied-command"),
    ("$(curl x)", "denied-command"),
    ("`curl x`", "denied-command"),
    ("exec curl x", "denied-command"),
    # R3-6: ANSI-C and locale quoting
    ("$'git' push", "framework"),
    ("$'su\\x64o' ls", "framework"),
    ('$"sudo" ls', "framework"),
    # R3-7: cmd's doubled quotes around a program with a space in its path
    ('cmd /c ""C:\\Program Files\\Git\\usr\\bin\\rm.exe" -rf x"', "destructive"),
]

MUST_ALLOW = [
    # everyday commands from the review rounds
    'git commit -m "fix: remove eval"',
    'git commit -m "feat: add push button"',
    "git commit -m push",
    "git log --grep push",
    "git log -S eval",
    "git log --oneline",
    "make eval",
    "npm run eval",
    "npm run build",
    "pytest -k su",
    'pytest -k "not slow"',
    "python -m eval",
    "grep -r iex .",
    "uv run ruff check .",
    'python -c "print(1)"',
    "bash -c ls",
    "find . -name su -exec cat {} ;",
    "kubectl get pods -o name",
    "powershell -ExecutionPolicy Bypass -File build.ps1",
    "powershell -NoProfile -File x.ps1",
    # remaining false refusals from round 3
    "time pytest tests/unit",
    "rg -c eval",
    "git grep -c eval",
    'echo "x" > su',
    "nice -n 5 make build",
    "xargs echo hello",
]


@pytest.fixture(scope="module")
def guard(tmp_path_factory) -> Guard:
    return Guard(tmp_path_factory.mktemp("repo"), RUN, deny_shell=DENY_SHELL)


@pytest.mark.parametrize(("command", "wall"), MUST_REFUSE)
def test_must_refuse(guard: Guard, command: str, wall: str):
    assert wall in {b.wall for b in guard.command(command)}


@pytest.mark.parametrize("command", MUST_ALLOW)
def test_must_allow(guard: Guard, command: str):
    assert guard.command(command) == []


def test_judging_a_command_at_the_size_cap_stays_fast(guard: Guard):
    import time

    for command in ["sudo " + "word " * 3270, "nice -n 5 " * 1630, 'sudo "a b" ' * 1480]:
        start = time.perf_counter()
        guard.command(command[:16_000])
        assert time.perf_counter() - start < 1.5, command[:20]


def test_a_command_nesting_too_many_programs_is_refused_unread(guard: Guard):
    blocks = guard.command("nice -n 5 " * 40 + "ls")

    assert "framework" in {b.wall for b in blocks}


def test_git_change_directory_is_not_git_config(guard: Guard):
    assert guard.command("git -C repo log --oneline") == []
