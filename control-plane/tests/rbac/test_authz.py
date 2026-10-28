"""The casbin side of `authorize` (Review Focus 7): the model is pinned, domains always
carry the tenant, the adapter is read-only, scopes follow the catalogue kind."""

import uuid
from pathlib import Path

import pytest

from agentic_cp.rbac import authz
from agentic_cp.rbac.authz import Org, Repo, Team, ask, build_enforcer, decide, domain
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
