"""RBAC touch-ups from the slice-1a review (DF-417, DF-419)."""

from __future__ import annotations

import pytest
from apiclient import member
from sqlalchemy import select, update

from agentic_cp.audit import read_records
from agentic_cp.rbac import defaults
from agentic_cp.rbac.defaults import DEFAULT_ROLES, ORG_ADMIN, RoleSpec, seed_roles
from agentic_cp.tables import roles, tenants
from agentic_cp.tenancy.context import tenant_tx

pytestmark = pytest.mark.anyio


async def test_put_role_authorises_before_validating(api):
    """DF-417: a caller without rbac.assign gets 403, whatever the body says."""
    developer, _ = await member(api, api.a, "dev@a.example.test", "developer", "payments")
    unknown = await api.put("/v1/rbac/roles/x-role", developer, {"permissions": ["no.such"]})
    assert unknown.status_code == 403, unknown.text
    admin_role = await api.put(f"/v1/rbac/roles/{ORG_ADMIN}", developer, {"permissions": []})
    assert admin_role.status_code == 403
    bad_name = await api.put("/v1/rbac/roles/Bad_Name", developer, {"permissions": []})
    assert bad_name.status_code == 403
    still = await api.put("/v1/rbac/roles/x-role", api.a.admin_token, {"permissions": ["no.such"]})
    assert still.status_code == 422  # an admin still gets the validation answer


async def _role(conn, name):
    row = (
        await conn.execute(
            select(roles.c.permissions, roles.c.seed_permissions).where(roles.c.name == name)
        )
    ).one()
    return set(row.permissions), set(row.seed_permissions or [])


async def test_seed_records_what_it_gave(api):
    async with tenant_tx(api.db.app, api.a.id) as conn:
        perms, seeded = await _role(conn, "developer")
    spec = {s.name: s for s in DEFAULT_ROLES}["developer"]
    assert perms == seeded == set(spec.permissions)


async def test_seed_adds_new_catalogue_permissions_but_keeps_tenant_removals(api, monkeypatch):
    """DF-419 (R22): a permission new in this release reaches the builtin role; one the
    tenant removed stays removed."""
    async with tenant_tx(api.db.owner, api.a.id) as conn:
        perms, _ = await _role(conn, "approver")
        await conn.execute(  # the tenant removed agent.propose.repo from approver
            update(roles)
            .where(roles.c.name == "approver")
            .values(permissions=sorted(perms - {"agent.propose.repo"}))
        )
        before = (
            await conn.execute(select(tenants.c.rbac_revision).where(tenants.c.id == api.a.id))
        ).scalar_one()
    release = [
        RoleSpec(s.name, (*s.permissions, "audit.read.own"), s.team_assignable, s.assignable)
        if s.name == "approver"
        else s
        for s in DEFAULT_ROLES
    ]
    monkeypatch.setattr(defaults, "DEFAULT_ROLES", tuple(release))
    async with tenant_tx(api.db.owner, api.a.id) as conn:
        changed = await seed_roles(conn, clock=api.clock)
        perms, seeded = await _role(conn, "approver")
        records = await read_records(conn)
        after = (
            await conn.execute(select(tenants.c.rbac_revision).where(tenants.c.id == api.a.id))
        ).scalar_one()
    assert changed == ["approver"]
    assert "audit.read.own" in perms
    assert "agent.propose.repo" not in perms  # the tenant's removal is kept
    assert "audit.read.own" in seeded and "agent.propose.repo" in seeded
    assert records[-1].event == "rbac.seeded"
    assert records[-1].after["added"] == {"approver": ["audit.read.own"]}
    assert after == before + 1
    async with tenant_tx(api.db.owner, api.a.id) as conn:
        assert await seed_roles(conn, clock=api.clock) == []  # idempotent


async def test_seed_on_a_role_seeded_before_tracking_adds_nothing(api):
    """A builtin role from before `seed_permissions` existed is taken as seeded with this
    release's defaults: nothing is added, the tracking starts."""
    async with tenant_tx(api.db.owner, api.a.id) as conn:
        await conn.execute(
            update(roles).where(roles.c.name == "auditor").values(seed_permissions=None)
        )
        perms_before, _ = await _role(conn, "auditor")
        assert await seed_roles(conn, clock=api.clock) == []
        perms, seeded = await _role(conn, "auditor")
    assert perms == perms_before and seeded
