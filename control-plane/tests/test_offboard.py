"""Offboarding (M2-18, Review Focus 4): an org admin offboards a person in one audited step -
disabled, every control-plane token and unused invite revoked, a `user:` kill switch for
their running work, their gateway keys revoked (or pending, finished by the worker) - and
the person cannot run any more."""

import pytest
from apiclient import Api, assign, member, settings_for
from fake_gateway import FakeGateway
from sqlalchemy import select, update

from agentic_cp.audit import read_records
from agentic_cp.keys.store import retry_revocations
from agentic_cp.tables import notifications, tenants
from agentic_cp.tenancy.context import tenant_tx

pytestmark = pytest.mark.anyio

REASON = {"reason": "left the company"}


@pytest.fixture
async def org(api):
    for tenant, name in ((api.a, "gw-a"), (api.b, "gw-b")):
        async with tenant_tx(api.db.owner, tenant.id) as conn:
            await conn.execute(
                update(tenants).where(tenants.c.id == tenant.id).values(gateway_tenant=name)
            )
    settings = settings_for(api.db).model_copy(update={"public_url": "https://cp.acme.example"})
    gateway = FakeGateway()
    harness = Api(api.db, api.clock, api.a, api.b, settings=settings, gateway_keys=gateway)
    harness.gateway = gateway
    try:
        yield harness
    finally:
        await harness.http.aclose()


async def _onboard(api, email="dev@a.example.test"):
    """Assign, then redeem the invite: the person holds a token and a gateway key."""
    await assign(api, api.a.admin_token, email, "developer", "payments")
    async with tenant_tx(api.db.app, api.a.id) as conn:
        body = (
            await conn.execute(
                select(notifications.c.body_text).where(notifications.c.recipient == email)
            )
        ).scalar_one()
    token = body.split("invite#", 1)[1].split('"', 1)[0].split()[0]
    redeemed = await api.post("/v1/invites/redeem", None, {"token": token})
    assert redeemed.status_code == 200, redeemed.text
    return redeemed.json()


async def _offboard(api, email="dev@a.example.test", token=None):
    return await api.post(f"/v1/users/{email}/offboard", token or api.a.admin_token, REASON)


async def test_an_offboarded_person_cannot_run_any_more(org):
    api = org
    got = await _onboard(api)
    reply = await _offboard(api)
    assert reply.status_code == 200, reply.text
    result = reply.json()
    assert result["email"] == "dev@a.example.test" and result["tokens_revoked"] == 1
    assert result["proxy_keys_revoked"] == [got["gateway_key_id"]]
    assert result["proxy_keys_pending"] == []
    token = got["control_plane_token"]
    assert (await api.get("/v1/me", token)).status_code == 401
    registration = {
        "run_id": "r-1",
        "team": "payments",
        "pipeline": "feature",
        "policy_version": "git:" + "c" * 40,
        "dry_run": True,
    }
    assert (await api.post("/v1/runs", token, registration)).status_code == 401
    assert not api.gateway.accepts(got["gateway_key"])
    switches = (await api.get("/v1/killswitch", api.a.admin_token)).json()
    [switch] = [s for s in switches if s["scope_kind"] == "user"]
    assert (switch["scope_value"], switch["mode"], switch["active"]) == (
        "dev@a.example.test",
        "kill",
        True,
    )
    assert switch["id"] == result["kill_switch"]


async def test_an_unused_invite_dies_with_the_offboarding(org):
    api = org
    await assign(api, api.a.admin_token, "dev@a.example.test", "developer", "payments")
    async with tenant_tx(api.db.app, api.a.id) as conn:
        body = (await conn.execute(select(notifications.c.body_text))).scalar_one()
    token = body.split("invite#", 1)[1].split('"', 1)[0].split()[0]
    reply = await _offboard(api)
    assert reply.status_code == 200 and reply.json()["invites_revoked"] == 1
    assert (await api.post("/v1/invites/redeem", None, {"token": token})).status_code == 404


async def test_offboarding_is_audited_and_happens_once(org):
    api = org
    await _onboard(api)
    await _offboard(api)
    async with tenant_tx(api.db.app, api.a.id) as conn:
        chain = await read_records(conn)
    [record] = [r for r in chain if r.event == "user.offboarded"]
    assert record.actor == "admin@a.example.test" and record.reason == "left the company"
    assert {"killswitch.set", "proxy_key.revoked"} <= {r.event for r in chain}
    again = await _offboard(api)
    assert again.status_code == 409 and again.json()["code"] == "user.disabled"


async def test_team_leads_cannot_offboard(org):
    api = org
    lead, _ = await member(api, api.a, "lead@a.example.test", "team-lead", "payments")
    await _onboard(api)
    assert (await _offboard(api, token=lead)).status_code == 403


async def test_the_last_org_admin_cannot_be_offboarded(org):
    api = org
    reply = await _offboard(api, "admin@a.example.test")
    assert reply.status_code == 409 and reply.json()["code"] == "rbac.last_admin"
    other, _ = await member(api, api.a, "admin2@a.example.test", "org-admin")
    assert (await _offboard(api, "admin@a.example.test", token=other)).status_code == 200


async def test_another_tenants_person_is_unknown(org):
    api = org
    await member(api, api.b, "dev@b.example.test", "developer", "payments")
    reply = await _offboard(api, "dev@b.example.test")
    missing = await _offboard(api, "nobody@a.example.test")
    assert reply.status_code == missing.status_code == 404
    assert reply.json()["detail"].replace("dev@b", "x") == missing.json()["detail"].replace(
        "nobody@a", "x"
    )


async def test_a_gateway_outage_leaves_the_key_pending_until_the_worker_revokes_it(org):
    api = org
    got = await _onboard(api)
    api.gateway.down = True
    reply = await _offboard(api)
    assert reply.status_code == 200
    assert reply.json()["proxy_keys_pending"] == [got["gateway_key_id"]]
    assert (await api.get("/v1/me", got["control_plane_token"])).status_code == 401
    api.gateway.down = False
    assert await retry_revocations(api.db.app, api.a.id, api.gateway, api.clock) == [
        got["gateway_key_id"]
    ]
    assert not api.gateway.accepts(got["gateway_key"])
