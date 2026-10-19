"""Tests for the guard adapter over Zyroflo's walls (M1-08, D31 deny lists)."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from sdlc_commons.guards import Block, Guard

RUN = "r-guard-1"


def walls(blocks: list[Block]) -> list[str]:
    return [b.wall for b in blocks]


@pytest.fixture
def guard(tmp_path: Path) -> Guard:
    return Guard(tmp_path, RUN, deny_shell=("kubectl delete", "sudo", "curl"),
                 deny_paths=("infra/prod/**", "**/*.pem"))  # fmt: skip


def in_worktree(guard: Guard, rel: str) -> Path:
    return guard.worktree / rel


# ── commands ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("command", ["git push -f origin main", "find . -exec rm -rf {} +",
                                     "rm -fr build", "git reset --hard HEAD~3"])  # fmt: skip
def test_zyroflo_destructive_defaults_apply_with_no_deny_list(tmp_path: Path, command):
    assert set(walls(Guard(tmp_path, RUN).command(command))) == {"destructive"}


@pytest.mark.parametrize("command", [
    "kubectl delete ns prod",
    "echo ok && kubectl delete pod x",
    "bash -c 'kubectl delete pod x'",
    "KUBECTL.exe Delete ns prod",
])  # fmt: skip
def test_a_deny_prefix_blocks_its_command_however_it_is_spelled(guard, command):
    blocks = guard.command(command)

    assert walls(blocks) == ["denied-command"]
    assert "'kubectl delete'" in blocks[0].why


@pytest.mark.parametrize("command", ["sudo kubectl get pods", "echo hi && sudo -u root ls",
                                     "/usr/bin/sudo ls"])  # fmt: skip
def test_a_denied_wrapper_blocks_every_command_it_wraps(guard, command):
    # Zyroflo looks THROUGH sudo to the command it runs; a rule naming sudo itself
    # must still refuse it
    assert walls(guard.command(command)) == ["denied-command"]


@pytest.mark.parametrize("command", ["pytest -q", "kubectl get pods", "pseudo-tool run",
                                     "uv run ruff check ."])  # fmt: skip
def test_commands_the_lists_do_not_name_are_allowed(guard, command):
    assert guard.command(command) == []


def test_a_command_matching_both_kinds_reports_both(guard):
    walls_hit = set(walls(guard.command("sudo git push -f origin main")))

    assert walls_hit == {"denied-command", "destructive"}


def test_reasons_never_carry_a_credential_from_the_command(guard):
    blocks = guard.command("curl -H 'Authorization: Bearer sk-live-abc123' https://x")
    blocks += guard.command("git push -f https://bob:ghp_secret123@github.com/a/b main")

    assert blocks and all("abc123" not in b.why and "ghp_secret123" not in b.why for b in blocks)


def test_for_agent_reads_the_resolved_deny_lists(tmp_path: Path):
    resolved = SimpleNamespace(deny_shell=["terraform destroy"], deny_paths=["**/*.tfstate"])
    guard = Guard.for_agent(tmp_path, RUN, resolved)

    assert walls(guard.command("terraform destroy -auto-approve")) == ["denied-command"]
    assert walls(guard.writes([in_worktree(guard, "infra/x.tfstate")])) == ["denied-path"]


# ── writes ────────────────────────────────────────────────────────────────────


def test_a_write_inside_the_worktree_is_allowed(guard):
    assert guard.writes([in_worktree(guard, "src/app.py")]) == []


def test_a_write_outside_the_run_worktree_is_refused(guard, tmp_path: Path):
    assert walls(guard.writes([tmp_path / "src" / "app.py"])) == ["worktree"]


def test_a_zyroflo_protected_path_is_refused(guard):
    assert walls(guard.writes([in_worktree(guard, ".env")])) == ["protected"]


@pytest.mark.parametrize("rel", ["infra/prod/main.tf", "certs/server.pem", "server.pem",
                                 "certs/server.pem."])  # fmt: skip
def test_denied_paths_match_relative_to_the_worktree(guard, rel):
    # `server.pem.` is how Windows stores `server.pem` (Zyroflo T499)
    blocks = guard.writes([in_worktree(guard, rel)])

    assert walls(blocks) == ["denied-path"]
    assert "deny" in blocks[0].why


def test_infra_outside_prod_is_not_denied(guard):
    assert guard.writes([in_worktree(guard, "infra/staging/main.tf")]) == []


# ── diff budget ───────────────────────────────────────────────────────────────


def diff_of(files: int) -> str:
    return "".join(
        f"diff --git a/f{i}.py b/f{i}.py\n--- a/f{i}.py\n+++ b/f{i}.py\n+x = {i}\n"
        for i in range(files)
    )


def test_the_diff_budget_is_enforced(tmp_path: Path):
    guard = Guard(tmp_path, RUN, max_files=2)

    assert guard.diff(diff_of(2)) == []
    assert walls(guard.diff(diff_of(3))) == ["budget"]


def test_the_diff_budget_can_only_be_tightened(tmp_path: Path):
    guard = Guard(tmp_path, RUN, max_files=10_000)  # Zyroflo's default is 40

    assert walls(guard.diff(diff_of(41))) == ["budget"]


# ── push ──────────────────────────────────────────────────────────────────────


def test_a_push_to_the_run_branch_is_allowed(guard):
    assert guard.push("origin", f"sdlc/{RUN}") == []


@pytest.mark.parametrize(("remote", "branch"), [("upstream", f"sdlc/{RUN}"), ("origin", "main"),
                                                ("origin", "sdlc/another-run")])  # fmt: skip
def test_a_push_anywhere_else_is_refused(guard, remote, branch):
    assert set(walls(guard.push(remote, branch))) == {"push"}


# ── Zyroflo's lists are never weakened ───────────────────────────────────────


def test_building_a_guard_does_not_change_zyroflo_defaults(tmp_path: Path):
    from tools.sdlc import policy

    before = list(policy.DEFAULTS["deny_commands"]), list(policy.DEFAULTS["protected_paths"])
    Guard(tmp_path, RUN, deny_shell=("curl",), deny_paths=("x/**",)).command("ls")

    assert (policy.DEFAULTS["deny_commands"], policy.DEFAULTS["protected_paths"]) == before
