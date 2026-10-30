"""The CLI's control-plane protocol on an async client, for many simulated runs at once (M3-22).

`agentic_cli.control_plane.ControlPlaneClient` is synchronous: one CLI drives one run. The
load test drives a thousand from one process, so `Wire` sends the same requests on an
`httpx2.AsyncClient`: the same paths, query, headers (`User-Agent`, the CLI version
header, `Accept`, the bearer token), JSON bodies, timeouts and 429 rules, taken from the
CLI module itself. `test_loadtest_wire.py` compares the two clients request by request.

Every attempt is recorded in a `Recorder` under its route template, with its status (or
`timeout` / `transport` when no answer came) and its seconds."""

from __future__ import annotations

import asyncio
import hashlib
import time
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

import httpx2

from agentic.orgapi import (
    API_PREFIX,
    CLI_VERSION_HEADER,
    ActivePolicy,
    HeartbeatResult,
    IngestResult,
    KillState,
    Me,
    RunPreflight,
)
from agentic_cli import __version__
from agentic_cli.control_plane import (
    HEARTBEAT_TIMEOUT,
    KILL_MAX_RETRY_AFTER_S,
    KILL_STATE_TIMEOUT,
    MAX_RETRY_AFTER_S,
    RETRIES_429,
    TIMEOUT,
    _retry_after,
)
from cp_loadtest.stats import Recorder

ME = "GET /v1/me"
ACTIVE = "GET /v1/policy/active"
BUNDLE = "GET /v1/policy/versions/{version}/bundle"
REGISTER = "POST /v1/runs"
HEARTBEAT = "POST /v1/runs/{run_id}/heartbeat"
KILL = "GET /v1/killswitch/state"
INGEST = "POST /v1/runs/{run_id}/events"


class WireError(Exception):
    def __init__(self, message: str, status: int | str) -> None:
        super().__init__(message)
        self.status = status


class Unavailable(WireError):
    """No answer (a transport error or timeout) or a 5xx."""


class Refused(WireError):
    """A 4xx: the problem's `code`, and the server's chain head on an ingest gap or fork."""

    def __init__(self, message: str, status: int, code: str, head: tuple[int, str] | None):
        super().__init__(message, status)
        self.code = code
        self.head = head


def make_http(
    base_url: str,
    *,
    transport: httpx2.AsyncBaseTransport | None = None,
    max_connections: int = 200,
) -> httpx2.AsyncClient:
    """One connection pool for many simulated CLIs: the CLI's headers but the token, which
    each `Wire` sends with its own requests."""
    options: dict[str, Any] = {
        "base_url": base_url.rstrip("/"),
        "headers": {
            "User-Agent": f"agentic-cli/{__version__}",
            CLI_VERSION_HEADER: __version__,
            "Accept": "application/json, application/problem+json",
        },
        "timeout": TIMEOUT,
        "follow_redirects": False,
        "limits": httpx2.Limits(
            max_connections=max_connections, max_keepalive_connections=max_connections
        ),
    }
    if transport is not None:
        options["transport"] = transport
    return httpx2.AsyncClient(**options)


class Wire:
    """One simulated CLI's calls (one user's token) on a shared pool."""

    def __init__(self, http: httpx2.AsyncClient, token: str, recorder: Recorder) -> None:
        self._http = http
        self._auth = {"Authorization": f"Bearer {token}"}
        self.recorder = recorder

    async def me(self) -> Me:
        return Me.model_validate((await self._send(ME, "GET", "/me")).json())

    async def active_policy(self) -> ActivePolicy:
        found = await self._send(ACTIVE, "GET", "/policy/active")
        return ActivePolicy.model_validate(found.json())

    async def bundle(self, active: ActivePolicy) -> bytes:
        """The bundle, refused unless its sha256 is `active.digest` (as the CLI does)."""
        response = await self._send(BUNDLE, "GET", active.bundle_path.removeprefix(API_PREFIX))
        data = response.content
        if "sha256:" + hashlib.sha256(data).hexdigest() != active.digest:
            raise Refused("the policy bundle does not match its digest", 200, "digest", None)
        return data

    async def register(self, body: Mapping[str, object]) -> RunPreflight:
        found = await self._send(REGISTER, "POST", "/runs", dict(body))
        return RunPreflight.model_validate(found.json())

    async def heartbeat(
        self, run_id: str, *, beat_at: datetime, sent_at: datetime, state: str = "running"
    ) -> HeartbeatResult:
        body = {"beat_at": beat_at.isoformat(), "sent_at": sent_at.isoformat(), "state": state}
        found = await self._send(
            HEARTBEAT, "POST", f"/runs/{run_id}/heartbeat", body, timeout=HEARTBEAT_TIMEOUT
        )
        return HeartbeatResult.model_validate(found.json())

    async def kill_state(
        self,
        *,
        team: str | None,
        repo: str | None,
        pipeline: str | None,
        etag: str | None = None,
    ) -> tuple[KillState | None, str | None]:
        """(state, ETag); state None when unchanged (a 304 to the `etag` sent)."""
        params = {"team": team, "repo": repo, "pipeline": pipeline}
        response = await self._send(
            KILL,
            "GET",
            "/killswitch/state",
            params={k: v for k, v in params.items() if v},
            timeout=KILL_STATE_TIMEOUT,
            headers={"If-None-Match": etag} if etag else None,
        )
        tag = response.headers.get("etag")
        if response.status_code == 304:
            return None, tag or etag
        return KillState.model_validate(response.json()), tag

    async def ingest(self, run_id: str, entries: Sequence[Mapping[str, object]]) -> IngestResult:
        found = await self._send(
            INGEST, "POST", f"/runs/{run_id}/events", {"entries": list(entries)}
        )
        return IngestResult.model_validate(found.json())

    async def _send(
        self,
        route: str,
        method: str,
        path: str,
        body: Any = None,
        *,
        params: Mapping[str, str] | None = None,
        timeout: httpx2.Timeout | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> httpx2.Response:
        """`ControlPlaneClient._send`, awaited: a short 429 wait is waited out."""
        options: dict[str, Any] = {"json": body, "headers": {**self._auth, **(headers or {})}}
        if params:
            options["params"] = dict(params)
        if timeout is not None:
            options["timeout"] = timeout
        kill_poll = timeout is KILL_STATE_TIMEOUT
        retries, longest = (1, KILL_MAX_RETRY_AFTER_S) if kill_poll else (RETRIES_429, None)
        for attempt in range(retries + 1):
            started = time.perf_counter()
            try:
                response = await self._http.request(method, f"{API_PREFIX}{path}", **options)
            except httpx2.HTTPError as error:
                kind = "timeout" if isinstance(error, httpx2.TimeoutException) else "transport"
                self.recorder.request(route, kind, time.perf_counter() - started)
                raise Unavailable(f"{route}: {type(error).__name__}", kind) from None
            self.recorder.request(route, response.status_code, time.perf_counter() - started)
            if response.status_code < 400:
                return response
            wait = _retry_after(response, longest or MAX_RETRY_AFTER_S)
            if response.status_code != 429 or wait is None or attempt == retries:
                break
            await asyncio.sleep(wait)
        raise _problem(route, response)


def _problem(route: str, response: httpx2.Response) -> WireError:
    try:
        problem = response.json()
    except ValueError:
        problem = {}
    if not isinstance(problem, dict):
        problem = {}
    status = response.status_code
    message = f"{route}: {status} {problem.get('code', '')}".strip()
    if status >= 500:
        return Unavailable(message, status)
    head = problem.get("head")
    return Refused(
        message,
        status,
        str(problem.get("code") or ""),
        (int(head.get("seq", 0)), str(head.get("hash", ""))) if isinstance(head, dict) else None,
    )
