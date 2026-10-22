"""The org permission catalogue (spec §12.3 and §23), shared by the control plane and the CLI.

Pure data and set logic: no rule engine. The control plane decides through pycasbin behind
`agentic_cp.rbac.authz.authorize`; the views here only read the sets the server computed,
for display and hints."""

from __future__ import annotations

from typing import Literal

ScopeKind = Literal["org", "team", "repo"]

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
    "skills.personal.edit": "team",
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
}


def unknown_permissions(names: list[str] | tuple[str, ...] | set[str]) -> list[str]:
    """The names in `names` that are not in the catalogue, sorted."""
    return sorted(set(names) - set(PERMISSIONS))
