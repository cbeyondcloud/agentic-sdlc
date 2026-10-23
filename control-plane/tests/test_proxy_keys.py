"""Proxy-key rotation and revocation through the gateway port (M2-17): a person rotates
their own key (the new key once, the old one refused by the gateway at once); an admin
revokes a person's keys; a gateway that cannot be reached leaves a revocation pending, and
the worker finishes it. Another tenant's person is unknown; audit records hold key ids
only."""

import pytest
from apiclient import Api, member
from fake_gateway import FakeGateway
from sqlalchemy import select, update

from agentic_cp.audit import read_records
from agentic_cp.auth.users import user_by_email
from agentic_cp.keys.store import issue_key, retry_revocations
from agentic_cp.tables import proxy_keys, tenants
from agentic_cp.tenancy.context import tenant_tx

pytestmark = pytest.mark.anyio


@pytest.fixture
async def keyed(api):
    """The app with a fake gateway; tenant a maps to gateway tenant `gw-a`."""
    gateway = FakeGateway()
    for tenant, name in ((api.a, "gw-a"), (api.b, "gw-b")):
        async with tenant_tx(api.db.owner, tenant.id) as conn:
            await conn.execute(
                update(tenants).where(tenants.c.id == tenant.id).values(gateway_tenant=name)
            )
    harness = Api(api.db, api.clock, api.a, api.b, gateway_keys=gateway)
    harness.gateway = gateway
    try:
        yield harness
    finally:
        await harness.http.aclose()


async def _give_key(api, tenant, email, team="payments"):
    async with tenant_tx(api.db.app, tenant.id) as conn:
        user = await user_by_email(conn, email)
        return await issue_key(
            conn,
            api.gateway,
            user=user,
            team=team,
            actor="system:test",
            actor_kind="system",
            clock=api.clock,
        )


async def _chain(api, tenant):
    async with tenant_tx(api.db.app, tenant.id) as conn:
        return await read_records(conn)


async def test_issue_binds_the_key_to_tenant_team_and_user(keyed):
    api = keyed
    await member(api, api.a, "dev@a.example.test", "developer", "payments")
    issued = await _give_key(api, api.a, "dev@a.example.test")
    [held] = api.gateway.active_for("dev@a.example.test")
    assert (held.tenant, held.team, held.key) == ("gw-a", "payments", issued.key)
    [record] = [r for r in await _chain(api, api.a) if r.event == "proxy_key.issued"]
    assert record.after == {
        "key_id": issued.key_id,
        "user": "dev@a.example.test",
        "team": "payments",
    }
    assert issued.key not in str(record)


async def test_a_person_rotates_their_own_key(keyed):
    api = keyed
    token, _ = await member(api, api.a, "dev@a.example.test", "developer", "payments")
    old = await _give_key(api, api.a, "dev@a.example.test")
    reply = await api.post("/v1/proxy-keys/rotate", token)
    assert reply.status_code == 200, reply.text
    new = reply.json()
    assert new["key"] != old.key and new["key_id"] != old.key_id
    assert api.gateway.accepts(new["key"]) and not api.gateway.accepts(old.key)
    assert reply.headers["cache-control"] == "no-store"
    async with tenant_tx(api.db.app, api.a.id) as conn:
        rows = {r.key_id: r.revoked_at for r in await conn.execute(select(proxy_keys))}
    assert rows[old.key_id] is not None and rows[new["key_id"]] is None
    [record] = [r for r in await _chain(api, api.a) if r.event == "proxy_key.rotated"]
    assert record.before == {"key_id": old.key_id} and record.after["key_id"] == new["key_id"]
    assert new["key"] not in str(record)


async def test_rotating_without_a_key_says_how_to_get_one(keyed):
    api = keyed
    token, _ = await member(api, api.a, "dev@a.example.test", "developer", "payments")
    reply = await api.post("/v1/proxy-keys/rotate", token)
    assert reply.status_code == 404 and reply.json()["code"] == "proxy_key.none"
    assert reply.json()["hint"]


async def test_rotation_with_the_gateway_down_changes_nothing(keyed):
    api = keyed
    token, _ = await member(api, api.a, "dev@a.example.test", "developer", "payments")
    old = await _give_key(api, api.a, "dev@a.example.test")
    api.gateway.down = True
    reply = await api.post("/v1/proxy-keys/rotate", token)
    assert reply.status_code == 503 and reply.json()["code"] == "gateway.unavailable"
    api.gateway.down = False
    assert api.gateway.accepts(old.key)


async def test_an_admin_revokes_a_persons_keys(keyed):
    api = keyed
    await member(api, api.a, "dev@a.example.test", "developer", "payments")
    old = await _give_key(api, api.a, "dev@a.example.test")
    reply = await api.delete("/v1/users/dev@a.example.test/proxy-keys", api.a.admin_token)
    assert reply.status_code == 200, reply.text
    assert reply.json() == {"email": "dev@a.example.test", "revoked": [old.key_id], "pending": []}
    assert not api.gateway.accepts(old.key)
    assert "proxy_key.revoked" in [r.event for r in await _chain(api, api.a)]


async def test_people_without_assign_rights_cannot_revoke(keyed):
    api = keyed
    await member(api, api.a, "dev@a.example.test", "developer", "payments")
    lead, _ = await member(api, api.a, "lead@a.example.test", "team-lead", "payments")
    reply = await api.delete("/v1/users/dev@a.example.test/proxy-keys", lead)
    assert reply.status_code == 403


async def test_a_revocation_the_gateway_misses_is_pending_and_the_worker_finishes_it(keyed):
    api = keyed
    await member(api, api.a, "dev@a.example.test", "developer", "payments")
    old = await _give_key(api, api.a, "dev@a.example.test")
    api.gateway.down = True
    reply = await api.delete("/v1/users/dev@a.example.test/proxy-keys", api.a.admin_token)
    assert reply.status_code == 200
    assert reply.json()["pending"] == [old.key_id] and reply.json()["revoked"] == []
    assert "proxy_key.revoke_pending" in [r.event for r in await _chain(api, api.a)]
    assert api.gateway.accepts(old.key)
    assert await retry_revocations(api.db.app, api.a.id, api.gateway, api.clock) == []
    api.gateway.down = False
    assert await retry_revocations(api.db.app, api.a.id, api.gateway, api.clock) == [old.key_id]
    assert not api.gateway.accepts(old.key)
    assert await retry_revocations(api.db.app, api.a.id, api.gateway, api.clock) == []


async def test_another_tenants_person_is_unknown(keyed):
    api = keyed
    await member(api, api.b, "dev@b.example.test", "developer", "payments")
    await _give_key(api, api.b, "dev@b.example.test")
    reply = await api.delete("/v1/users/dev@b.example.test/proxy-keys", api.a.admin_token)
    missing = await api.delete("/v1/users/nobody@a.example.test/proxy-keys", api.a.admin_token)
    assert reply.status_code == missing.status_code == 404
    assert len(api.gateway.active_for("dev@b.example.test")) == 1


async def test_without_a_gateway_tenant_no_key_is_issued(keyed):
    api = keyed
    async with tenant_tx(api.db.owner, api.a.id) as conn:
        await conn.execute(
            update(tenants).where(tenants.c.id == api.a.id).values(gateway_tenant=None)
        )
    await member(api, api.a, "dev@a.example.test", "developer", "payments")
    from agentic_cp.keys.gateway import GatewayUnavailable

    with pytest.raises(GatewayUnavailable, match="gateway tenant"):
        await _give_key(api, api.a, "dev@a.example.test")
    assert api.gateway.keys == {}
