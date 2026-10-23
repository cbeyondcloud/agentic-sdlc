"""The gateway key port (M2-17): create, revoke and rotate a person's proxy key.

`TokenLeanKeys` speaks TokenLean's `/admin/keys` API (ST-9, branch
feat/admin-keys-and-routing): `POST /admin/keys {tenant_id, team?, user?, label?}` -> 201
`{key, key_id, ...}`, `DELETE /admin/keys/{key_id}`, `POST /admin/keys/{key_id}/rotate` ->
`{key, key_id, replaced_key_id}`, an admin key as the bearer. A key is named by its
`key_id` (the first 16 hex characters of its sha256); the raw key is in a create or rotate
answer only, and nothing here stores or logs it. Revoking a key the gateway no longer has
(404) counts as revoked. Every other failure - the gateway unreachable, a 5xx, a refused
admin key - is `GatewayUnavailable`, whose message never holds a key.

`NoGatewayKeys` stands in when the deployment names no gateway admin (ruling P9)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx2

from agentic_cp.settings import Settings


class GatewayUnavailable(Exception):
    """The gateway's key API could not do what was asked; nothing changed there."""


@dataclass(frozen=True)
class IssuedKey:
    key: str = field(repr=False)
    key_id: str


class GatewayKeys(Protocol):
    configured: bool

    async def create(
        self, *, tenant: str, team: str | None, user: str, label: str
    ) -> IssuedKey: ...

    async def revoke(self, key_id: str) -> None: ...

    async def rotate(self, key_id: str) -> IssuedKey: ...


class NoGatewayKeys:
    """No gateway admin configured: every call is refused, nothing is sent."""

    configured = False

    def _refuse(self) -> GatewayUnavailable:
        return GatewayUnavailable(
            "no gateway admin is configured (AGENTIC_CP_GATEWAY_ADMIN_URL and _KEY)"
        )

    async def create(self, *, tenant: str, team: str | None, user: str, label: str) -> IssuedKey:
        raise self._refuse()

    async def revoke(self, key_id: str) -> None:
        raise self._refuse()

    async def rotate(self, key_id: str) -> IssuedKey:
        raise self._refuse()


class TokenLeanKeys:
    configured = True

    def __init__(
        self,
        admin_url: str,
        admin_key: str,
        *,
        transport: httpx2.AsyncBaseTransport | None = None,
        timeout: float = 15.0,
    ) -> None:
        self.admin_url = admin_url.rstrip("/")
        self._admin_key = admin_key
        self._transport = transport
        self._timeout = timeout

    def __repr__(self) -> str:  # never the admin key
        return f"TokenLeanKeys({self.admin_url!r})"

    async def create(self, *, tenant: str, team: str | None, user: str, label: str) -> IssuedKey:
        body: dict[str, Any] = {"tenant_id": tenant, "user": user, "label": label[:128]}
        if team:
            body["team"] = team
        return _issued(await self._call("POST", "/admin/keys", body, expect=201))

    async def revoke(self, key_id: str) -> None:
        await self._call("DELETE", f"/admin/keys/{key_id}", gone_ok=True)

    async def rotate(self, key_id: str) -> IssuedKey:
        return _issued(await self._call("POST", f"/admin/keys/{key_id}/rotate"))

    async def _call(
        self,
        method: str,
        path: str,
        body: dict | None = None,
        *,
        expect: int = 200,
        gone_ok: bool = False,
    ) -> dict:
        options: dict[str, Any] = {
            "base_url": self.admin_url,
            "timeout": self._timeout,
            "follow_redirects": False,
            "headers": {"Authorization": f"Bearer {self._admin_key}"},
        }
        if self._transport is not None:
            options["transport"] = self._transport
        try:
            async with httpx2.AsyncClient(**options) as client:
                response = await client.request(method, path, json=body)
        except httpx2.HTTPError as error:
            raise GatewayUnavailable(
                f"the gateway at {self.admin_url} could not be reached ({type(error).__name__})"
            ) from None
        if gone_ok and response.status_code == 404:
            return {}
        if response.status_code != expect:
            raise GatewayUnavailable(
                f"the gateway's key API answered {response.status_code} to {method} {path}"
            )
        try:
            found = response.json()
        except ValueError:
            found = None
        if not isinstance(found, dict):
            raise GatewayUnavailable(f"the gateway's key API answered {method} {path} without JSON")
        return found


def _issued(found: dict) -> IssuedKey:
    key, key_id = found.get("key"), found.get("key_id")
    if not isinstance(key, str) or not key or not isinstance(key_id, str) or not key_id:
        raise GatewayUnavailable("the gateway's key API answered without a key and key_id")
    return IssuedKey(key=key, key_id=key_id.lower())


def gateway_keys_for(settings: Settings) -> GatewayKeys:
    """The deployment's gateway key API, or `NoGatewayKeys`."""
    if settings.gateway_admin_url and settings.gateway_admin_key is not None:
        return TokenLeanKeys(
            settings.gateway_admin_url, settings.gateway_admin_key.get_secret_value()
        )
    return NoGatewayKeys()
