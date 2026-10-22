"""An API test harness: two bootstrapped tenants, a controllable clock and an httpx client
on the app (same event loop as the test, so asyncpg connections stay on one loop)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import httpx

from agentic.orgapi import CLI_VERSION_HEADER
from agentic_cp.app import create_app
from agentic_cp.auth.bootstrap import bootstrap_tenant
from agentic_cp.auth.tokens import issue_token
from agentic_cp.auth.users import create_user
from agentic_cp.settings import Settings
from agentic_cp.tenancy.context import tenant_tx


@dataclass
class FakeClock:
    now: datetime = field(default_factory=lambda: datetime.now(UTC).replace(microsecond=0))

    def __call__(self) -> datetime:
        return self.now

    def advance(self, delta: timedelta) -> None:
        self.now += delta


@dataclass
class TenantHandle:
    id: UUID
    slug: str
    admin_id: UUID
    admin_token: str
    admin_token_id: UUID


class Client:
    """Calls the app with a bearer token (and the CLI's headers)."""

    def __init__(self, app) -> None:
        self.app = app
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        self.http = httpx.AsyncClient(transport=transport, base_url="http://cp.test")

    async def call(
        self,
        method: str,
        path: str,
        token: str | None = None,
        body: Any = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        sent = {"User-Agent": "agentic-cli/test", CLI_VERSION_HEADER: "0.0.0-test"}
        if token is not None:
            sent["Authorization"] = f"Bearer {token}"
        sent.update(headers or {})
        return await self.http.request(method, path, json=body, headers=sent)

    async def get(self, path, token=None, headers=None):
        return await self.call("GET", path, token, headers=headers)

    async def post(self, path, token=None, body=None, headers=None):
        return await self.call("POST", path, token, body, headers)

    async def put(self, path, token=None, body=None, headers=None):
        return await self.call("PUT", path, token, body, headers)

    async def delete(self, path, token=None, headers=None):
        return await self.call("DELETE", path, token, headers=headers)


class Api(Client):
    """The app on a test database with tenants `a` and `b`, each with an org admin."""

    def __init__(self, db, clock: FakeClock, a: TenantHandle, b: TenantHandle, **hooks) -> None:
        self.db, self.clock, self.a, self.b = db, clock, a, b
        self.settings = settings_for(db)
        super().__init__(create_app(self.settings, engine=db.app, clock=clock, **hooks))
        self.client = self

    def with_app(self, **hooks) -> Api:
        """The same database and tenants behind a second app (another replica)."""
        return Api(self.db, self.clock, self.a, self.b, **hooks)

    async def add_user(
        self, tenant: TenantHandle, email: str, *, kind: str = "human"
    ) -> tuple[UUID, str]:
        """A user in `tenant` with a fresh token (owner role, inside the tenant)."""
        async with tenant_tx(self.db.owner, tenant.id) as conn:
            user = await create_user(conn, email, kind=kind)
            _, value = await issue_token(
                conn,
                user_id=user.id,
                name="test",
                expires_at=self.clock.now + timedelta(days=30),
                created_by="system:test",
            )
        return user.id, value


def settings_for(db) -> Settings:
    return Settings.from_env({"AGENTIC_CP_DATABASE_URL": db.app_dsn})


async def bootstrap(db, clock: FakeClock, slug: str, name: str) -> TenantHandle:
    result = await bootstrap_tenant(
        db.owner,
        settings_for(db),
        slug=slug,
        name=name,
        admin_email=f"admin@{slug[-1]}.example.test",
        clock=clock,
    )
    return TenantHandle(
        id=result.tenant.id,
        slug=slug,
        admin_id=result.admin_id,
        admin_token=result.token,
        admin_token_id=result.token_id,
    )


# -- RBAC helpers (tests of the RBAC and audit APIs) --


async def assign(api, token, email, role, team=None):
    body = {"email": email, "role": role}
    if team is not None:
        body["team"] = team
    return await api.post("/v1/rbac/assignments", token, body)


async def member(api, tenant, email, role, team=None):
    """A user of `tenant` holding `role` (assigned by its org admin), with a token."""
    _, token = await api.add_user(tenant, email)
    reply = await assign(api, tenant.admin_token, email, role, team)
    assert reply.status_code == 201, reply.text
    return token, reply.json()["id"]


async def check(api, token, permission, **where):
    reply = await api.post("/v1/rbac/check", token, {"permission": permission, **where})
    assert reply.status_code == 200, reply.text
    return reply.json()
