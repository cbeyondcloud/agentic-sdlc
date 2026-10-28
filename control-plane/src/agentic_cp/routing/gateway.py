"""The gateway routing port (M2-09): read and replace TokenLean's routing overlay.

`TokenLeanRouting` speaks TokenLean's `/admin/routing` API (ST-10, TokenLean PR #57):
`GET /admin/routing` -> `{version, fragment, ...}` (`version` 0 and `fragment` {} before
the first), `GET /admin/routing/versions/{v}` -> that version (404: none),
`PUT /admin/routing {fragment, expected_version, comment}` -> 200 the new version, 409 when
`expected_version` is not the current one, 422 `{error: invalid_fragment, errors: [{path,
message}]}`. The bearer is the deployment's gateway admin key, which must carry no team
binding (a team-bound key is 403: the overlay is one document for every tenant). The key is
never in a repr, a log line or an error message.

`NoGatewayRouting` stands in when the deployment names no gateway admin (ruling P9)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

import httpx2

from agentic_cp.keys.gateway import GatewayUnavailable
from agentic_cp.settings import Settings

PATH = "/admin/routing"
#: Problems kept from a 422, each cut short (they quote the fragment's values).
MAX_PROBLEMS, MAX_PROBLEM_CHARS = 50, 300


@dataclass(frozen=True)
class RoutingState:
    """One version of the gateway's overlay."""

    version: int
    fragment: dict


class RoutingConflict(Exception):
    """The overlay moved on since it was read (409): read it again and redo the change."""

    def __init__(self, current_version: int | None) -> None:
        super().__init__(f"the gateway's routing overlay is now at version {current_version}")
        self.current_version = current_version


class RoutingRefused(Exception):
    """The gateway refused the fragment (422); `errors` are its `path: message` items."""

    def __init__(self, errors: list[str]) -> None:
        shown = "; ".join(errors[:3]) or "no reason given"
        super().__init__(f"the gateway refused the routing fragment: {shown}")
        self.errors = errors


class GatewayRouting(Protocol):
    configured: bool

    async def read(self) -> RoutingState: ...

    async def read_version(self, version: int) -> RoutingState | None: ...

    async def write(
        self, fragment: dict, *, expected_version: int, comment: str
    ) -> RoutingState: ...


class NoGatewayRouting:
    """No gateway admin configured: every call is refused, nothing is sent."""

    configured = False

    def _refuse(self) -> GatewayUnavailable:
        return GatewayUnavailable(
            "no gateway admin is configured (AGENTIC_CP_GATEWAY_ADMIN_URL and _KEY)"
        )

    async def read(self) -> RoutingState:
        raise self._refuse()

    async def read_version(self, version: int) -> RoutingState | None:
        raise self._refuse()

    async def write(self, fragment: dict, *, expected_version: int, comment: str) -> RoutingState:
        raise self._refuse()


class TokenLeanRouting:
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
        return f"TokenLeanRouting({self.admin_url!r})"

    async def read(self) -> RoutingState:
        return _state(await self._call("GET", PATH), "GET")

    async def read_version(self, version: int) -> RoutingState | None:
        found = await self._call("GET", f"{PATH}/versions/{int(version)}", missing_ok=True)
        return None if found is None else _state(found, "GET")

    async def write(self, fragment: dict, *, expected_version: int, comment: str) -> RoutingState:
        body = {"fragment": fragment, "expected_version": expected_version, "comment": comment}
        return _state(await self._call("PUT", PATH, body), "PUT")

    async def _call(
        self, method: str, path: str, body: dict | None = None, *, missing_ok: bool = False
    ) -> Any:
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
        status = response.status_code
        if status == 404 and missing_ok:
            return None
        if status == 409:
            raise RoutingConflict(_detail(response).get("current_version"))
        if status == 422:
            raise RoutingRefused(_problems(_detail(response)))
        if status == 403:
            raise GatewayUnavailable(
                f"the gateway refused the admin key for {method} {path} (403): routing needs "
                "an admin key with no team binding"
            )
        if status != 200:
            raise GatewayUnavailable(f"the gateway's routing API answered {status} to {method}")
        try:
            return response.json()
        except ValueError:
            raise GatewayUnavailable(
                f"the gateway's routing API answered {method} {path} without JSON"
            ) from None


def _detail(response: httpx2.Response) -> dict:
    try:
        found = response.json()
    except ValueError:
        return {}
    detail = found.get("detail", found) if isinstance(found, dict) else {}
    return detail if isinstance(detail, dict) else {}


def _problems(detail: dict) -> list[str]:
    problems = []
    for item in detail.get("errors") or []:
        if isinstance(item, dict):
            text = f"{item.get('path', '?')}: {item.get('message', '')}"
            problems.append(text[:MAX_PROBLEM_CHARS])
    return problems[:MAX_PROBLEMS]


def _state(found: Any, method: str) -> RoutingState:
    version = found.get("version") if isinstance(found, dict) else None
    fragment = found.get("fragment") if isinstance(found, dict) else None
    if not isinstance(version, int) or isinstance(version, bool) or version < 0:
        raise GatewayUnavailable(f"the gateway's routing API answered {method} without a version")
    if not isinstance(fragment, dict):
        raise GatewayUnavailable(f"the gateway's routing API answered {method} without a fragment")
    return RoutingState(version, fragment)


def gateway_routing_for(settings: Settings) -> GatewayRouting:
    """The deployment's gateway routing API, or `NoGatewayRouting`."""
    if settings.gateway_admin_url and settings.gateway_admin_key is not None:
        return TokenLeanRouting(
            settings.gateway_admin_url, settings.gateway_admin_key.get_secret_value()
        )
    return NoGatewayRouting()
