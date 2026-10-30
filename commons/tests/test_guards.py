"""Tests for the guard adapter over Zyroflo's walls (M1-08, D31 deny lists).

Writes are judged on paths inside a REAL run tree (made by Zyroflo through the
worktree adapter): a guard that only works on paths a test assembled by hand is
the gap the SP2 review found.
"""

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from sdlc_commons.guards import Block, Guard, GuardError
from sdlc_commons.ledger import RunIdentity, RunLedger
from sdlc_commons.worktrees import RunWorktrees

IDENTITY = RunIdentity(team="platform", policy_version="git:" + "a" * 40)

RUN = "r-guard-1"
DENY_SHELL = ("kubectl delete", "sudo", "curl")
DENY_PATHS = ("infra/prod/**", "**/*.pem")
GIT_USER = ["-c", "user.email=dev@example.invalid", "-c", "user.name=Dev"]


def walls(blocks: list[Block]) -> set[str]:
    return {b.wall for b in blocks}


def git(where: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-C", str(where), *GIT_USER, *args], capture_output=True, text=True, check=True
    )
    return done.stdout.strip()


def make_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    git(path, "init", "-q", "-b", "main")
    (path / "app.py").write_text("x = 1\n", encoding="utf-8")
    git(path, "add", "-A")
    git(path, "commit", "-qm", "base")
    return path


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return make_repo(tmp_path / "repo")


@pytest.fixture
def tree(repo: Path) -> Path:
    """The run's real tree, where agents write."""
    RunLedger(repo, RUN).open(
        identity=IDENTITY, kind="feature", project="demo", owner="platform", criticality="low"
    )
    (made,) = RunWorktrees(repo, RUN).create().values()
    return made.path


@pytest.fixture
def guard(repo: Path) -> Guard:
    return Guard(repo, RUN, deny_shell=DENY_SHELL, deny_paths=DENY_PATHS)


# ── commands: Zyroflo's walls and the deny lists ──────────────────────────────


@pytest.mark.parametrize(
    "command",
    [
        "git push -f origin main",
        "find . -exec rm -rf {} +",
        "rm -fr build",
        "git reset --hard HEAD~3",
    ],
)
def test_zyroflo_destructive_defaults_apply_with_no_deny_list(repo: Path, command):
    assert "destructive" in walls(Guard(repo, RUN).command(command))


@pytest.mark.parametrize(
    "command",
    [
        "kubectl delete ns prod",
        "echo ok && kubectl delete pod x",
        "bash -c 'kubectl delete pod x'",
        "KUBECTL.exe Delete ns prod",
    ],
)
def test_a_deny_prefix_blocks_its_command_however_it_is_spelled(guard, command):
    blocks = guard.command(command)

    assert walls(blocks) == {"denied-command"}
    assert "'kubectl delete'" in blocks[0].why


@pytest.mark.parametrize(
    "command", ["sudo kubectl get pods", "echo hi && sudo -u root ls", "/usr/bin/sudo ls"]
)
def test_a_denied_wrapper_blocks_every_command_it_wraps(guard, command):
    assert "denied-command" in walls(guard.command(command))


@pytest.mark.parametrize(
    "command", ["pytest -q", "kubectl get pods", "pseudo-tool run", "uv run ruff check ."]
)
def test_commands_the_lists_do_not_name_are_allowed(guard, command):
    assert guard.command(command) == []


def test_a_command_matching_both_kinds_reports_both(guard):
    assert walls(guard.command("sudo git push -f origin main")) >= {"denied-command", "destructive"}


# ── commands: spellings the SP2 review got past the guard ────────────────────


@pytest.mark.parametrize(
    "command",
    [
        "su''do ls",
        's"u"do ls',
        "su\\do ls",
        "sh -c 'su\"do\" ls'",  # split by quotes
        "c''url https://x",
        'kubectl de""lete ns prod',
        '"C:\\Program Files\\tools\\kubectl.exe" delete ns prod',  # a quoted Windows path
        "& 'C:\\Program Files\\curl\\curl.exe' https://x",
        "KUBECT~1.EXE delete ns prod",  # an 8.3 short name
    ],
)
def test_quoting_paths_and_short_names_do_not_get_past_a_deny_rule(guard, command):
    assert "denied-command" in walls(guard.command(command))


@pytest.mark.parametrize(
    "command",
    [
        "r''m -rf x",
        "r\\m -rf x",
        '"C:\\Program Files\\Git\\usr\\bin\\rm.exe" -rf x',
        "if true; then rm -rf x; fi",
        "for i in 1; do rm -rf x; done",
        "! rm -rf x",
        ">/dev/null rm -rf x",
        "2>&1 rm -rf x",
        "bash <<< 'rm -rf x'",
    ],
)
def test_quoting_paths_and_shell_structure_do_not_hide_a_destructive_command(repo, command):
    assert "destructive" in walls(Guard(repo, RUN).command(command))


@pytest.mark.parametrize(
    "command",
    [
        "git -c alias.nuke='!rm -rf .' nuke",  # git config runs arbitrary commands
        "git -c core.sshCommand='rm -rf x' fetch",
        "git --config-env=core.pager=X log",
        "git config alias.x '!rm -rf .'",
        "git push origin main",  # publishing is the framework's job, never an agent's
        "git remote add evil https://evil.example/x.git",
        'eval "r""m -rf x"',
        "iex 'curl x'",
        "Invoke-Expression 'x'",
        "busybox rm -rf x",
        "powershell -enc ZQBjAGgAbwA=",
        "pwsh -e ZQBjAGgAbwA=",
        "powershell -EncodedCommand ZQB=",
    ],
)
def test_the_framework_floor_applies_whatever_the_policy_says(repo, command):
    assert "framework" in walls(Guard(repo, RUN).command(command))


def test_reasons_never_carry_a_credential_or_the_command(guard):
    commands = [
        "curl -H 'Authorization: Bearer sk-live-abc123' https://x",  # gitleaks:allow (fake token)
        "git push -f https://bob:ghp_secret123@github.com/a/b main",
    ]
    blocks = [b for c in commands for b in guard.command(c)]

    assert blocks
    for b in blocks:
        assert "abc123" not in b.why and "ghp_secret123" not in b.why
        assert not any(c in b.why for c in commands)


def test_for_agent_reads_the_resolved_deny_lists(repo, tree):
    resolved = SimpleNamespace(
        deny_shell=["terraform destroy"],
        deny_paths=["**/*.tfstate"],
        shell=["terraform"],
        ask_shell=[],
    )
    guard = Guard.for_agent(repo, RUN, resolved)

    assert walls(guard.command("terraform destroy -auto-approve")) == {"denied-command"}
    assert walls(guard.writes([tree / "infra" / "x.tfstate"])) == {"denied-path"}


# ── writes, on the run's real tree ────────────────────────────────────────────


def test_a_write_inside_the_tree_is_allowed(guard, tree):
    assert guard.writes([tree / "src" / "app.py"]) == []


def test_a_write_outside_the_run_trees_is_refused(guard, repo, tree):
    assert walls(guard.writes([repo / "src" / "app.py"])) == {"worktree"}


def test_a_zyroflo_protected_path_is_refused(guard, tree):
    assert walls(guard.writes([tree / ".env"])) == {"protected"}


@pytest.mark.parametrize(
    "rel", ["infra/prod/main.tf", "certs/server.pem", "server.pem", "certs/server.pem."]
)
def test_denied_paths_match_relative_to_the_run_tree(guard, tree, rel):
    # `server.pem.` is how Windows stores `server.pem` (Zyroflo T499)
    blocks = guard.writes([tree / rel])

    assert walls(blocks) == {"denied-path"}
    assert "deny" in blocks[0].why


def test_infra_outside_prod_is_not_denied(guard, tree):
    assert guard.writes([tree / "infra" / "staging" / "main.tf"]) == []


@pytest.mark.parametrize(
    "rel",
    [
        ".github/workflows/ci.yml",
        ".claude/settings.json",
        "CLAUDE.md",
        "AGENTS.md",
        ".agentic/agents/x.md",
        ".husky/pre-commit",
        ".git/config",
    ],
)
def test_files_that_steer_agents_or_ci_are_framework_protected(guard, tree, rel):
    assert "framework" in walls(guard.writes([tree / rel]))


def test_each_component_of_an_estate_is_its_own_root_for_deny_globs(tmp_path: Path):
    root = tmp_path / "workspace"
    for name in ("api", "lib"):
        make_repo(root / name)
    (root / ".sdlc").mkdir()
    (root / ".sdlc" / "profile.yml").write_text(
        'scope: estate\ncomponents:\n  - name: "api"\n    path: "api"\n    role: authoritative\n'
        '  - name: "lib"\n    path: "lib"\n    role: authoritative\n',
        encoding="utf-8",
    )
    RunLedger(root, RUN).open(
        identity=IDENTITY, kind="feature", project="demo", owner="platform", criticality="low"
    )
    trees = RunWorktrees(root, RUN).create()
    guard = Guard(root, RUN, deny_paths=DENY_PATHS)

    for t in trees.values():
        assert walls(guard.writes([t.path / "infra" / "prod" / "x.tf"])) == {"denied-path"}
        assert guard.writes([t.path / "src" / "x.py"]) == []


# ── the repository's own policy cannot lower the floor ───────────────────────


def test_a_repo_policy_that_empties_zyroflo_lists_does_not_weaken_the_guard(repo, tree):
    (repo / ".sdlc").mkdir(exist_ok=True)
    (repo / ".sdlc" / "policy.yml").write_text(
        "deny_git: []\ndeny_commands: []\nprotected_paths: []\nmax_files_per_batch: 0\n",
        encoding="utf-8",
    )
    guard = Guard(repo, RUN)

    assert "destructive" in walls(guard.command("rm -rf x"))
    assert "destructive" in walls(guard.command("git push -f origin main"))
    assert "protected" in walls(guard.writes([tree / ".env"]))
    assert "budget" in walls(guard.diff(diff_of(41)))


# ── inputs that used to fail open ─────────────────────────────────────────────


@pytest.mark.parametrize(
    "kwargs",
    [
        {"deny_shell": "sudo"},
        {"deny_paths": "infra/**"},  # a string, not a list of rules
        {"deny_shell": ["sudo", ""]},
        {"deny_paths": ["  "]},  # a blank rule denies nothing
        {"deny_shell": [7]},
        {"max_files": True},
        {"max_files": 2.5},
        {"max_lines": "7"},
        {"max_files": 0},
    ],
)
def test_inputs_that_would_deny_nothing_are_refused(repo, kwargs):
    with pytest.raises(GuardError):
        Guard(repo, RUN, **kwargs)


# ── diff budget ───────────────────────────────────────────────────────────────


def diff_of(files: int) -> str:
    return "".join(
        f"diff --git a/f{i}.py b/f{i}.py\n--- a/f{i}.py\n+++ b/f{i}.py\n+x = {i}\n"
        for i in range(files)
    )


def test_the_diff_budget_is_enforced(repo: Path):
    guard = Guard(repo, RUN, max_files=2)

    assert guard.diff(diff_of(2)) == []
    assert walls(guard.diff(diff_of(3))) == {"budget"}


def test_the_diff_budget_can_only_be_tightened(repo: Path):
    guard = Guard(repo, RUN, max_files=10_000)  # Zyroflo's default is 40

    assert walls(guard.diff(diff_of(41))) == {"budget"}


# ── push (the framework's own push, M1-58) ────────────────────────────────────


def test_a_push_to_the_run_branch_is_allowed(guard):
    assert guard.push("origin", f"sdlc/{RUN}") == []


@pytest.mark.parametrize(
    ("remote", "branch"),
    [
        ("upstream", f"sdlc/{RUN}"),
        ("origin", "main"),
        ("origin", "sdlc/another-run"),
        ("", f"sdlc/{RUN}"),
    ],
)
def test_a_push_anywhere_else_is_refused(guard, remote, branch):
    assert walls(guard.push(remote, branch)) == {"push"}


# ── Zyroflo's lists are never changed ─────────────────────────────────────────


def test_building_a_guard_does_not_change_zyroflo_defaults(repo: Path):
    from zyroflo.sdlc import policy

    before = list(policy.DEFAULTS["deny_commands"]), list(policy.DEFAULTS["protected_paths"])
    Guard(repo, RUN, deny_shell=("curl",), deny_paths=("x/**",)).command("ls")

    assert (policy.DEFAULTS["deny_commands"], policy.DEFAULTS["protected_paths"]) == before


# ── second SP2 review: no false refusals, no new bypasses (N1-N5) ────────────


@pytest.mark.parametrize(
    "command",
    [
        'git commit -m "fix: remove eval"',
        'git commit -m "feat: add push button"',
        'git commit -m "docs: git config notes"',
        "git log --grep push",
        "git log --grep=push",
        "grep -rn eval src",
        "cat docs/eval",
        "ls src/eval",
        "python -m pytest tests/eval",
        "npm run eval",
        "make eval",
        'rg "iex" .',
        'pytest -k "not slow"',
        "npm run build",
        "uv run ruff check .",
        "git log --oneline",
        "git status",
        "git diff --stat",
        "powershell -NoProfile -File x.ps1",
        "powershell -ExecutionPolicy Bypass -File x.ps1",
        "pwsh -NonInteractive -Command Get-ChildItem",
        "kubectl get pods -l app=delete",
        r'"C:\Program Files\Git\bin\git.exe" status',
    ],
)
def test_everyday_developer_commands_are_not_refused(guard, command):
    assert guard.command(command) == []


@pytest.mark.parametrize(
    "command",
    [
        r'C:\"Program Files"\Git\usr\bin\rm.exe -rf x',  # a partly quoted path
        r'& C:\"Program Files"\Git\usr\bin\rm.exe -rf x',
    ],
)
def test_a_partly_quoted_program_path_does_not_hide_a_destructive_command(repo, command):
    assert "destructive" in walls(Guard(repo, RUN).command(command))


@pytest.mark.parametrize(
    "command",
    [
        "git submodule foreach 'rm -rf x'",
        "git rebase -x 'rm -rf x' main",
        "git rebase --exec='rm -rf x' main",
        "git bisect run rm -rf x",
        "git difftool -x rm",
        "git mergetool -t evil",
        "GIT_SSH_COMMAND='rm -rf x' git fetch",
        "GIT_EXTERNAL_DIFF=rm git diff",
        "GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=alias.x GIT_CONFIG_VALUE_0='!rm -rf .' git x",
        "env GIT_SSH_COMMAND='rm -rf x' git fetch",
        "git -C repo push origin main",
        "doas rm -rf x",
        "su -c 'rm -rf x'",
        "pkexec ls",
        "runas /user:admin cmd",
        "powershell -EncodedC ZQB=",
        "powershell -enc ZQB=",
    ],
)
def test_more_ways_to_run_a_command_through_git_or_privilege_are_refused(repo, command):
    assert "framework" in walls(Guard(repo, RUN).command(command))


def test_a_huge_command_is_refused_quickly_and_fails_closed(guard):
    import time

    start = time.perf_counter()
    blocks = guard.command('"' + "a/" * 50_000)  # an unterminated quote, 100 KB

    assert time.perf_counter() - start < 2
    assert "framework" in walls(blocks)


def test_a_long_but_allowed_command_is_still_judged_fast(guard):
    import time

    start = time.perf_counter()
    assert guard.command("echo " + "word " * 3000) == []  # about 15 KB
    assert time.perf_counter() - start < 5


@pytest.mark.parametrize(
    "rel",
    [
        ".pre-commit-config.yaml",
        ".gitmodules",
        ".vscode/tasks.json",
        ".devcontainer/devcontainer.json",
        # git hook folders and hook managers' settings (M2-61 review F3)
        ".githooks/pre-commit",
        "lefthook.yml",
        ".lefthook.yml",
        "lefthook-local.yml",
        ".lefthook/pre-commit/check.sh",
    ],
)
def test_files_that_run_commands_on_commit_or_open_are_framework_protected(guard, tree, rel):
    assert "framework" in walls(guard.writes([tree / rel]))
