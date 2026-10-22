"""The org permission catalogue and the read-only views over server-computed permissions
(spec §12.3, §23; owner decision row 74)."""

import uuid
from datetime import UTC, datetime

from agentic.orgapi import AssignmentRef, Me, TenantRef, UserRef
from agentic.orgapi.rbac import (
    PERMISSIONS,
    EffectivePermissions,
    OrgAuthorizer,
    allows,
    holds_role,
    role_held,
)

SPEC_12_3 = {
    "run.trigger",
    "run.cancel.own",
    "run.cancel.any",
    "gate.approve",
    "agent.propose.repo",
    "agent.approve.repo",
    "agent.publish.team",
    "pipeline.publish.team",
    "skills.personal.edit",
    "policy.edit.org",
    "policy.edit.team",
    "budget.raise.team",
    "killswitch.team",
    "killswitch.global",
    "rbac.assign",
    "dashboard.view.own",
    "dashboard.view.team",
    "dashboard.view.org",
    "audit.read.team",
    "audit.read.org",
    "trace.view.team",
    "trace.view.org",
    "skills.approve",
}
ADDITIONS = {
    "agent.edit.repo",
    "rbac.assign.team",
    "dashboard.view.repo",
    "audit.read.own",
    "audit.read.repo",
}


def _me(
    assignments: list[AssignmentRef],
    org: set[str] = frozenset(),
    teams: dict | None = None,
    kind: str = "human",
) -> Me:
    return Me(
        user=UserRef(id=uuid.uuid4(), email="pat@example.test"),
        kind=kind,
        tenant=TenantRef(slug="acme", name="Acme"),
        token_expires_at=datetime(2027, 1, 1, tzinfo=UTC),
        assignments=assignments,
        permissions=EffectivePermissions(
            org=frozenset(org), teams={k: frozenset(v) for k, v in (teams or {}).items()}
        ),
    )


def test_permission_catalog_matches_spec():
    assert set(PERMISSIONS) == SPEC_12_3 | ADDITIONS
    assert set(PERMISSIONS.values()) <= {"org", "team", "repo"}
    for name in ("policy.edit.org", "killswitch.global", "rbac.assign", "skills.approve"):
        assert PERMISSIONS[name] == "org"


def test_org_admin_holds_any_gate_role():
    admin = [AssignmentRef(role="org-admin", scope="org")]
    me = _me(admin, org={"gate.approve", "rbac.assign"})
    assert role_held(me, "approver", team="payments") == "org-admin"
    assert OrgAuthorizer(me, team="payments").holds("pat@example.test", "architect")
    stripped = _me(admin, org={"rbac.assign"})  # the org-admin role edited to drop it
    assert role_held(stripped, "approver", team="payments") is None
    assert not OrgAuthorizer(stripped, team="payments").holds("pat@example.test", "approver")


def test_role_held_names_the_role_actually_held():
    approver = _me(
        [AssignmentRef(role="approver", scope="team", team="payments")],
        teams={"payments": {"gate.approve"}},
    )
    assert role_held(approver, "approver", team="payments") == "approver"
    assert role_held(approver, "approver", team="billing") is None
    admin = _me(
        [
            AssignmentRef(role="org-admin", scope="org"),
            AssignmentRef(role="approver", scope="team", team="payments"),
        ],
        org={"gate.approve"},
    )
    assert role_held(admin, "approver", team="payments") == "approver"
    assert role_held(admin, "approver", team="billing") == "org-admin"


def test_holds_role_at_org_scope_counts_in_every_team():
    me = _me([AssignmentRef(role="approver", scope="org")])
    assert holds_role(me.assignments, "approver", team="payments")
    assert holds_role(me.assignments, "approver")
    assert not holds_role(me.assignments, "team-lead", team="payments")


def test_service_account_cannot_approve_gates():
    bot = _me(
        [AssignmentRef(role="org-admin", scope="org")],
        org={"gate.approve"},
        kind="service",
    )
    assert role_held(bot, "approver", team="payments") is None
    assert not OrgAuthorizer(bot, team="payments").holds("pat@example.test", "approver")


def test_org_authorizer_refuses_other_users():
    me = _me([AssignmentRef(role="approver", scope="team", team="payments")])
    authorizer = OrgAuthorizer(me, team="payments")
    assert authorizer.holds("pat@example.test", "approver")
    assert authorizer.role_held("pat@example.test", "approver") == "approver"
    assert not authorizer.holds("sam@example.test", "approver")
    assert authorizer.role_held("sam@example.test", "approver") is None


def test_allows_reads_the_scope_the_catalogue_names():
    perms = EffectivePermissions(
        org=frozenset({"dashboard.view.org"}),
        teams={"payments": frozenset({"run.trigger", "audit.read.org"})},
        repos={"github.com/acme/api": frozenset({"agent.approve.repo"})},
        repo_teams={"github.com/acme/web": "payments"},
    )
    assert allows(perms, "dashboard.view.org")
    assert allows(perms, "run.trigger", team="payments")
    assert not allows(perms, "run.trigger", team="billing")
    assert allows(perms, "run.trigger", repo="github.com/acme/web")  # through its team
    assert allows(perms, "agent.approve.repo", repo="github.com/acme/api")
    assert not allows(perms, "audit.read.org")  # an org permission held at team scope is inert
