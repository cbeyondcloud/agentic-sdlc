"""The casbin side of `authorize` (Review Focus 7): the model is pinned, domains always
carry the tenant, the adapter is read-only, scopes follow the catalogue kind."""

import uuid
from pathlib import Path

import pytest

from agentic.orgapi.rbac import PERMISSIONS, EffectivePermissions
from agentic_cp.rbac import authz
from agentic_cp.rbac.authz import (
    Org,
    Repo,
    Team,
    ask,
    build_enforcer,
    decide,
    domain,
    permissions_for,
)
from agentic_cp.rbac.defaults import SERVICE_PERMISSIONS
from agentic_cp.rbac.store import Grant, PolicyRows

A, B = uuid.uuid4(), uuid.uuid4()
PAT = uuid.uuid4()

MODEL_CONF = """\
[request_definition]
r = sub, dom, act

[policy_definition]
p = sub, act

[role_definition]
g = _, _, _

[policy_effect]
e = some(where (p.eft == allow))

[matchers]
m = g(r.sub, p.sub, r.dom) && r.act == p.act
"""

ROLES = {
    "developer": frozenset({"run.trigger", "gate.approve"}),
    "auditor": frozenset({"audit.read.org", "audit.read.team"}),
}


def _rows(*grants: Grant, tenant=A) -> PolicyRows:
    return PolicyRows(tenant_id=tenant, roles=ROLES, grants=list(grants))


def test_model_conf_snapshot():
    packaged = Path(authz.__file__).with_name("model.conf").read_text(encoding="utf-8")
    assert packaged.replace("\r\n", "\n") == MODEL_CONF


def test_team_scoped_role_does_not_reach_other_team():
    enforcer = build_enforcer(_rows(Grant(PAT, "developer", "team:payments")))
    assert decide(enforcer, A, PAT, "run.trigger", Team("payments")).allowed
    assert not decide(enforcer, A, PAT, "run.trigger", Team("billing")).allowed
    assert not decide(enforcer, A, PAT, "run.trigger", Repo("github.com/x/y", "billing")).allowed
    assert not decide(enforcer, A, PAT, "run.trigger", Org()).allowed


def test_org_only_permission_inert_at_team_scope():
    enforcer = build_enforcer(_rows(Grant(PAT, "auditor", "team:payments")))
    assert decide(enforcer, A, PAT, "audit.read.team", Team("payments")).allowed
    assert not decide(enforcer, A, PAT, "audit.read.org", Team("payments")).allowed
    assert not decide(enforcer, A, PAT, "audit.read.org", Org()).allowed


def test_enforcer_never_matches_another_tenants_domain():
    # a row for tenant b in a's enforcer (it can never be loaded under RLS, but even so)
    enforcer = build_enforcer(_rows(Grant(PAT, "developer", "org", tenant=B)))
    assert not decide(enforcer, A, PAT, "run.trigger", Org()).allowed
    # and a domain naming b is refused before casbin is asked
    enforcer_b = build_enforcer(_rows(Grant(PAT, "developer", "org"), tenant=B))
    assert enforcer_b.enforce(f"user:{PAT}", domain(B, "org"), "run.trigger")
    assert ask(enforcer_b, A, PAT, domain(B, "org"), "run.trigger") == (False, None)


def test_authorize_names_the_role_that_granted():
    enforcer = build_enforcer(
        _rows(Grant(PAT, "developer", "team:payments"), Grant(PAT, "auditor", "org"))
    )
    granted = decide(enforcer, A, PAT, "gate.approve", Repo("github.com/acme/api", "payments"))
    assert (granted.allowed, granted.role, granted.scope) == (True, "developer", "team:payments")
    org = decide(enforcer, A, PAT, "audit.read.org", Org())
    assert (org.role, org.scope) == ("auditor", "org")


def test_unknown_permission_is_a_programming_error():
    enforcer = build_enforcer(_rows())
    with pytest.raises(ValueError, match="no.such"):
        decide(enforcer, A, PAT, "no.such", Org())


def test_rows_adapter_is_read_only():
    enforcer = build_enforcer(_rows(Grant(PAT, "developer", "org")))
    with pytest.raises(NotImplementedError):
        enforcer.add_policy("developer", "rbac.assign")
    with pytest.raises(NotImplementedError):
        enforcer.save_policy()
    with pytest.raises(NotImplementedError):
        enforcer.remove_policy("developer", "run.trigger")
    assert not decide(enforcer, A, PAT, "rbac.assign", Org()).allowed


def test_a_role_with_no_permissions_and_a_user_with_none_are_denied():
    enforcer = build_enforcer(PolicyRows(tenant_id=A, roles={"empty": frozenset()}, grants=[]))
    assert not decide(enforcer, A, PAT, "run.trigger", Org()).allowed


# -- effective permissions (DF-406, M3-22) --

WIDE_ROLES = {
    "developer": frozenset({"run.trigger", "run.cancel.own", "gate.approve"}),
    "team-lead": frozenset({"run.cancel.any", "dashboard.view.team", "rbac.assign.team"}),
    "maintainer": frozenset({"agent.approve.repo", "dashboard.view.repo"}),
    "auditor": frozenset({"audit.read.org", "audit.read.team"}),
}


def _reference(enforcer, user, kind, repo_teams) -> EffectivePermissions:
    """The answer asked one permission at a time through `decide` (the matcher per question)."""
    subject = f"user:{user}"
    scopes = {
        rule[2].split("|", 1)[1]
        for rule in enforcer.get_grouping_policy()
        if rule[0] == subject and rule[2].startswith(f"{A}|")
    }

    def allowed(permission, resource):
        return decide(enforcer, A, user, permission, resource).allowed

    org = frozenset(p for p in PERMISSIONS if allowed(p, Org()))
    teams = {
        s[5:]: frozenset(
            p for p in PERMISSIONS if PERMISSIONS[p] != "org" and allowed(p, Team(s[5:]))
        )
        for s in scopes
        if s.startswith("team:")
    }
    repos = {
        s[5:]: frozenset(
            p for p in PERMISSIONS if PERMISSIONS[p] == "repo" and allowed(p, Repo(s[5:]))
        )
        for s in scopes
        if s.startswith("repo:")
    }
    if kind == "service":
        org = org & SERVICE_PERMISSIONS
        teams = {k: v & SERVICE_PERMISSIONS for k, v in teams.items()}
        repos = {k: v & SERVICE_PERMISSIONS for k, v in repos.items()}
    shown = {k: t for k, t in repo_teams.items() if k in repos}
    return EffectivePermissions(org=org, teams=teams, repos=repos, repo_teams=shown)


def _wide_rows(users: list[uuid.UUID]) -> PolicyRows:
    grants = []
    for n, user in enumerate(users):
        grants.append(Grant(user, "developer", f"team:team-{n % 7:02d}"))
        if n % 3 == 0:
            grants.append(Grant(user, "team-lead", f"team:team-{n % 5:02d}"))
        if n % 4 == 0:
            grants.append(Grant(user, "maintainer", f"repo:git.example.test/app-{n % 6}"))
        if n % 11 == 0:
            grants.append(Grant(user, "auditor", "org"))
        if n % 13 == 0:
            grants.append(Grant(user, "developer", "org"))
    grants.append(Grant(users[0], "developer", "team:team-00", tenant=B))  # another tenant's
    repo_teams = {f"git.example.test/app-{k}": f"team-{k:02d}" for k in range(6)}
    return PolicyRows(tenant_id=A, roles=WIDE_ROLES, grants=grants, repo_teams=repo_teams)


@pytest.mark.parametrize("kind", ["human", "service"])
def test_effective_permissions_equal_one_decision_per_permission(kind):
    users = [uuid.uuid4() for _ in range(60)]
    rows = _wide_rows(users)
    enforcer = build_enforcer(rows)
    for user in [*users, uuid.uuid4()]:  # and one with no grant at all
        found = permissions_for(enforcer, A, user, kind, rows.repo_teams)
        assert found == _reference(enforcer, user, kind, rows.repo_teams), user


def test_effective_permissions_do_not_evaluate_the_matcher_per_permission(monkeypatch):
    """`/v1/me` asked casbin once per permission and scope (about 100 matcher runs over
    every policy line): 318 ms of CPU per call at 500 people per tenant in the M3-22 run."""
    users = [uuid.uuid4() for _ in range(500)]
    rows = _wide_rows(users)
    enforcer = build_enforcer(rows)
    calls = []
    monkeypatch.setattr(enforcer, "enforce_ex", lambda *a: calls.append(a) or (False, []))
    permissions_for(enforcer, A, users[0], "human", rows.repo_teams)
    assert calls == []
