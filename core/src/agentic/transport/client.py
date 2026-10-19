"""The HTTP client every model call goes through (M1-11).

Built on httpx: the provider SDKs and Pydantic AI accept an `httpx.Client` /
`httpx.AsyncClient`, so stamping identity in a request hook covers every call
whichever library makes it. The hook, in order:

  1. refuses TokenLean's behaviour headers (they switch on caching, routing,
     memory or prompt changes - spec §14.2) unless the caller allows one on
     purpose;
  2. refuses an `X-Agentic-*` header that disagrees with the run's identity, so
     agent code cannot re-label its own calls;
  3. stamps the identity headers and a W3C `traceparent` (the active span's when
     there is one, otherwise a fresh one).
"""

import secrets
from collections.abc import Iterable

import httpx
from opentelemetry.propagate import inject

from agentic.transport.identity import CallIdentity

#: Request headers TokenLean acts on (it caches, routes, adds memory or rewrites
#: the prompt), read from its source 2026-10-19. Never sent unless allowed.
BEHAVIOUR_HEADERS = frozenset({
    "x-step-name", "x-step-inputs-hash", "x-template-version", "x-workflow-id",
    "x-session-id", "x-complexity", "x-route-to", "x-no-cache", "x-compress-user",
    "x-prefix-profile", "x-template-id", "x-echo-prompt", "x-confidence-score", "x-user-id",
    "x-tenant-id", "x-rag-collection",
})  # fmt: skip


class ForbiddenHeaderError(ValueError):
    """A TokenLean behaviour header the caller did not allow."""


class IdentityHeaderError(ValueError):
    """An `X-Agentic-*` header that disagrees with the run's identity."""


def make_client(
    base_url: str,
    identity: CallIdentity,
    *,
    api_key: str | None = None,
    network: object | None = None,
    allow: Iterable[str] = frozenset(),
    timeout: float = 600.0,
    _transport: httpx.BaseTransport | None = None,
) -> httpx.Client:
    """A synchronous client for TokenLean at `base_url`, stamping `identity`."""
    stamp = _stamper(identity, _allowed(allow))
    return httpx.Client(base_url=base_url, headers=_auth(api_key), timeout=timeout,
                        event_hooks={"request": [stamp]}, transport=_transport)  # fmt: skip


def make_async_client(
    base_url: str,
    identity: CallIdentity,
    *,
    api_key: str | None = None,
    network: object | None = None,
    allow: Iterable[str] = frozenset(),
    timeout: float = 600.0,
    _transport: httpx.AsyncBaseTransport | None = None,
) -> httpx.AsyncClient:
    """An asynchronous client for TokenLean at `base_url`, stamping `identity`."""
    stamp = _stamper(identity, _allowed(allow))

    async def stamp_async(request: httpx.Request) -> None:
        stamp(request)

    hooks = {"request": [stamp_async]}
    return httpx.AsyncClient(base_url=base_url, headers=_auth(api_key), timeout=timeout,
                             event_hooks=hooks, transport=_transport)  # fmt: skip


def _allowed(allow: Iterable[str]) -> frozenset[str]:
    if isinstance(allow, str):
        raise ValueError("allow is a set of header names, not one string")
    names = frozenset(name.lower() for name in allow)
    if unknown := sorted(names - BEHAVIOUR_HEADERS):
        raise ValueError(f"only TokenLean behaviour headers can be allowed, not {unknown}")
    return names


def _auth(api_key: str | None) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"} if api_key else {}


def _stamper(identity: CallIdentity, allowed: frozenset[str]):
    expected = {name.lower(): value for name, value in identity.headers().items()}

    def stamp(request: httpx.Request) -> None:
        for name in request.headers:
            lowered = name.lower()
            if lowered in BEHAVIOUR_HEADERS and lowered not in allowed:
                why = "switches on TokenLean behaviour and was not allowed for this call"
                raise ForbiddenHeaderError(f"request header {lowered!r} {why}")
            if lowered.startswith("x-agentic-") and request.headers[name] != expected.get(lowered):
                why = "disagrees with the run's identity - calls cannot be re-labelled"
                raise IdentityHeaderError(f"request header {lowered!r} {why}")
        request.headers.update(identity.headers())
        if "traceparent" not in request.headers:
            request.headers["traceparent"] = _traceparent()

    return stamp


def _traceparent() -> str:
    """The active span's W3C traceparent, or a fresh sampled one."""
    carrier: dict[str, str] = {}
    inject(carrier)
    if "traceparent" in carrier:
        return carrier["traceparent"]
    return f"00-{secrets.token_hex(16)}-{secrets.token_hex(8)}-01"
