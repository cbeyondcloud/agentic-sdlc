"""The request body cap (`max_body_bytes`, DF-420), for every route.

A declared `Content-Length` over the cap is answered 413 before the app sees the request;
a body without one (chunked) is counted as it streams, and the read that crosses the cap
raises a 413 the problem handlers render. A reverse proxy limit is still recommended in
front of the service; this one holds without it."""

from __future__ import annotations

from starlette.exceptions import HTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from agentic_cp.errors import problem_response


class BodyLimit:
    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        declared = dict(scope.get("headers") or []).get(b"content-length")
        if declared is not None and declared.isdigit() and int(declared) > self.max_bytes:
            response = problem_response(413, "too_large", self._detail())
            await response(scope, receive, send)
            return
        seen = 0

        async def counted() -> Message:
            nonlocal seen
            message = await receive()
            if message["type"] == "http.request":
                seen += len(message.get("body", b""))
                if seen > self.max_bytes:
                    raise HTTPException(413, self._detail())
            return message

        await self.app(scope, counted, send)

    def _detail(self) -> str:
        return f"the request body is larger than {self.max_bytes} bytes"
