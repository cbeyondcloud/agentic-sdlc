"""One test per cell of the spec §23 permission matrix (Review Focus 4), decided by the
casbin enforcer built from the seed policy - no database needed."""

import uuid

import pytest
from matrix import HELD_AT, OTHER_REPO, OTHER_TEAM, OWN_REPO, OWN_TEAM, cells

from agentic_cp.rbac.authz import Org, Repo, Team, build_enforcer, decide
from agentic_cp.rbac.defaults import DEFAULT_ROLES
from agentic_cp.rbac.store import Grant, PolicyRows

TENANT = uuid.uuid4()
USERS = {role: uuid.uuid4() for role in HELD_AT}
RESOURCES = {
    "O": Org(),
    "T": Team(OWN_TEAM),
    "t": Team(OTHER_TEAM),
    "R": Repo(OWN_REPO, team=OWN_TEAM),
    "r": Repo(OTHER_REPO, team=OTHER_TEAM),
}


def seed_rows() -> PolicyRows:
    grants = [
        Grant(user_id=USERS[role], role=role, scope=kind if kind == "org" else f"{kind}:{value}")
        for role, (kind, value) in HELD_AT.items()
    ]
    return PolicyRows(
        tenant_id=TENANT,
        roles={spec.name: frozenset(spec.permissions) for spec in DEFAULT_ROLES},
        grants=grants,
    )


ENFORCER = build_enforcer(seed_rows())
CELLS = cells()


@pytest.mark.parametrize(
    ("role", "permission", "column", "expected"),
    CELLS,
    ids=[f"{r}-{p}-{c}" for r, p, c, _ in CELLS],
)
def test_permission_matrix_cell(role, permission, column, expected):
    decision = decide(ENFORCER, TENANT, USERS[role], permission, RESOURCES[column])
    assert decision.allowed is expected
    if expected:
        assert decision.role == role
