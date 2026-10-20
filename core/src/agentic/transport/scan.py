"""The secret scan under every model-call client (M1-41).

A transport between the identity client and whatever sends the request (the
metering transport, the network): the body is read, scanned, and then either
sent unchanged, sent masked (with its length corrected), or refused with
`SecretBlocked` - before metering, so a blocked call reserves nothing and
nothing leaves the machine. `make_client` and `make_async_client` always put
one in, so a client built without a scanner still scans (block, default
detectors).
"""

import httpx2

from agentic.security import SecretScanner

WHERE = "model-call"  # the `where` of a model call's scan event


def _scanned(request: httpx2.Request, scanner: SecretScanner) -> httpx2.Request:
    content = request.content
    checked = scanner.check_body(content, WHERE)
    if checked is content:
        return request
    headers = httpx2.Headers(request.headers)
    headers.pop("transfer-encoding", None)
    headers["content-length"] = str(len(checked))
    return httpx2.Request(
        request.method, request.url, headers=headers, content=checked, extensions=request.extensions
    )


class ScanningTransport(httpx2.AsyncBaseTransport):
    def __init__(self, inner: httpx2.AsyncBaseTransport, scanner: SecretScanner) -> None:
        self.inner, self.scanner = inner, scanner

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        await request.aread()
        return await self.inner.handle_async_request(_scanned(request, self.scanner))

    async def aclose(self) -> None:
        await self.inner.aclose()


class SyncScanningTransport(httpx2.BaseTransport):
    def __init__(self, inner: httpx2.BaseTransport, scanner: SecretScanner) -> None:
        self.inner, self.scanner = inner, scanner

    def handle_request(self, request: httpx2.Request) -> httpx2.Response:
        request.read()
        return self.inner.handle_request(_scanned(request, self.scanner))

    def close(self) -> None:
        self.inner.close()
