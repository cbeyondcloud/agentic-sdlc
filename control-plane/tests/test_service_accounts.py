"""Service accounts (M2-14, Review Focus 2): team-scoped, `run.trigger` only, an expiring
token shown once. A service account cannot approve gates, change roles or assignments, set
switches or create accounts; it can never be given another role, and the service-account
role cannot be widened - and even a hand-made grant gives it nothing more."""

from datetime import timedelta

import pytest
from apiclient import check, member
from sqlalchemy import select

from agentic_cp.audit import read_records
from agentic_cp.tables import assignments, users
from agentic_cp.tenancy.context import tenant_tx

pytestmark = pytest.mark.anyio

BODY = {"name": "nightly", "team": "payments", "days": 30}


async def _create(api, token, body=None):
    return await api.post("/v1/service-accounts", token, body or BODY)


async def test_an_org_admin_creates_a_service_account_with_a_token_shown_once(api):
    reply = await _create(api, api.a.admin_token)
    assert reply.status_code == 201, reply.text
    made = reply.json()
    assert made["email"] == "nightly@payments.service.invalid" and made["team"] == "payments"
    me = await api.get("/v1/me", made["token"])
    assert me.status_code == 200
    assert me.json()["kind"] == "service" and me.json()["tenant"]["slug"] == "tenant-a"
    assert me.json()["assignments"] == [
        {
            "role": "service-account",
            "scope": "team",
            "team": "payments",
            "repo": None,
            "derived": False,
        }
    ]
    assert made["expires_at"].startswith((api.clock.now + timedelta(days=30)).date().isoformat())
    listed = await api.get("/v1/service-accounts", api.a.admin_token)
    assert listed.status_code == 200
    assert [a["email"] for a in listed.json()] == [made["email"]]
    assert made["token"] not in listed.text
    async with tenant_tx(api.db.app, api.a.id) as conn:
        events = [r.event for r in await read_records(conn)]
    assert events[-2:] == ["service_account.created", "token.issued"]


async def test_a_team_lead_creates_one_in_their_own_team_only(api):
    lead, _ = await member(api, api.a, "lead@a.example.test", "team-lead", "payments")
    assert (await _create(api, lead)).status_code == 201
    other = await _create(api, lead, {**BODY, "team": "search"})
    assert other.status_code == 403 and other.json()["code"] == "rbac.forbidden"
    dev, _ = await member(api, api.a, "dev@a.example.test", "developer", "payments")
    assert (await _create(api, dev, {**BODY, "name": "other"})).status_code == 403
    listed = await api.get("/v1/service-accounts?team=payments", lead)
    assert listed.status_code == 200 and len(listed.json()) == 1
    assert (await api.get("/v1/service-accounts?team=search", lead)).status_code == 403


@pytest.mark.parametrize("days", [None, 0, 366])
async def test_expiry_is_required_and_at_most_365_days(api, days):
    body = {"name": "nightly", "team": "payments"}
    if days is not None:
        body["days"] = days
    assert (await _create(api, api.a.admin_token, body)).status_code == 422


async def test_the_deployments_service_token_maximum_applies(api):
    capped = api.with_settings(service_token_max_days=30)
    try:
        reply = await capped.post("/v1/service-accounts", api.a.admin_token, BODY | {"days": 31})
        assert reply.status_code == 422 and reply.json()["code"] == "auth.lifetime_too_long"
        assert (
            await capped.post("/v1/service-accounts", api.a.admin_token, BODY)
        ).status_code == 201
    finally:
        await capped.http.aclose()


async def test_a_service_account_never_gets_a_second_team(api):
    made = (await _create(api, api.a.admin_token)).json()
    reply = await api.post(
        "/v1/rbac/assignments",
        api.a.admin_token,
        {"email": made["email"], "role": "service-account", "team": "search"},
    )
    assert reply.status_code == 422 and reply.json()["code"] == "rbac.service_account_role"


async def test_a_name_is_unique_within_its_team(api):
    assert (await _create(api, api.a.admin_token)).status_code == 201
    again = await _create(api, api.a.admin_token)
    assert again.status_code == 409 and again.json()["code"] == "service_account.exists"
    assert (await _create(api, api.a.admin_token, {**BODY, "team": "search"})).status_code == 201


async def test_a_service_account_triggers_runs_and_nothing_else(api):
    token = (await _create(api, api.a.admin_token)).json()["token"]
    assert (await check(api, token, "run.trigger", team="payments"))["allowed"]
    assert not (await check(api, token, "run.trigger", team="search"))["allowed"]
    assert not (await check(api, token, "gate.approve", team="payments"))["allowed"]
    refused = [
        await api.post(
            "/v1/rbac/assignments",
            token,
            {"email": "x@a.example.test", "role": "developer", "team": "payments"},
        ),
        await api.put("/v1/rbac/roles/custom", token, {"permissions": ["run.trigger"]}),
        await api.post(
            "/v1/killswitch",
            token,
            {"scope_kind": "team", "scope_value": "payments", "reason": "incident"},
        ),
        await _create(api, token, {**BODY, "name": "another"}),
    ]
    assert [r.status_code for r in refused] == [403, 403, 403, 403]


async def test_a_service_account_is_never_given_another_role(api):
    made = (await _create(api, api.a.admin_token)).json()
    reply = await api.post(
        "/v1/rbac/assignments",
        api.a.admin_token,
        {"email": made["email"], "role": "team-lead", "team": "payments"},
    )
    assert reply.status_code == 422 and reply.json()["code"] == "rbac.service_account_role"


async def test_a_person_is_never_given_the_service_account_role(api):
    reply = await api.post(
        "/v1/rbac/assignments",
        api.a.admin_token,
        {"email": "dev@a.example.test", "role": "service-account", "team": "payments"},
    )
    assert reply.status_code == 422 and reply.json()["code"] == "rbac.service_account_role"


async def test_the_service_account_role_cannot_be_widened(api):
    reply = await api.put(
        "/v1/rbac/roles/service-account",
        api.a.admin_token,
        {"permissions": ["run.trigger", "gate.approve"]},
    )
    assert reply.status_code == 409 and reply.json()["code"] == "rbac.service_account_role"


async def test_a_hand_made_grant_still_gives_a_service_account_nothing_more(api):
    """Defence in depth: whatever the tables say, a service principal holds only
    `run.trigger`."""
    made = (await _create(api, api.a.admin_token)).json()
    async with tenant_tx(api.db.owner, api.a.id) as conn:
        user_id = (
            await conn.execute(select(users.c.id).where(users.c.email == made["email"]))
        ).scalar_one()
        await conn.execute(
            assignments.insert().values(
                tenant_id=api.a.id,
                user_id=user_id,
                role="org-admin",
                scope_kind="org",
                scope_value="",
                created_by="system:test",
            )
        )
    token = made["token"]
    assert not (await check(api, token, "gate.approve", team="payments"))["allowed"]
    assert (await check(api, token, "run.trigger", team="payments"))["allowed"]
    assert (
        await api.post(
            "/v1/rbac/assignments",
            token,
            {"email": "x@a.example.test", "role": "developer", "team": "payments"},
        )
    ).status_code == 403
    permissions = (await api.get("/v1/me", token)).json()["permissions"]
    assert permissions["org"] == ["run.trigger"]
    # and asked about it by an admin, the answer is the same
    asked = await check(api, api.a.admin_token, "gate.approve", team="payments", user=made["email"])
    assert not asked["allowed"]


async def test_a_service_account_of_one_tenant_is_unknown_in_another(api):
    made = (await _create(api, api.a.admin_token)).json()
    listed = await api.get("/v1/service-accounts", api.b.admin_token)
    assert listed.status_code == 200 and listed.json() == []
    asked = await api.post(
        "/v1/rbac/check",
        api.b.admin_token,
        {"permission": "run.trigger", "team": "payments", "user": made["email"]},
    )
    assert asked.status_code == 404
