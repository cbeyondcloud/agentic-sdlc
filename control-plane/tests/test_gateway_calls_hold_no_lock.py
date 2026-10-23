"""A slow gateway never stalls the tenant (review F2). Every audited write takes the
tenant's audit-chain lock until commit, and RBAC writes the tenant's RBAC lock; a gateway
call (up to the client's timeout) made while either is held would make every other
audited write in the tenant wait for it. So redeeming an invite, offboarding, an admin's
key revocation and the worker's retries make their gateway calls while holding neither:
an unrelated assignment in the same tenant goes through while the gateway is still busy.
A person with two keys has the second revocation held - after the first one is done."""

import asyncio

import pytest
from apiclient import Api, assign, settings_for
from fake_gateway import FakeGateway
from sqlalchemy import select, update

from agentic_cp.auth.users import user_by_email
from agentic_cp.keys.store import issue_key, retry_revocations
from agentic_cp.tables import notifications, tenants
from agentic_cp.tenancy.context import tenant_tx

pytestmark = pytest.mark.anyio

EMAIL = "dev@a.example.test"
#: Far below the real client's 15 s timeout, far above an uncontended write.
PROMPT = 10.0


@pytest.fixture
async def org(api):
    async with tenant_tx(api.db.owner, api.a.id) as conn:
        await conn.execute(
            update(tenants).where(tenants.c.id == api.a.id).values(gateway_tenant="gw-a")
        )
    settings = settings_for(api.db).model_copy(update={"public_url": "https://cp.acme.example"})
    gateway = FakeGateway()
    harness = Api(api.db, api.clock, api.a, api.b, settings=settings, gateway_keys=gateway)
    harness.gateway = gateway
    try:
        yield harness
    finally:
        await harness.http.aclose()


async def _invite_token(api, email=EMAIL) -> str:
    assert (await assign(api, api.a.admin_token, email, "developer", "payments")).status_code == 201
    async with tenant_tx(api.db.app, api.a.id) as conn:
        body = (
            await conn.execute(
                select(notifications.c.body_text).where(notifications.c.recipient == email)
            )
        ).scalar_one()
    return body.split("invite#", 1)[1].split('"', 1)[0].split()[0]


async def _two_keys(api) -> list[str]:
    """Onboard the person (one key from the redemption), then give them a second key."""
    redeemed = await api.post("/v1/invites/redeem", None, {"token": await _invite_token(api)})
    assert redeemed.status_code == 200, redeemed.text
    async with tenant_tx(api.db.app, api.a.id) as conn:
        second = await issue_key(
            conn,
            api.gateway,
            user=await user_by_email(conn, EMAIL),
            team="payments",
            actor="system:test",
            actor_kind="system",
            clock=api.clock,
        )
    return sorted([redeemed.json()["gateway_key_id"], second.key_id])


async def _while_the_gateway_waits(api, call: str, work, *, skip: int = 0):
    """Start `work` (which reaches the gateway's `call`), and while the gateway holds it make
    an unrelated audited RBAC write in the same tenant: it must not wait for the gateway."""
    api.gateway.hold, api.gateway.hold_skip = call, skip
    task = asyncio.ensure_future(work())
    try:
        await asyncio.wait_for(api.gateway.entered.wait(), PROMPT)
        other = await asyncio.wait_for(
            assign(api, api.a.admin_token, "other@a.example.test", "developer", "search"), PROMPT
        )
        assert other.status_code == 201, other.text
    finally:
        api.gateway.release.set()
    return await task


def _revoked(api, key_ids) -> bool:
    return all(api.gateway.keys[k].revoked for k in key_ids)


async def test_redeeming_calls_the_gateway_without_holding_the_tenant(org):
    api = org
    token = await _invite_token(api)
    redeemed = await _while_the_gateway_waits(
        api, "create", lambda: api.post("/v1/invites/redeem", None, {"token": token})
    )
    assert redeemed.status_code == 200, redeemed.text
    assert api.gateway.accepts(redeemed.json()["gateway_key"])


async def test_offboarding_calls_the_gateway_without_holding_the_tenant(org):
    api = org
    keys = await _two_keys(api)
    reply = await _while_the_gateway_waits(
        api,
        "revoke",
        lambda: api.post(f"/v1/users/{EMAIL}/offboard", api.a.admin_token, {"reason": "left"}),
        skip=1,
    )
    assert reply.status_code == 200, reply.text
    assert sorted(reply.json()["proxy_keys_revoked"]) == keys
    assert _revoked(api, keys)


async def test_an_admins_revocation_calls_the_gateway_without_holding_the_tenant(org):
    api = org
    keys = await _two_keys(api)
    reply = await _while_the_gateway_waits(
        api,
        "revoke",
        lambda: api.delete(f"/v1/users/{EMAIL}/proxy-keys", api.a.admin_token),
        skip=1,
    )
    assert reply.status_code == 200, reply.text
    assert sorted(reply.json()["revoked"]) == keys
    assert _revoked(api, keys)


async def test_the_workers_retry_calls_the_gateway_without_holding_the_tenant(org):
    api = org
    keys = await _two_keys(api)
    api.gateway.down = True
    pending = await api.delete(f"/v1/users/{EMAIL}/proxy-keys", api.a.admin_token)
    assert sorted(pending.json()["pending"]) == keys
    api.gateway.down = False
    done = await _while_the_gateway_waits(
        api,
        "revoke",
        lambda: retry_revocations(api.db.app, api.a.id, api.gateway, api.clock),
        skip=1,
    )
    assert sorted(done) == keys
    assert _revoked(api, keys)
