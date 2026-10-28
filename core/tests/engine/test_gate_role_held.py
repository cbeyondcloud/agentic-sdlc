"""Gate approvals record the role they needed and the role the approver held (DF-409, DF-402;
owner decision 74): an org admin approving in place of the named role is visible in the run's
own approval ledger, not only in the control plane's audit."""

import asyncio

from _engine_support import answer

from agentic.engine.gates import Approve, StandaloneAuthorizer

GATED = """\
name: test
version: 1.0.0
steps:
  - id: design
    agent: designer
  - gate: design-approval
    approver_role: team-lead
"""


class OrgLike:
    """An authorizer that, like `OrgAuthorizer`, says under which role a person approves."""

    def __init__(self, held: str | None) -> None:
        self.held = held

    def holds(self, user: str, role: str) -> bool:
        return self.held is not None

    def role_held(self, user: str, role: str) -> str | None:
        return self.held


def _approve(make_harness, authorizer):
    h = make_harness(GATED, {"designer": [answer("design")]})
    state = asyncio.run(h.engine.start("test", {}, "dev@acme.example", "r-1"))
    assert state.pending.id == "design-approval"
    decision = Approve("design-approval", approver="admin@acme.example")
    asyncio.run(
        h.engine.decide("r-1", decision, authorizer=authorizer, self_approval=lambda a, s: None)
    )
    return h.gates["r-1"].entries[-1]


def test_an_org_admin_approval_records_the_role_it_stood_in_for(make_harness):
    entry = _approve(make_harness, OrgLike("org-admin"))
    assert entry["extra"]["required_role"] == "team-lead"
    assert entry["extra"]["role_held"] == "org-admin"


def test_a_standalone_approval_records_the_role_as_unverified(make_harness):
    entry = _approve(make_harness, StandaloneAuthorizer())
    assert entry["extra"]["required_role"] == "team-lead"
    assert entry["extra"]["role_held"] is None  # no RBAC before org sign-in
