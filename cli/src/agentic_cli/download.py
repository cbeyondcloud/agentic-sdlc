"""Downloading a tool artefact from the org mirror (M1-51), for `sdlc_commons.tool_fetch`.

httpx2 with the OS certificate store and the proxy variables (`agentic.transport.network`:
corporate networks re-sign TLS). Every hop must stay on https - a redirect to plain http
is refused - and every failure is an OSError, which the fetcher reports as "could not
reach the mirror" rather than a crash. The bytes are verified by the fetcher, not here.
"""

import httpx2

from agentic.transport.network import NetworkSettings, ssl_context

MAX_REDIRECTS = 5
TIMEOUT_S = 120.0


def download(url: str, *, transport: httpx2.BaseTransport | None = None) -> bytes:
    options: dict = {"timeout": TIMEOUT_S, "follow_redirects": False, "trust_env": True}
    if transport is not None:
        options["transport"] = transport
    else:
        options["verify"] = ssl_context(NetworkSettings())
    try:
        with httpx2.Client(**options) as client:
            for _ in range(MAX_REDIRECTS + 1):
                if httpx2.URL(url).scheme != "https":
                    raise OSError(f"the mirror sent the download off https ({url})")
                response = client.get(url)
                if response.is_redirect:
                    url = str(response.url.join(response.headers["location"]))
                    continue
                if response.status_code != 200:
                    raise OSError(f"the mirror answered {response.status_code} for {url}")
                return response.content
    except httpx2.HTTPError as error:
        raise OSError(f"{type(error).__name__}: {error}") from error
    raise OSError(f"more than {MAX_REDIRECTS} redirects for {url}")
