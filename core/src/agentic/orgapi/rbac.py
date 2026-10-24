"""The org permission catalogue (spec §12.3 and §23), shared by the control plane and the CLI.

Pure data and set logic: no rule engine. The control plane decides through pycasbin behind
`agentic_cp.rbac.authz.authorize`; the views here only read the sets the server computed,
for display and hints."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from agentic.orgapi.models import AssignmentRef, Me

ScopeKind = Literal["org", "team", "repo"]
ORG_ADMIN = "org-admin"

#: Every permission and where a grant of it counts (the catalogue kind):
#: `org` only from an org-scope assignment; `team` from org scope or that team; `repo` from
#: org scope, the repo's team, or a derived maintainership of that repo.
PERMISSIONS: dict[str, ScopeKind] = {
    # spec §12.3
    "run.trigger": "repo",
    "run.cancel.own": "repo",
    "run.cancel.any": "team",
    "gate.approve": "repo",
    "agent.propose.repo": "repo",
    "agent.approve.repo": "repo",
    "agent.publish.team": "team",
    "pipeline.publish.team": "team",
    "skills.personal.edit": "repo",
    "policy.edit.org": "org",
    "policy.edit.team": "team",
    "budget.raise.team": "team",
    "killswitch.team": "team",
    "killswitch.global": "org",
    "rbac.assign": "org",
    "dashboard.view.own": "team",
    "dashboard.view.team": "team",
    "dashboard.view.org": "org",
    "audit.read.team": "team",
    "audit.read.org": "org",
    "trace.view.team": "team",
    "trace.view.org": "org",
    "skills.approve": "org",
    # additions (SP15 plan): CLI check 7, team-scoped assignment, repo dashboards and audit
    "agent.edit.repo": "repo",
    "rbac.assign.team": "team",
    "dashboard.view.repo": "repo",
    "audit.read.own": "team",
    "audit.read.repo": "repo",
    # M2-13: exporting the chain with its proof (auditors; a team lead reads but never exports)
    "audit.export": "team",
}


def unknown_permissions(names: list[str] | tuple[str, ...] | set[str]) -> list[str]:
    """The names in `names` that are not in the catalogue, sorted."""
    return sorted(set(names) - set(PERMISSIONS))


class ScopeRef(BaseModel):
    kind: ScopeKind
    value: str | None = None


class EffectivePermissions(BaseModel):
    """What the caller may do, as the server computed it: permissions granted at org scope,
    per team, and per repo (derived maintainerships), plus each known repo's team. For
    display and CLI hints only; authoritative answers come from `POST /v1/rbac/check`."""

    org: frozenset[str] = frozenset()
    teams: dict[str, frozenset[str]] = Field(default_factory=dict)
    repos: dict[str, frozenset[str]] = Field(default_factory=dict)
    repo_teams: dict[str, str] = Field(default_factory=dict)


def allows(
    perms: EffectivePermissions,
    permission: str,
    *,
    team: str | None = None,
    repo: str | None = None,
) -> bool:
    """Whether `perms` holds `permission` where the catalogue says a grant counts."""
    kind = PERMISSIONS[permission]
    if permission in perms.org:
        return True
    if kind == "org":
        return False
    team = perms.repo_teams.get(repo, team) if repo else team
    if team is not None and permission in perms.teams.get(team, frozenset()):
        return True
    return kind == "repo" and repo is not None and permission in perms.repos.get(repo, frozenset())


def holds_role(assignments: list[AssignmentRef], role: str, *, team: str | None = None) -> bool:
    """True when `role` is held at org scope, or in `team`."""
    return any(
        a.role == role and (a.scope == "org" or (team is not None and a.team == team))
        for a in assignments
    )


def role_held(me: Me, role: str, *, team: str | None = None) -> str | None:
    """The role under which `me` satisfies a gate that names `role`: the role itself (in the
    team or at org scope), else `org-admin` for an org admin holding `gate.approve` (owner
    decision row 74), else None. A service account never approves (spec §12.3)."""
    if me.kind != "human":
        return None
    if holds_role(me.assignments, role, team=team):
        return role
    if holds_role(me.assignments, ORG_ADMIN) and "gate.approve" in me.permissions.org:
        return ORG_ADMIN
    return None


class OrgAuthorizer:
    """The engine's `Authorizer` in org mode: roles come from the control plane (`/v1/me`),
    never from `~/.agentic/config.yaml`. Only the logged-in user holds anything here; the
    four-eyes rule stays in the engine. `role_held` is what a gate approval records."""

    def __init__(self, me: Me, team: str | None) -> None:
        self.me = me
        self.team = team

    def holds(self, user: str, role: str) -> bool:
        return self.role_held(user, role) is not None

    def role_held(self, user: str, role: str) -> str | None:
        if user.strip().lower() != self.me.user.email.lower():
            return None
        return role_held(self.me, role, team=self.team)
