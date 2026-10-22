"""The spec §23 default permission matrix, transcribed as expectations per role.

Each role is held where §23 says (org admin, auditor and exec viewer at org scope; team
lead, developer, approver and service account in team `payments`; repo maintainer derived
for repo `github.com/acme/pay-api`, a repo of `payments`). For every permission the string
lists where it is allowed:

    O  the org         T  own team (payments)      t  another team (billing)
    R  own repo        r  another team's repo

A permission missing from a role's row is allowed nowhere. Written out by hand from §23 and
§12.3 - not computed from `DEFAULT_ROLES` - so a change to the seed policy shows up here."""

from __future__ import annotations

from agentic.orgapi.rbac import PERMISSIONS

OWN_TEAM, OTHER_TEAM = "payments", "billing"
OWN_REPO, OTHER_REPO = "github.com/acme/pay-api", "github.com/acme/bill-api"
EVERYWHERE = "OTtRr"

#: where each role is held: (scope kind, scope value)
HELD_AT = {
    "org-admin": ("org", ""),
    "team-lead": ("team", OWN_TEAM),
    "repo-maintainer": ("repo", OWN_REPO),
    "developer": ("team", OWN_TEAM),
    "approver": ("team", OWN_TEAM),
    "auditor": ("org", ""),
    "exec-viewer": ("org", ""),
    "service-account": ("team", OWN_TEAM),
}

EXPECTED: dict[str, dict[str, str]] = {
    # Org admin: everything, everywhere.
    "org-admin": {permission: EVERYWHERE for permission in PERMISSIONS},
    # Team lead: their team and its repos; never org-only rights.
    "team-lead": {
        "run.trigger": "TR",
        "run.cancel.own": "TR",
        "run.cancel.any": "TR",
        "gate.approve": "TR",
        "skills.personal.edit": "TR",
        "agent.propose.repo": "TR",
        "agent.approve.repo": "TR",
        "agent.edit.repo": "TR",
        "agent.publish.team": "TR",
        "pipeline.publish.team": "TR",
        "policy.edit.team": "TR",
        "budget.raise.team": "TR",
        "killswitch.team": "TR",
        "rbac.assign.team": "TR",
        "dashboard.view.own": "TR",
        "dashboard.view.team": "TR",
        "audit.read.own": "TR",
        "audit.read.team": "TR",
        "trace.view.team": "TR",
    },
    # Repo maintainer: shared repo items of that one repo (D30); never pipelines (D33).
    "repo-maintainer": {
        "run.trigger": "R",
        "run.cancel.own": "R",
        "gate.approve": "R",
        "skills.personal.edit": "R",
        "agent.propose.repo": "R",
        "agent.approve.repo": "R",
        "agent.edit.repo": "R",
        "dashboard.view.repo": "R",
        "audit.read.repo": "R",
    },
    # Developer: own runs, proposals, personal skills; cannot change pipelines.
    "developer": {
        "run.trigger": "TR",
        "run.cancel.own": "TR",
        "gate.approve": "TR",
        "skills.personal.edit": "TR",
        "agent.propose.repo": "TR",
        "agent.edit.repo": "TR",
        "dashboard.view.own": "TR",
        "audit.read.own": "TR",
    },
    # Approver: gates where assigned, team dashboard, personal skills, proposals.
    "approver": {
        "gate.approve": "TR",
        "skills.personal.edit": "TR",
        "agent.propose.repo": "TR",
        "dashboard.view.team": "TR",
    },
    # Auditor (org scope): read-only audit, dashboards and traces.
    "auditor": {
        "audit.read.own": EVERYWHERE,
        "audit.read.team": EVERYWHERE,
        "audit.read.repo": EVERYWHERE,
        "audit.read.org": EVERYWHERE,
        "dashboard.view.own": EVERYWHERE,
        "dashboard.view.team": EVERYWHERE,
        "dashboard.view.repo": EVERYWHERE,
        "dashboard.view.org": EVERYWHERE,
        "trace.view.team": EVERYWHERE,
        "trace.view.org": EVERYWHERE,
    },
    # Executive viewer: org dashboards only.
    "exec-viewer": {"dashboard.view.org": EVERYWHERE},
    # Service account: trigger runs only (cannot approve gates).
    "service-account": {"run.trigger": "TR"},
}


def cells() -> list[tuple[str, str, str, bool]]:
    """(role, permission, column, expected) for every role x permission x column."""
    return [
        (role, permission, column, column in EXPECTED[role].get(permission, ""))
        for role in EXPECTED
        for permission in PERMISSIONS
        for column in EVERYWHERE
    ]
