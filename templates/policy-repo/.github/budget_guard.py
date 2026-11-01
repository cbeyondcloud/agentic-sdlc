# /// script
# requires-python = ">=3.12"
# dependencies = ["codeowners==0.9.0", "ruamel.yaml==0.19.1"]
# ///
"""Team budget guard: a team lead may only lower their own team's budget (DF-857).

Org admins change any team's `monthly_budget_usd`; a team lead may only lower their own.
CODEOWNERS lets either approve `policy/teams/<team>.yaml`, so this check adds the rule
CODEOWNERS cannot express: a pull request that raises a budget (a higher number, a new team
file, or a removed budget, which means no cap) passes only when an org admin is its author
or approved its head commit. Lowering, or not touching a budget, always passes.

Org admins are the owners of `policy/org.yaml` in the BASE branch's CODEOWNERS (the pull
request cannot add itself). An `@login` owner counts as is. An `@org/team` owner counts
through its members, which the git host only shows to a token with read access to the
organisation's members (`ORG_READ_TOKEN`); without it the team stays unresolved and a raise
fails closed. The workflow runs this file from the base branch too (`git show`), so a pull
request cannot weaken the check that judges it.

    uv run --script budget_guard.py --base origin/main --author LOGIN \\
        --repo OWNER/NAME --pull N --head-sha SHA [--expand-teams]

Exit codes: 0 passes, 1 a raise without an org admin, 2 the check could not run."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path

from codeowners import CodeOwners
from ruamel.yaml import YAML, YAMLError

TEAMS_DIR = "policy/teams"
FIELD = "monthly_budget_usd"
#: The file whose CODEOWNERS owners are the org admins (the org policy is theirs alone).
ORG_POLICY = "policy/org.yaml"
#: Where git hosts look for CODEOWNERS, in GitHub's order.
CODEOWNERS_FILES = (".github/CODEOWNERS", "CODEOWNERS", "docs/CODEOWNERS")
#: A budget that is missing, unreadable or not a number: treated as no cap (the highest).
NO_CAP = None


class GuardError(Exception):
    """The guard could not run (git or the host's API failed); exit 2."""


@dataclass(frozen=True)
class BudgetChange:
    team: str
    before: str  # as shown: "200", "a new team", "no cap", "not a number"
    after: str
    raised: bool

    @property
    def path(self) -> str:
        return f"{TEAMS_DIR}/{self.team}.yaml"


def _budget(text: str | None) -> tuple[Decimal | None, str]:
    """A team file's budget and how to show it; None is no cap (or not readable)."""
    if text is None:
        return NO_CAP, "a new team"
    try:
        data = YAML(typ="safe").load(text) or {}
    except YAMLError:
        return NO_CAP, "not readable"
    value = data.get(FIELD) if isinstance(data, dict) else None
    if value is None:
        return NO_CAP, "no cap"
    if isinstance(value, bool):
        return NO_CAP, "not a number"
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        return NO_CAP, "not a number"
    if not number.is_finite():
        return NO_CAP, "not a number"
    return number, format(number.normalize(), "f")


def budget_changes(base: dict[str, str | None], head: dict[str, str | None]) -> list[BudgetChange]:
    """Each team whose budget differs between the base and the head (team -> file text;
    a missing team is None). A team the head removes is not a raise and not listed."""
    changes = []
    for team in sorted(set(base) | set(head)):
        after_text = head.get(team)
        if after_text is None:
            continue
        before_text = base.get(team)
        before, before_shown = _budget(before_text)
        after, after_shown = _budget(after_text)
        if before_text is not None and before == after and before is not None:
            continue
        if before_text is not None and before is None and after is None:
            continue  # uncapped before and after: nothing raised by this change
        raised = after is None or before is None or after > before
        if before_text is None:
            before_shown = "a new team"
        changes.append(BudgetChange(team, before_shown, after_shown, raised))
    return changes


def org_admins(codeowners_text: str) -> tuple[set[str], set[str]]:
    """The owners of the org policy: (`@login` owners, `org/team` owners), lower case."""
    logins, teams = set(), set()
    for kind, owner in CodeOwners(codeowners_text).of(ORG_POLICY):
        if kind == "USERNAME":
            logins.add(owner.removeprefix("@").lower())
        elif kind == "TEAM":
            teams.add(owner.removeprefix("@").lower())
    return logins, teams


def decide(
    changes: Sequence[BudgetChange],
    *,
    author: str,
    approvers: set[str],
    admins: set[str],
    unresolved: set[str] | frozenset[str] = frozenset(),
) -> list[str]:
    """The problems: one per raise, unless an org admin wrote or approved the change."""
    raises = [change for change in changes if change.raised]
    if not raises:
        return []
    admins = {name.lower() for name in admins}
    if author.lower() in admins or {name.lower() for name in approvers} & admins:
        return []
    problems = [
        f"{change.path}: {FIELD} {change.before} -> {change.after} raises the team's budget; "
        "it needs an org admin (an owner of policy/org.yaml in the base branch's CODEOWNERS) "
        "as the author or an approving reviewer of this commit"
        for change in raises
    ]
    if unresolved:
        names = ", ".join(sorted(unresolved))
        problems.append(
            f"the org admin team(s) {names} could not be expanded to their members: give the "
            "workflow an ORG_READ_TOKEN secret that can read the organisation's members, or "
            "list org admins as individual @logins on policy/ in CODEOWNERS"
        )
    return problems


# ── git and the host ──────────────────────────────────────────────────────────


def _git(root: Path, *args: str) -> str | None:
    """`git <args>` in `root`; None when the object does not exist."""
    done = subprocess.run(
        ["git", "-c", "core.autocrlf=false", *args],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if done.returncode != 0:
        return None
    return done.stdout


def _base_teams(root: Path, base: str) -> dict[str, str | None]:
    listing = _git(root, "ls-tree", "--name-only", f"{base}:{TEAMS_DIR}")
    if listing is None:
        if _git(root, "rev-parse", "--verify", "--quiet", f"{base}^{{commit}}") is None:
            raise GuardError(f"the base {base} is not a commit here (fetch it first)")
        return {}
    return {
        name.removesuffix(".yaml"): _git(root, "show", f"{base}:{TEAMS_DIR}/{name}")
        for name in listing.split()
        if name.endswith(".yaml")
    }


def _head_teams(root: Path) -> dict[str, str | None]:
    folder = root / TEAMS_DIR
    if not folder.is_dir():
        return {}
    return {p.stem: p.read_text(encoding="utf-8") for p in sorted(folder.glob("*.yaml"))}


def _base_codeowners(root: Path, base: str) -> str:
    for name in CODEOWNERS_FILES:
        text = _git(root, "show", f"{base}:{name}")
        if text is not None:
            return text
    return ""


def gh_api(path: str) -> list:
    """`gh api --paginate <path>` as one JSON list (the GitHub CLI on the runner)."""
    done = subprocess.run(
        ["gh", "api", "--paginate", "--slurp", path],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if done.returncode != 0:
        reason = (done.stderr.strip().splitlines() or ["failed"])[-1]
        raise GuardError(f"gh api {path}: {reason[:200]}")
    pages = json.loads(done.stdout or "[]")
    return [item for page in pages for item in page]


def approvers_of(reviews: list, head_sha: str) -> set[str]:
    """Who approved `head_sha` and has not since dismissed it or requested changes: each
    reviewer's latest review counts, and only on the head commit."""
    latest: dict[str, dict] = {}
    for review in reviews:
        login = ((review.get("user") or {}).get("login") or "").lower()
        if login and review.get("state") in ("APPROVED", "CHANGES_REQUESTED", "DISMISSED"):
            latest[login] = review
    return {
        login
        for login, review in latest.items()
        if review["state"] == "APPROVED" and review.get("commit_id") == head_sha
    }


def main(
    argv: Sequence[str] | None = None,
    *,
    root: Path | None = None,
    gh: Callable[[str], list] = gh_api,
) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base", required=True, help="the base branch's ref (origin/main)")
    parser.add_argument("--author", required=True, help="the pull request author's login")
    parser.add_argument("--repo", required=True, help="OWNER/NAME on the git host")
    parser.add_argument("--pull", required=True, help="the pull request's number")
    parser.add_argument("--head-sha", required=True, help="the commit approvals must name")
    parser.add_argument(
        "--expand-teams", action="store_true", help="ask the host for admin teams' members"
    )
    args = parser.parse_args(argv)
    root = root or Path.cwd()
    try:
        changes = budget_changes(_base_teams(root, args.base), _head_teams(root))
        if not any(change.raised for change in changes):
            print("budget guard: no team budget is raised")
            return 0
        logins, teams = org_admins(_base_codeowners(root, args.base))
        admins, unresolved = set(logins), set()
        for team in sorted(teams):
            org, _, slug = team.partition("/")
            if not args.expand_teams:
                unresolved.add(team)
                continue
            try:
                admins |= {m["login"].lower() for m in gh(f"orgs/{org}/teams/{slug}/members")}
            except GuardError as error:
                print(f"budget guard: {error}")
                unresolved.add(team)
        reviews = gh(f"repos/{args.repo}/pulls/{args.pull}/reviews")
    except GuardError as error:
        print(f"budget guard: could not run: {error}")
        return 2
    problems = decide(
        changes,
        author=args.author,
        approvers=approvers_of(reviews, args.head_sha),
        admins=admins,
        unresolved=unresolved,
    )
    for problem in problems:
        print(f"budget guard: {problem}")
    if problems:
        return 1
    print("budget guard: the raise is an org admin's (author or approver of this commit)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
