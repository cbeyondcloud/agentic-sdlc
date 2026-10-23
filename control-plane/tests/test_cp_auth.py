"""Tenant-bound token authentication (M2-01, owner row 75): the tenant comes only from the
token; bad tokens all look alike; tokens are stored hashed and never logged."""

import logging
from datetime import timedelta

import pytest
from sqlalchemy import func, select, text

from agentic.orgapi import TOKEN_PREFIX
from agentic_cp.audit import read_records
from agentic_cp.auth.tokens import token_digest
from agentic_cp.tables import tokens, users
from agentic_cp.tenancy.context import tenant_tx
from agentic_cp.tenancy.ratelimit import RateDecision

pytestmark = pytest.mark.anyio


async def test_missing_token_is_401_problem(api):
    reply = await api.client.get("/v1/me")
    assert reply.status_code == 401
    assert reply.headers["www-authenticate"].startswith("Bearer")
    assert reply.headers["content-type"].startswith("application/problem+json")
    assert reply.json()["code"] == "auth.missing_token"


async def test_unknown_malformed_and_revoked_tokens_look_the_same(api):
    _, revoked = await api.add_user(api.a, "rita@example.test")
    async with tenant_tx(api.db.owner, api.a.id) as conn:
        await conn.execute(tokens.update().values(revoked_at=func.now()))
    _, suspended = await api.add_user(api.b, "sam@example.test")
    async with api.db.owner.begin() as conn:
        await conn.exec_driver_sql(
            f"UPDATE tenants SET status = 'suspended' WHERE id = '{api.b.id}'"
        )
    candidates = [
        TOKEN_PREFIX + "x" * 43,  # well formed, unknown
        "not-a-token",
        TOKEN_PREFIX + "short",
        revoked,
        suspended,
    ]
    replies = [await api.get("/v1/me", token) for token in candidates]
    bodies = {(r.status_code, r.json()["code"], r.json()["detail"]) for r in replies}
    assert len(bodies) == 1, bodies
    assert next(iter(bodies))[:2] == (401, "auth.invalid_token")
    assert all(r.headers["www-authenticate"].startswith("Bearer") for r in replies)


async def test_expired_token_is_rejected_with_login_hint(api):
    api.clock.advance(timedelta(days=91))
    reply = await api.get("/v1/me", api.a.admin_token)
    assert reply.status_code == 401
    body = reply.json()
    assert body["code"] == "auth.token_expired"
    assert "agentic login --for control-plane" in body["hint"]


async def test_user_and_service_token_lifetimes(api):
    issued = await api.post("/v1/tokens", api.a.admin_token, {"name": "laptop"})
    assert issued.status_code == 201
    expires = issued.json()["expires_at"]
    assert expires.startswith((api.clock.now + timedelta(days=90)).date().isoformat())
    too_long = await api.post("/v1/tokens", api.a.admin_token, {"name": "x", "days": 91})
    assert too_long.status_code == 422
    assert too_long.json()["code"] == "auth.lifetime_too_long"
    _, service = await api.add_user(api.a, "ci-bot@example.test", kind="service")
    # a service token (30 days here) never mints one that outlives it
    same = await api.post("/v1/tokens", service, {"name": "ci", "days": 30})
    assert same.status_code == 201
    year = await api.post("/v1/tokens", service, {"name": "ci", "days": 365})
    assert year.status_code == 422
    longer = await api.post("/v1/tokens", service, {"name": "ci", "days": 366})
    assert longer.status_code == 422
    assert longer.json()["code"] == "auth.lifetime_too_long"


async def test_disabled_user_tokens_are_rejected(api):
    user_id, token = await api.add_user(api.a, "dora@example.test")
    assert (await api.get("/v1/me", token)).status_code == 200
    async with tenant_tx(api.db.owner, api.a.id) as conn:
        await conn.execute(users.update().where(users.c.id == user_id).values(status="disabled"))
    reply = await api.get("/v1/me", token)
    assert reply.status_code == 401
    assert reply.json()["code"] == "auth.invalid_token"


async def test_me_returns_the_principal_and_tenant(api):
    reply = await api.get("/v1/me", api.a.admin_token)
    assert reply.status_code == 200
    me = reply.json()
    assert me["user"]["email"] == "admin@a.example.test"
    assert me["kind"] == "human"
    assert me["tenant"] == {"slug": "tenant-a", "name": "Tenant A"}
    assert me["token_expires_at"]


async def test_tenant_comes_only_from_the_token(api):
    headers = {"X-Tenant": "tenant-b", "X-Agentic-Tenant": str(api.b.id)}
    reply = await api.get(
        f"/v1/me?tenant=tenant-b&tenant_id={api.b.id}", api.a.admin_token, headers
    )
    assert reply.json()["tenant"]["slug"] == "tenant-a"
    smuggled = await api.post(
        "/v1/tokens", api.a.admin_token, {"name": "x", "tenant": "tenant-b"}, headers
    )
    assert smuggled.status_code == 422
    assert "tenant" in smuggled.json()["detail"]
    listed = await api.get("/v1/tokens", api.a.admin_token, headers)
    assert {t["id"] for t in listed.json()} == {str(api.a.admin_token_id)}


async def test_resolve_token_returns_at_most_one_row_and_nothing_else(api):
    async with api.db.app.begin() as conn:
        unknown = await conn.execute(
            text("select * from resolve_token(:h)"), {"h": token_digest(TOKEN_PREFIX + "nope")}
        )
        assert unknown.all() == []
        known = await conn.execute(
            text("select * from resolve_token(:h)"), {"h": token_digest(api.a.admin_token)}
        )
        rows = known.all()
        assert len(rows) == 1
        assert list(known.keys()) == [
            "tenant_id",
            "tenant_slug",
            "tenant_status",
            "rbac_revision",
            "token_id",
            "user_id",
            "email",
            "kind",
            "user_status",
            "expires_at",
            "revoked_at",
        ]


async def test_owner_sees_no_tokens_without_the_lookup_switch(api):
    async with api.db.owner.begin() as conn:
        for table in (tokens, users):
            assert (await conn.execute(select(func.count()).select_from(table))).scalar_one() == 0
    async with api.db.app.begin() as conn:
        await conn.execute(select(func.set_config("app.credential_lookup", "on", True)))
        assert (await conn.execute(select(func.count()).select_from(tokens))).scalar_one() == 0


async def test_user_can_rotate_own_token(api):
    _, old = await api.add_user(api.a, "rory@example.test")
    kept = await api.post("/v1/tokens", old, {"name": "second"})
    assert kept.status_code == 201
    assert (await api.get("/v1/me", old)).status_code == 200
    rotated = await api.post("/v1/tokens", old, {"name": "third", "revoke_current": True})
    assert rotated.status_code == 201
    new = rotated.json()["token"]
    assert (await api.get("/v1/me", new)).status_code == 200
    assert (await api.get("/v1/me", old)).status_code == 401


async def test_user_can_revoke_own_token_by_id(api):
    _, token = await api.add_user(api.a, "ruth@example.test")
    issued = (await api.post("/v1/tokens", token, {"name": "spare"})).json()
    reply = await api.delete(f"/v1/tokens/{issued['id']}", token)
    assert reply.status_code == 204
    assert (await api.get("/v1/me", issued["token"])).status_code == 401
    assert (await api.get("/v1/me", token)).status_code == 200


async def test_token_is_stored_hashed_and_never_logged(api, caplog):
    caplog.set_level(logging.DEBUG)
    issued = await api.post("/v1/tokens", api.a.admin_token, {"name": "secret-check"})
    value = issued.json()["token"]
    assert value.startswith(TOKEN_PREFIX)
    await api.get("/v1/me", value)
    async with tenant_tx(api.db.owner, api.a.id) as conn:
        for table in (tokens, users):
            for row in await conn.execute(select(table)):
                assert all(value not in str(cell) for cell in row)
        stored = (await conn.execute(select(tokens.c.token_sha256))).scalars().all()
        assert token_digest(value) in stored
    assert value not in caplog.text
    assert value not in repr(issued.json().keys())


async def test_token_events_are_audited(api):
    issued = (await api.post("/v1/tokens", api.a.admin_token, {"name": "audited"})).json()
    await api.delete(f"/v1/tokens/{issued['id']}", api.a.admin_token)
    async with tenant_tx(api.db.app, api.a.id) as conn:
        records = await read_records(conn)
    events = [r.event for r in records]
    assert "token.issued" in events and "token.revoked" in events
    for record in records:
        assert issued["token"] not in str(record)
    async with tenant_tx(api.db.app, api.b.id) as conn:
        assert all(r.target != f"token:{issued['id']}" for r in await read_records(conn))


class _RefuseTenant:
    def __init__(self, tenant_id):
        self.tenant_id = tenant_id

    async def check(self, tenant_id, principal_id, route):
        if tenant_id == self.tenant_id:
            return RateDecision(allowed=False, retry_after=7)
        return RateDecision(allowed=True)


async def test_rate_limiter_refusal_is_429_for_that_tenant_only(api):
    limited = api.with_app(rate_limiter=_RefuseTenant(api.a.id))
    refused = await limited.get("/v1/me", api.a.admin_token)
    assert refused.status_code == 429
    assert refused.json()["code"] == "rate.limited"
    assert refused.headers["retry-after"] == "7"
    assert (await limited.get("/v1/me", api.b.admin_token)).status_code == 200
