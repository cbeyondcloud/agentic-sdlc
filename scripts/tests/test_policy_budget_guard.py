"""The policy repo's team budget guard (M3-05, DF-857): a team lead may only lower their own
team's `monthly_budget_usd`; a raise needs an org admin, as the author or as an approving
reviewer of the pull request's head commit. Org admins are the owners of
`policy/org.yaml` in the base branch's CODEOWNERS; when they cannot be told (a team handle
whose members are unknown), a raise fails closed."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = ROOT / "templates" / "policy-repo"
GUARD = TEMPLATE / ".github" / "budget_guard.py"
WORKFLOW = TEMPLATE / ".github" / "workflows" / "policy-check.yml"


def _load():
    spec = importlib.util.spec_from_file_location("budget_guard", GUARD)
    module = importlib.util.module_from_spec(spec)
    sys.modules["budget_guard"] = module
    written, sys.dont_write_bytecode = sys.dont_write_bytecode, True
    try:  # no __pycache__ in the template: the bootstrap copies it into policy repos
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = written
    return module


guard = _load()

TEAM = "allowed_tiers: [fast]\nmonthly_budget_usd: {}\n"
CODEOWNERS = (
    "*                         @acme/agentic-org-admins @root-admin\n"
    "/policy/                  @acme/agentic-org-admins @root-admin\n"
    "/teams/payments/          @acme/payments-leads\n"
    "/policy/teams/payments.yaml @acme/payments-leads @acme/agentic-org-admins\n"
)


def _changes(before: dict[str, str | None], after: dict[str, str | None]):
    return guard.budget_changes(before, after)


def test_lowering_or_keeping_a_budget_is_not_a_raise():
    changes = _changes({"payments": TEAM.format(200)}, {"payments": TEAM.format(150)})
    assert [(c.team, c.raised) for c in changes] == [("payments", False)]
    assert _changes({"payments": TEAM.format(200)}, {"payments": TEAM.format("200.0")}) == []
    assert guard.decide(changes, author="lead", approvers=set(), admins={"root-admin"}) == []


@pytest.mark.parametrize(
    ("before", "after", "why"),
    [
        (TEAM.format(200), TEAM.format(201), "200 -> 201"),
        (None, TEAM.format(50), "a new team"),
        (TEAM.format(200), "allowed_tiers: [fast]\n", "no cap"),
        (TEAM.format(200), TEAM.format("lots"), "not a number"),
        # review: the gateway reads a cap of 0 or less as no cap at all
        (TEAM.format(200), TEAM.format(0), "no cap"),
        (TEAM.format(200), TEAM.format(-1), "no cap"),
        (TEAM.format(200), TEAM.format("'0'"), "no cap"),
        # review: a removed team file leaves the team without its budget
        (TEAM.format(200), None, "team file removed"),
    ],
)
def test_a_raise_needs_an_org_admin(before, after, why):
    changes = _changes({"payments": before}, {"payments": after})
    assert [c.raised for c in changes] == [True]
    problems = guard.decide(changes, author="lead", approvers={"other-lead"}, admins={"root"})
    assert len(problems) == 1 and "policy/teams/payments.yaml" in problems[0], problems
    assert why in problems[0] and "org admin" in problems[0]


def test_an_org_admin_author_or_approver_lets_a_raise_through():
    changes = _changes({"payments": TEAM.format(200)}, {"payments": TEAM.format(400)})
    admins = {"root-admin"}
    assert guard.decide(changes, author="Root-Admin", approvers=set(), admins=admins) == []
    assert guard.decide(changes, author="lead", approvers={"root-admin"}, admins=admins) == []


def test_the_guard_reads_budgets_as_the_policy_loader_does():
    """The YAML reading of agentic.yamlio and the coercion of TeamPolicy: the guard and
    the control plane never disagree on a team's budget."""
    from agentic.policy.model import TeamPolicy
    from agentic.yamlio import load_yaml

    for value in ("1_000", "0x10", "1e3", "'200'", "200.50", "'0'", "0.0"):
        text = f"{guard.FIELD}: {value}\n"
        loaded = TeamPolicy(**load_yaml(text)[0]).monthly_budget_usd
        if loaded > 0:
            expected = format(guard.Decimal(str(loaded)).normalize(), "f")
        else:
            expected = "no cap"  # the gateway reads 0 or less as no cap
        assert guard._budget(text)[1] == expected, value


def test_org_admins_are_the_base_codeowners_of_org_policy():
    logins, teams = guard.org_admins(CODEOWNERS)
    assert logins == {"root-admin"} and teams == {"acme/agentic-org-admins"}


def test_unexpanded_admin_teams_fail_closed_and_say_how_to_fix():
    changes = _changes({"payments": TEAM.format(200)}, {"payments": TEAM.format(400)})
    problems = guard.decide(
        changes,
        author="lead",
        approvers={"someone"},
        admins=set(),
        unresolved={"acme/agentic-org-admins"},
    )
    assert problems and "ORG_READ_TOKEN" in problems[-1]
    assert "acme/agentic-org-admins" in problems[-1]


def _git(where: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-c", "core.autocrlf=false", *args],
        cwd=where,
        capture_output=True,
        text=True,
        check=True,
    )
    return done.stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    where = tmp_path / "policy"
    (where / "policy" / "teams").mkdir(parents=True)
    (where / "CODEOWNERS").write_text(CODEOWNERS, encoding="utf-8")
    (where / "policy" / "teams" / "payments.yaml").write_text(TEAM.format(200), encoding="utf-8")
    _git(where, "init", "-q", "-b", "main")
    _git(where, "-c", "user.name=t", "-c", "user.email=t@e.x", "add", "-A")
    _git(where, "-c", "user.name=t", "-c", "user.email=t@e.x", "commit", "-q", "-m", "base")
    return where


class FakeGh:
    """`gh api` answers: the pull request's reviews and the admin team's members."""

    def __init__(self, reviews: list[dict], members: list[str] | None) -> None:
        self.reviews, self.members, self.calls = reviews, members, []

    def __call__(self, path: str, *, org: bool = False) -> list:
        self.calls.append(path)
        assert org == ("/teams/" in path)  # review: the members token only for members
        if path.endswith("/reviews"):
            return self.reviews
        if "/teams/" in path:
            if self.members is None:
                raise guard.GuardError("gh: HTTP 403 (no read:org)")
            return [{"login": m} for m in self.members]
        raise AssertionError(path)


def _main(repo: Path, gh: FakeGh, *extra: str) -> int:
    argv = ["--base", "main", "--author", "lead", "--repo", "acme/policy", "--pull", "7"]
    argv += ["--head-sha", "abc123", *extra]
    return guard.main(argv, root=repo, gh=gh)


def test_main_reads_the_base_from_git_and_reviews_from_the_host(repo, capsys):
    (repo / "policy" / "teams" / "payments.yaml").write_text(TEAM.format(500), encoding="utf-8")
    stale = {"user": {"login": "carol"}, "state": "APPROVED", "commit_id": "old"}
    fresh = {"user": {"login": "carol"}, "state": "APPROVED", "commit_id": "abc123"}
    # carol is in the admin team; her approval of an older commit does not count
    assert _main(repo, FakeGh([stale], ["carol"]), "--expand-teams") == 1
    assert "200 -> 500" in capsys.readouterr().out
    assert _main(repo, FakeGh([stale, fresh], ["carol"]), "--expand-teams") == 0
    # an approval later dismissed or turned into changes requested does not count either
    later = {"user": {"login": "carol"}, "state": "CHANGES_REQUESTED", "commit_id": "abc123"}
    assert _main(repo, FakeGh([fresh, later], ["carol"]), "--expand-teams") == 1
    capsys.readouterr()
    # without membership data the team handle stays unresolved: fail closed
    assert _main(repo, FakeGh([fresh], None), "--expand-teams") == 1
    assert "ORG_READ_TOKEN" in capsys.readouterr().out
    # an individual admin in CODEOWNERS needs no membership data
    root = {"user": {"login": "root-admin"}, "state": "APPROVED", "commit_id": "abc123"}
    assert _main(repo, FakeGh([root], None)) == 0


def test_main_passes_a_change_without_raises(repo, capsys):
    (repo / "policy" / "teams" / "payments.yaml").write_text(TEAM.format(100), encoding="utf-8")
    gh = FakeGh([], None)
    assert _main(repo, gh) == 0
    assert gh.calls == []  # nothing to approve: the host is not asked
    assert "no team budget is raised" in capsys.readouterr().out


def test_the_workflow_runs_the_base_branchs_guard_on_reviews_too():
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "pull_request_review:" in text
    assert "git show" in text and ".github/budget_guard.py" in text  # the base's copy
    assert "pull-requests: read" in text
    # review: outside the checkout, no project config (no index of the pull request's)
    assert 'cd "${RUNNER_TEMP}"' in text and "uv run --no-config --script" in text
    # review: the members token only reads members; reviews use the workflow's token
    assert "GH_TOKEN: ${{ github.token }}" in text
    assert "ORG_READ_TOKEN: ${{ secrets.ORG_READ_TOKEN }}" in text
    assert "edited" in text  # a retargeted pull request is judged against its new base


def test_the_install_line_names_published_packages_at_one_version():
    """DF-598: `agentic[skills]` is not published; the gate installs the CLI with the core's
    skills extra (skills-ref), both at the org's pinned version, from its own index."""
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "agentic[skills]" not in text
    assert 'uv tool install "agentic-cli==${AGENTIC_VERSION}"' in text
    assert '--with "agentic-core[skills]==${AGENTIC_VERSION}"' in text
    assert "AGENTIC_INDEX_URL" in text


def test_the_guard_declares_its_pinned_dependencies():
    """`uv run --script` installs them (PEP 723) on the CI runner, never on a laptop."""
    text = GUARD.read_text(encoding="utf-8")
    assert text.startswith("# /// script\n")
    head = text.split("# ///\n", 1)[0]
    assert '"codeowners==0.9.0"' in head and '"ruamel.yaml==0.19.1"' in head
