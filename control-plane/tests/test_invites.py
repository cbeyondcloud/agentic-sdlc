"""Invites (M2-18, Review Focus 4): a person new to the tenant gets one invite mail whose
link carries a single-use, expiring token in its fragment; redeeming it once gives a
control-plane token bound to the invite's tenant and a gateway key bound to tenant, team
and user. Used, expired, revoked and unknown links are refused; the token never enters the
audit chain."""

import re
from datetime import timedelta

import pytest
from apiclient import Api, assign, member, settings_for
from fake_gateway import FakeGateway
from sqlalchemy import select, update

from agentic_cp.audit import read_records
from agentic_cp.tables import notifications, tenants
from agentic_cp.tenancy.context import tenant_tx

pytestmark = pytest.mark.anyio

PUBLIC = "https://cp.acme.example"
GATEWAY_URL = "https://gateway.acme.example"
LINK = re.compile(r"https://cp\.acme\.example/invite#(aci_[A-Za-z0-9_-]+)")


@pytest.fixture
async def inviting(api):
    """The app with a public URL, a fake gateway and gateway tenants for a and b."""
    for tenant, name in ((api.a, "gw-a"), (api.b, "gw-b")):
        async with tenant_tx(api.db.owner, tenant.id) as conn:
            await conn.execute(
                update(tenants).where(tenants.c.id == tenant.id).values(gateway_tenant=name)
            )
    settings = settings_for(api.db).model_copy(
        update={"public_url": PUBLIC, "gateway_url": GATEWAY_URL, "smtp_host": "127.0.0.1"}
    )
    gateway = FakeGateway()
    harness = Api(api.db, api.clock, api.a, api.b, settings=settings, gateway_keys=gateway)
    harness.gateway = gateway
    try:
        yield harness
    finally:
        await harness.http.aclose()


async def _mails(api, tenant):
    async with tenant_tx(api.db.app, tenant.id) as conn:
        found = select(notifications).where(notifications.c.kind == "invite")
        return list(await conn.execute(found.order_by(notifications.c.created_at)))


async def _token_for(api, tenant, email):
    mails = [m for m in await _mails(api, tenant) if m.recipient == email]
    return LINK.search(mails[-1].body_text).group(1)


async def _redeem(api, token):
    return await api.post("/v1/invites/redeem", None, {"token": token})


async def test_a_new_persons_assignment_queues_one_invite_mail(inviting):
    api = inviting
    reply = await assign(api, api.a.admin_token, "dev@a.example.test", "developer", "payments")
    assert reply.status_code == 201 and reply.json()["invited"] is True
    [mail] = await _mails(api, api.a)
    assert mail.recipient == "dev@a.example.test" and mail.status == "pending"
    assert LINK.search(mail.body_text) and LINK.search(mail.body_html)
    assert "developer" in mail.body_text and "payments" in mail.body_text
    again = await assign(api, api.a.admin_token, "dev@a.example.test", "approver", "payments")
    assert again.json()["invited"] is False and len(await _mails(api, api.a)) == 1
    events = [r.event for r in await _chain(api, api.a)]
    assert "invite.created" in events


async def _chain(api, tenant):
    async with tenant_tx(api.db.app, tenant.id) as conn:
        return await read_records(conn)


async def test_redeeming_gives_a_token_of_the_invites_tenant_and_a_bound_gateway_key(inviting):
    api = inviting
    await assign(api, api.a.admin_token, "dev@a.example.test", "developer", "payments")
    token = await _token_for(api, api.a, "dev@a.example.test")
    reply = await _redeem(api, token)
    assert reply.status_code == 200, reply.text
    assert reply.headers["cache-control"] == "no-store"
    got = reply.json()
    assert (got["tenant"]["slug"], got["email"]) == ("tenant-a", "dev@a.example.test")
    assert (got["control_plane_url"], got["gateway_url"]) == (PUBLIC, GATEWAY_URL)
    me = await api.get("/v1/me", got["control_plane_token"])
    assert me.status_code == 200 and me.json()["tenant"]["slug"] == "tenant-a"
    assert me.json()["user"]["email"] == "dev@a.example.test"
    [key] = api.gateway.active_for("dev@a.example.test")
    assert (key.key, key.tenant, key.team) == (got["gateway_key"], "gw-a", "payments")
    chain = await _chain(api, api.a)
    assert {"invite.redeemed", "token.issued", "proxy_key.issued"} <= {r.event for r in chain}
    for secret in (token, got["control_plane_token"], got["gateway_key"]):
        assert all(secret not in str(record) for record in chain)


async def test_a_link_works_once(inviting):
    api = inviting
    await assign(api, api.a.admin_token, "dev@a.example.test", "developer", "payments")
    token = await _token_for(api, api.a, "dev@a.example.test")
    assert (await _redeem(api, token)).status_code == 200
    again = await _redeem(api, token)
    assert again.status_code == 409 and again.json()["code"] == "invite.used"
    assert len(api.gateway.active_for("dev@a.example.test")) == 1


async def test_an_expired_link_is_refused(inviting):
    api = inviting
    await assign(api, api.a.admin_token, "dev@a.example.test", "developer", "payments")
    token = await _token_for(api, api.a, "dev@a.example.test")
    api.clock.advance(timedelta(hours=72, seconds=1))
    reply = await _redeem(api, token)
    assert reply.status_code == 410 and reply.json()["code"] == "invite.expired"
    assert "invite" in reply.json()["hint"]


async def test_a_new_invite_revokes_the_earlier_one(inviting):
    api = inviting
    await assign(api, api.a.admin_token, "dev@a.example.test", "developer", "payments")
    first = await _token_for(api, api.a, "dev@a.example.test")
    api.clock.advance(timedelta(seconds=1))  # mails are ordered by when they were queued
    reply = await api.post("/v1/users/dev@a.example.test/invite", api.a.admin_token)
    assert reply.status_code == 201 and reply.json()["mailed"] is True
    second = await _token_for(api, api.a, "dev@a.example.test")
    assert first != second
    assert (await _redeem(api, first)).status_code == 404
    assert (await _redeem(api, second)).status_code == 200


async def test_unknown_and_malformed_links_answer_alike(inviting):
    api = inviting
    unknown = await _redeem(api, "aci_" + "Q" * 43)
    malformed = await _redeem(api, "aci_short!!")
    assert unknown.status_code == malformed.status_code == 404
    assert unknown.json() == malformed.json()
    assert unknown.json()["code"] == "invite.invalid"


async def test_a_gateway_failure_rolls_the_redemption_back(inviting):
    api = inviting
    await assign(api, api.a.admin_token, "dev@a.example.test", "developer", "payments")
    token = await _token_for(api, api.a, "dev@a.example.test")
    api.gateway.down = True
    reply = await _redeem(api, token)
    assert reply.status_code == 503 and reply.json()["code"] == "gateway.unavailable"
    api.gateway.down = False
    assert (await _redeem(api, token)).status_code == 200  # still usable


async def test_without_a_gateway_tenant_only_the_token_is_given(inviting):
    api = inviting
    async with tenant_tx(api.db.owner, api.a.id) as conn:
        await conn.execute(
            update(tenants).where(tenants.c.id == api.a.id).values(gateway_tenant=None)
        )
    await assign(api, api.a.admin_token, "dev@a.example.test", "developer", "payments")
    reply = await _redeem(api, await _token_for(api, api.a, "dev@a.example.test"))
    assert reply.status_code == 200
    got = reply.json()
    assert got["gateway_key"] is None and "gateway key" in got["notice"]
    assert api.gateway.keys == {}


async def test_a_suspended_tenants_invite_is_invalid(inviting):
    api = inviting
    await assign(api, api.a.admin_token, "dev@a.example.test", "developer", "payments")
    token = await _token_for(api, api.a, "dev@a.example.test")
    async with tenant_tx(api.db.owner, api.a.id) as conn:
        await conn.execute(
            update(tenants).where(tenants.c.id == api.a.id).values(status="suspended")
        )
    assert (await _redeem(api, token)).status_code == 404


async def test_without_a_public_url_nothing_is_mailed(api):
    reply = await assign(api, api.a.admin_token, "dev@a.example.test", "developer", "payments")
    assert reply.json()["invited"] is False
    assert await _mails(api, api.a) == []
    again = await api.post("/v1/users/dev@a.example.test/invite", api.a.admin_token)
    assert again.status_code == 409 and again.json()["code"] == "invite.unconfigured"


async def test_without_a_relay_the_invite_is_queued_not_mailed(inviting):
    api = inviting
    quiet = Api(
        api.db,
        api.clock,
        api.a,
        api.b,
        settings=api.settings.model_copy(update={"smtp_host": None}),
        gateway_keys=api.gateway,
    )
    try:
        await assign(quiet, api.a.admin_token, "org@a.example.test", "auditor")
        reply = await quiet.post("/v1/users/org@a.example.test/invite", api.a.admin_token)
    finally:
        await quiet.http.aclose()
    assert reply.status_code == 201 and reply.json()["mailed"] is False
    [*_, mail] = await _mails(api, api.a)
    assert "auditor" in mail.body_text  # an org-scope person's own role, not a made-up one


async def test_team_leads_reinvite_their_own_members_only(inviting):
    api = inviting
    lead, _ = await member(api, api.a, "lead@a.example.test", "team-lead", "payments")
    await assign(api, api.a.admin_token, "dev@a.example.test", "developer", "payments")
    await assign(api, api.a.admin_token, "other@a.example.test", "developer", "search")
    assert (await api.post("/v1/users/dev@a.example.test/invite", lead)).status_code == 201
    assert (await api.post("/v1/users/other@a.example.test/invite", lead)).status_code == 403
    dev, _ = await member(api, api.a, "dev2@a.example.test", "developer", "payments")
    assert (await api.post("/v1/users/dev@a.example.test/invite", dev)).status_code == 403
