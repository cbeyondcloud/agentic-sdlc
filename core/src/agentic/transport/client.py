"""The HTTP client every model call goes through (M1-11).

Built on httpx2: the provider SDKs and Pydantic AI accept an `httpx2.Client` /
`httpx2.AsyncClient`, so a request hook covers every call whichever library
makes it. The hook, in order:

  1. refuses a request to any host but TokenLean's (scheme, host and port of
     `base_url`), so the API key and identity never leave for another server;
     redirects are not followed;
  2. refuses TokenLean's behaviour headers (they switch on caching, routing,
     memory or prompt changes - spec §14.2) unless the caller allows one on
     purpose, and any header in TokenLean's own `x-tokenlean-*` namespace;
     `_` and `-` are the same here, as front proxies often treat them;
  3. refuses an `X-Agentic-*` header that disagrees with the run's identity, so
     agent code cannot re-label its own calls;
  4. stamps the identity headers and a W3C `traceparent`: the active span's, or
     one whose trace id is derived from the run id - so all calls of a run join
     into one trace - with a fresh span id per call.

Under the hook, the body is secret-scanned (`agentic.transport.scan`, M1-41):
blocked or masked per policy before anything is metered or sent.

The hook guards what goes through the client; it is not a sandbox against code
that builds its own client. The OS sandbox and egress rules (M1-32, M2-42) are.
"""

import hashlib
import re
import secrets
from collections.abc import Iterable

import httpx2
from opentelemetry.propagate import inject

from agentic.security import SecretScanner
from agentic.transport.identity import CallIdentity
from agentic.transport.network import NetworkSettings, ssl_context
from agentic.transport.scan import ScanningTransport, SyncScanningTransport

#: Request headers TokenLean acts on (it caches, routes, adds memory or rewrites
#: the prompt), read from its source 2026-10-19. Never sent unless allowed.
BEHAVIOUR_HEADERS = frozenset({
    "x-step-name", "x-step-inputs-hash", "x-template-version", "x-workflow-id",
    "x-session-id", "x-complexity", "x-route-to", "x-no-cache", "x-compress-user",
    "x-prefix-profile", "x-template-id", "x-echo-prompt", "x-confidence-score", "x-user-id",
    "x-tenant-id", "x-rag-collection",
})  # fmt: skip
_TRACEPARENT = re.compile(r"00-(?!0{32})[0-9a-f]{32}-(?!0{16})[0-9a-f]{16}-[0-9a-f]{2}")


class ForbiddenHeaderError(ValueError):
    """A TokenLean behaviour header the caller did not allow."""


class IdentityHeaderError(ValueError):
    """An `X-Agentic-*` header that disagrees with the run's identity."""


class ForeignHostError(ValueError):
    """A request to a host other than TokenLean's."""


def make_client(
    base_url: str,
    identity: CallIdentity,
    *,
    api_key: str | None = None,
    network: NetworkSettings | None = None,
    allow: Iterable[str] = frozenset(),
    timeout: float = 600.0,
    scanner: SecretScanner | None = None,
    _transport: httpx2.BaseTransport | None = None,
) -> httpx2.Client:
    """A synchronous client for TokenLean at `base_url`, stamping `identity`; every
    body is secret-scanned first (`scanner`, default: block, default detectors)."""
    stamp = _stamper(base_url, identity, _allowed(allow))
    settings = network or NetworkSettings()
    tls = ssl_context(settings)
    inner = _transport or _SyncClientTransport(
        httpx2.Client(verify=tls, trust_env=settings.trust_env)
    )
    transport = SyncScanningTransport(inner, scanner or SecretScanner())
    return httpx2.Client(
        base_url=base_url,
        headers=_auth(api_key),
        timeout=timeout,
        event_hooks={"request": [stamp]},
        transport=transport,
        follow_redirects=False,
    )


def make_async_client(
    base_url: str,
    identity: CallIdentity,
    *,
    api_key: str | None = None,
    network: NetworkSettings | None = None,
    allow: Iterable[str] = frozenset(),
    timeout: float = 600.0,
    scanner: SecretScanner | None = None,
    _transport: httpx2.AsyncBaseTransport | None = None,
) -> httpx2.AsyncClient:
    """An asynchronous client for TokenLean at `base_url`, stamping `identity`; every
    body is secret-scanned first (`scanner`, default: block, default detectors)."""
    stamp = _stamper(base_url, identity, _allowed(allow))

    async def stamp_async(request: httpx2.Request) -> None:
        stamp(request)

    inner = _transport or make_network_transport(network)
    transport = ScanningTransport(inner, scanner or SecretScanner())
    return httpx2.AsyncClient(
        base_url=base_url,
        headers=_auth(api_key),
        timeout=timeout,
        event_hooks={"request": [stamp_async]},
        transport=transport,
        follow_redirects=False,
    )


class _SyncClientTransport(httpx2.BaseTransport):
    """`_ClientTransport` for the synchronous client."""

    def __init__(self, client: httpx2.Client) -> None:
        self.client = client

    def handle_request(self, request: httpx2.Request) -> httpx2.Response:
        return self.client.send(request, stream=True, follow_redirects=False)

    def close(self) -> None:
        self.client.close()


class _ClientTransport(httpx2.AsyncBaseTransport):
    """Sends through a plain client, so its proxy (HTTPS_PROXY / NO_PROXY) and CA
    settings apply. A client given `transport=` ignores both, which is how a
    wrapping transport (metering) would otherwise lose them."""

    def __init__(self, client: httpx2.AsyncClient) -> None:
        self.client = client

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        return await self.client.send(request, stream=True, follow_redirects=False)

    async def aclose(self) -> None:
        await self.client.aclose()


def make_network_transport(network: NetworkSettings | None = None) -> httpx2.AsyncBaseTransport:
    """The innermost transport for a wrapping transport: the corporate CA and the
    proxy environment variables, exactly as `make_async_client` applies them."""
    settings = network or NetworkSettings()
    return _ClientTransport(
        httpx2.AsyncClient(verify=ssl_context(settings), trust_env=settings.trust_env)
    )


def _allowed(allow: Iterable[str]) -> frozenset[str]:
    if isinstance(allow, str):
        raise ValueError("allow is a set of header names, not one string")
    names = frozenset(_normal(name) for name in allow)
    if unknown := sorted(names - BEHAVIOUR_HEADERS):
        raise ValueError(f"only TokenLean behaviour headers can be allowed, not {unknown}")
    return names


def _auth(api_key: str | None) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"} if api_key else {}


def _normal(name: str) -> str:
    return name.lower().replace("_", "-")


def _origin(url: httpx2.URL) -> tuple[str, str, int | None]:
    default = {"https": 443, "http": 80}.get(url.scheme)
    return url.scheme, url.host, url.port or default


def _stamper(base_url: str, identity: CallIdentity, allowed: frozenset[str]):
    expected = {_normal(name): value for name, value in identity.headers().items()}
    home = _origin(httpx2.URL(base_url))
    trace_id = hashlib.sha256(identity.run_id.encode()).hexdigest()[:32]

    def stamp(request: httpx2.Request) -> None:
        if _origin(request.url) != home:
            raise ForeignHostError(f"model calls go only to {home[0]}://{home[1]} - refused a "
                                   f"request to another host")  # fmt: skip
        for name in request.headers:
            normal = _normal(name)
            if normal.startswith("x-tokenlean-") or (
                normal in BEHAVIOUR_HEADERS and normal not in allowed
            ):
                why = "switches on TokenLean behaviour and was not allowed for this call"
                raise ForbiddenHeaderError(f"request header {normal!r} {why}")
            if normal.startswith("x-agentic-") and request.headers[name] != expected.get(normal):
                why = "disagrees with the run's identity - calls cannot be re-labelled"
                raise IdentityHeaderError(f"request header {normal!r} {why}")
        request.headers.update(identity.headers())
        if not _TRACEPARENT.fullmatch(request.headers.get("traceparent", "")):
            request.headers["traceparent"] = _traceparent(trace_id)

    return stamp


def _traceparent(trace_id: str) -> str:
    """The active span's W3C traceparent, or one in the run's trace with a fresh span."""
    carrier: dict[str, str] = {}
    inject(carrier)
    if _TRACEPARENT.fullmatch(carrier.get("traceparent", "")):
        return carrier["traceparent"]
    return f"00-{trace_id}-{secrets.token_hex(8)}-01"
