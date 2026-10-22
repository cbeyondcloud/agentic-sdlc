"""The RBAC API (M2-03, Review Focus 4 and 7): roles, assignments, checks, guards, and an
enforcer that never drifts from the tables - not across replicas, not after a refusal."""

import pytest
from apiclient import assign, check, member
from sqlalchemy import select

from agentic_cp.audit import read_records
from agentic_cp.rbac.authz import build_enforcer
from agentic_cp.rbac.defaults import DEFAULT_ROLES
from agentic_cp.rbac.store import PolicyRows, load_rows
from agentic_cp.tables import tenants
from agentic_cp.tenancy.context import tenant_tx

pytestmark = pytest.mark.anyio


async def revision(api, tenant) -> int:
    async with api.db.owner.begin() as conn:
        found = await conn.execute(select(tenants.c.rbac_revision).where(tenants.c.id == tenant.id))
        return found.scalar_one()


async def test_enforcer_built_from_tables_equals_seed(api):
    async with tenant_tx(api.db.app, api.a.id) as conn:
        _, rows = await load_rows(conn)
    seed = PolicyRows(
        tenant_id=api.a.id,
        roles={spec.name: frozenset(spec.permissions) for spec in DEFAULT_ROLES},
        grants=rows.grants,
    )
    assert sorted(build_enforcer(rows).get_policy()) == sorted(build_enforcer(seed).get_policy())
    assert [(g.user_id, g.role, g.scope) for g in rows.grants] == [
        (api.a.admin_id, "org-admin", "org")
    ]


async def test_role_change_visible_on_next_request_across_replicas(api):
    token, _ = await member(api, api.a, "dev@a.example.test", "developer", "payments")
    replica = api.with_app()
    assert not (await check(replica, token, "policy.edit.team", team="payments"))["allowed"]
    granted = await assign(api, api.a.admin_token, "dev@a.example.test", "team-lead", "payments")
    assert granted.status_code == 201
    assert (await check(replica, token, "policy.edit.team", team="payments"))["allowed"]
    revoked = await api.delete(f"/v1/rbac/assignments/{granted.json()['id']}", api.a.admin_token)
    assert revoked.status_code == 204
    assert not (await check(replica, token, "policy.edit.team", team="payments"))["allowed"]


async def _admin_assignment_id(api, tenant) -> str:
    listed = await api.get("/v1/rbac/assignments", tenant.admin_token)
    return next(a["id"] for a in listed.json() if a["role"] == "org-admin")


async def test_last_org_admin_cannot_be_removed(api):
    only = await _admin_assignment_id(api, api.a)
    refused = await api.delete(f"/v1/rbac/assignments/{only}", api.a.admin_token)
    assert refused.status_code == 409
    assert refused.json()["code"] == "rbac.last_admin"
    await member(api, api.a, "second@a.example.test", "org-admin")
    assert (await api.delete(f"/v1/rbac/assignments/{only}", api.a.admin_token)).status_code == 204


async def test_failed_change_does_not_bump_revision(api):
    before = await revision(api, api.a)
    async with tenant_tx(api.db.app, api.a.id) as conn:
        chain = len(await read_records(conn))
    only = await _admin_assignment_id(api, api.a)
    assert (await api.delete(f"/v1/rbac/assignments/{only}", api.a.admin_token)).status_code == 409
    assert await revision(api, api.a) == before
    async with tenant_tx(api.db.app, api.a.id) as conn:
        assert len(await read_records(conn)) == chain


async def test_rbac_in_tenant_a_does_not_affect_tenant_b(api):
    in_a, _ = await member(api, api.a, "same@example.test", "org-admin")
    in_b, _ = await member(api, api.b, "same@example.test", "developer", "payments")
    assert (await check(api, in_a, "rbac.assign"))["allowed"]
    assert not (await check(api, in_b, "rbac.assign"))["allowed"]
    assert (await check(api, in_b, "run.trigger", team="payments"))["allowed"]


async def test_team_lead_assigns_only_developer_or_approver_in_own_team(api):
    lead, _ = await member(api, api.a, "lead@a.example.test", "team-lead", "payments")
    ok = await assign(api, lead, "new@a.example.test", "approver", "payments")
    assert ok.status_code == 201
    assert ok.json() == {
        "id": ok.json()["id"],
        "email": "new@a.example.test",
        "role": "approver",
        "scope": "team",
        "team": "payments",
    }
    higher = await assign(api, lead, "new@a.example.test", "team-lead", "payments")
    assert higher.status_code == 403
    assert higher.json()["code"] == "rbac.not_assignable"
    elsewhere = await assign(api, lead, "new@a.example.test", "developer", "billing")
    assert elsewhere.status_code == 403
    assert elsewhere.json()["code"] == "rbac.forbidden"
    org_wide = await assign(api, lead, "new@a.example.test", "developer")
    assert org_wide.status_code == 403


async def test_derived_role_cannot_be_assigned(api):
    reply = await assign(api, api.a.admin_token, "x@a.example.test", "repo-maintainer", "payments")
    assert reply.status_code == 422
    assert reply.json()["code"] == "rbac.not_assignable"


async def test_unknown_role_is_refused(api):
    reply = await assign(api, api.a.admin_token, "x@a.example.test", "wizard")
    assert reply.status_code == 422
    assert reply.json()["code"] == "rbac.unknown_role"


async def test_role_edit_cannot_remove_rbac_assign_from_org_admin(api):
    role = (await api.get("/v1/rbac/roles/org-admin", api.a.admin_token)).json()
    stripped = [p for p in role["permissions"] if p != "rbac.assign"]
    reply = await api.put("/v1/rbac/roles/org-admin", api.a.admin_token, {"permissions": stripped})
    assert reply.status_code == 409
    assert reply.json()["code"] == "rbac.admin_needs_assign"


async def test_unknown_permission_in_role_refused(api):
    reply = await api.put(
        "/v1/rbac/roles/release-manager",
        api.a.admin_token,
        {"permissions": ["run.trigger", "bogus.perm"]},
    )
    assert reply.status_code == 422
    assert "bogus.perm" in reply.json()["detail"]


async def test_custom_role_grants_once_assigned(api):
    created = await api.put(
        "/v1/rbac/roles/release-manager", api.a.admin_token, {"permissions": ["killswitch.team"]}
    )
    assert created.status_code == 201
    assert created.json()["builtin"] is False
    token, _ = await member(api, api.a, "rel@a.example.test", "release-manager", "payments")
    assert (await check(api, token, "killswitch.team", team="payments"))["allowed"]
    in_use = await api.delete("/v1/rbac/roles/release-manager", api.a.admin_token)
    assert in_use.status_code == 409
    builtin = await api.delete("/v1/rbac/roles/developer", api.a.admin_token)
    assert builtin.status_code == 409
    assert builtin.json()["code"] == "rbac.builtin_role"


async def test_only_rbac_assign_may_edit_roles(api):
    token, _ = await member(api, api.a, "dev@a.example.test", "developer", "payments")
    reply = await api.put("/v1/rbac/roles/sneaky", token, {"permissions": ["rbac.assign"]})
    assert reply.status_code == 403
    assert reply.json()["code"] == "rbac.forbidden"


async def test_every_rbac_change_is_audited_with_before_and_after(api):
    admin = api.a.admin_token
    await api.put("/v1/rbac/roles/release-manager", admin, {"permissions": ["killswitch.team"]})
    await api.put(
        "/v1/rbac/roles/release-manager", admin, {"permissions": ["killswitch.team", "run.trigger"]}
    )
    _, assignment = await member(api, api.a, "rel@a.example.test", "release-manager", "payments")
    await api.delete(f"/v1/rbac/assignments/{assignment}", admin)
    await api.delete("/v1/rbac/roles/release-manager", admin)
    async with tenant_tx(api.db.app, api.a.id) as conn:
        records = [r for r in await read_records(conn) if r.event.startswith("role.")]
    assert [r.event for r in records] == [
        "role.updated",
        "role.updated",
        "role.assigned",
        "role.revoked",
        "role.deleted",
    ]
    created, updated, assigned, revoked, deleted = records
    assert created.before is None
    assert created.after["permissions"] == ["killswitch.team"]
    assert updated.before["permissions"] == ["killswitch.team"]
    assert updated.after["permissions"] == ["killswitch.team", "run.trigger"]
    assert assigned.before is None and assigned.after["team"] == "payments"
    assert revoked.before["role"] == "release-manager"
    assert set(revoked.after) == {"rbac_revision"}
    assert deleted.before["name"] == "release-manager"
    assert set(deleted.after) == {"rbac_revision"}
    revisions = [r.after["rbac_revision"] for r in records]
    assert revisions == sorted(revisions) and len(set(revisions)) == len(records)


async def test_check_api_answers_for_caller(api):
    token, _ = await member(api, api.a, "dev@a.example.test", "developer", "payments")
    yes = await check(api, token, "gate.approve", team="payments", repo="github.com/acme/api")
    assert yes["allowed"] and yes["role"] == "developer" and yes["scope"] == "team:payments"
    no = await check(api, token, "policy.edit.team", team="payments")
    assert no["allowed"] is False and "policy.edit.team" in no["reason"]
    unknown = await api.post("/v1/rbac/check", token, {"permission": "no.such"})
    assert unknown.status_code == 422


async def test_checking_another_user_needs_rbac_assign(api):
    token, _ = await member(api, api.a, "dev@a.example.test", "developer", "payments")
    refused = await api.post(
        "/v1/rbac/check", token, {"permission": "rbac.assign", "user": "admin@a.example.test"}
    )
    assert refused.status_code == 403
    admin_view = await check(
        api, api.a.admin_token, "run.trigger", team="payments", user="dev@a.example.test"
    )
    assert admin_view["allowed"]
    missing = await api.post(
        "/v1/rbac/check", api.a.admin_token, {"permission": "run.trigger", "user": "no@x.test"}
    )
    assert missing.status_code == 404


async def test_me_lists_effective_permissions(api):
    token, _ = await member(api, api.a, "dev@a.example.test", "developer", "payments")
    me = (await api.get("/v1/me", token)).json()
    assert me["assignments"] == [{"role": "developer", "scope": "team", "team": "payments"}]
    assert "run.trigger" in me["permissions"]["teams"]["payments"]
    assert me["permissions"]["org"] == []
    admin = (await api.get("/v1/me", api.a.admin_token)).json()
    assert "rbac.assign" in admin["permissions"]["org"]


async def test_roles_are_listed_per_tenant(api):
    await api.put("/v1/rbac/roles/a-only", api.a.admin_token, {"permissions": ["run.trigger"]})
    names_a = {r["name"] for r in (await api.get("/v1/rbac/roles", api.a.admin_token)).json()}
    names_b = {r["name"] for r in (await api.get("/v1/rbac/roles", api.b.admin_token)).json()}
    assert "a-only" in names_a and "a-only" not in names_b
    assert {spec.name for spec in DEFAULT_ROLES} <= names_b
