"""The harness's async client sends exactly what the CLI's `ControlPlaneClient` sends (M3-22,
Review Focus 1): if the CLI's protocol changes, this test fails before the load test lies."""

import asyncio
import hashlib
import json
from datetime import UTC, datetime

import httpx2
import pytest
from cp_loadtest.stats import Recorder
from cp_loadtest.wire import Refused, Wire, make_http

from agentic.orgapi import CLI_VERSION_HEADER
from agentic_cli.control_plane import ControlPlaneClient, ControlPlaneSink

BASE = "http://127.0.0.1:8280"
TOKEN = "agt_" + "x" * 8 + "_load"  # gitleaks:allow - a made-up token
BUNDLE = b"policy bundle bytes"
DIGEST = "sha256:" + hashlib.sha256(BUNDLE).hexdigest()
ACTIVE = {
    "version": "git:" + "a" * 40,
    "branch": "main",
    "digest": DIGEST,
    "tree_digest": "sha256:" + "b" * 64,
    "activated_at": "2026-10-29T09:00:00+00:00",
    "bundle_path": "/v1/policy/versions/git:" + "a" * 40 + "/bundle",
}
KILL = {"switches": [], "checked_at": "2026-10-29T09:00:00+00:00", "ttl_seconds": 10}
ME = {
    "tenant": {"slug": "lt-1", "name": "LT"},
    "user": {"id": "00000000-0000-0000-0000-000000000002", "email": "u@lt.test"},
    "kind": "human",
    "token_expires_at": "2026-12-01T00:00:00+00:00",
}
HEADERS = ("authorization", "user-agent", CLI_VERSION_HEADER.lower(), "accept", "content-type")


class Server:
    """Records every request and answers like the control plane would."""

    def __init__(self) -> None:
        self.seen: list[tuple] = []
        self.too_many = 0

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        headers = {k: request.headers.get(k) for k in (*HEADERS, "if-none-match")}
        self.seen.append((request.method, str(request.url), headers, request.content))
        path = request.url.path
        if self.too_many:
            self.too_many -= 1
            return httpx2.Response(429, headers={"Retry-After": "0"}, json={"code": "rate"})
        if path == "/v1/me":
            return httpx2.Response(200, json=ME)
        if path == "/v1/policy/active":
            return httpx2.Response(200, json=ACTIVE)
        if path.endswith("/bundle"):
            return httpx2.Response(200, content=BUNDLE)
        if path == "/v1/runs":
            return httpx2.Response(200, json={"registered": True, "checks": []})
        if path.endswith("/heartbeat"):
            return httpx2.Response(200, json={"status": "open", "counted": True})
        if path == "/v1/killswitch/state":
            if request.headers.get("if-none-match") == '"t1"':
                return httpx2.Response(304, headers={"ETag": '"t1"'})
            return httpx2.Response(200, json=KILL, headers={"ETag": '"t1"'})
        if path.endswith("/events"):
            entries = json.loads(request.content)["entries"]
            if entries[0]["seq"] == 9:
                head = {"seq": 3, "hash": "sha256:" + "c" * 64}
                return httpx2.Response(
                    409, json={"code": "ingest.gap", "detail": "gap", "head": head}
                )
            head = {"seq": entries[-1]["seq"], "hash": entries[-1]["hash"]}
            return httpx2.Response(
                200, json={"accepted": len(entries), "duplicates": 0, "head": head}
            )
        return httpx2.Response(404, json={"code": "not_found"})


REGISTRATION = {
    "run_id": "lt-0001",
    "team": "team-01",
    "repo": "git.example.test/org/app",
    "pipeline": "feature-delivery",
    "pipeline_file": "pipelines/feature-delivery.yaml",
    "pipeline_sha256": "sha256:" + "d" * 64,
    "pipeline_version": "1.0.0",
    "overlay_file": None,
    "overlay_sha256": None,
    "policy_version": "git:" + "a" * 40,
    "agents": ["developer@1.0.0"],
    "models": ["acme/model-b"],
    "backends": ["native"],
    "dry_run": False,
}
BEAT_AT = datetime(2026, 10, 29, 9, 0, 1, tzinfo=UTC)
SENT_AT = datetime(2026, 10, 29, 9, 0, 2, tzinfo=UTC)
ENTRIES = [{"seq": 1, "hash": "sha256:" + "e" * 64, "event": "run_opened"}]


def _cli_requests() -> list[tuple]:
    server = Server()
    with ControlPlaneClient(BASE, TOKEN, transport=httpx2.MockTransport(server)) as client:
        client.me()
        active = client.active_policy()
        client.bundle(active)
        client.register_run(REGISTRATION)
        client.heartbeat("lt-0001", beat_at=BEAT_AT, sent_at=SENT_AT, state="running")
        client.heartbeat("lt-0001", beat_at=BEAT_AT, sent_at=SENT_AT, state="waiting")
        first = client.kill_state_reply(
            team="team-01", repo="git.example.test/org/app", pipeline="feature-delivery"
        )
        client.kill_state_reply(
            team="team-01",
            repo="git.example.test/org/app",
            pipeline="feature-delivery",
            etag=first.etag,
        )
        ControlPlaneSink(client).deliver("lt-0001", ENTRIES)
    return server.seen


async def _wire_requests() -> list[tuple]:
    server = Server()
    recorder = Recorder()
    async with make_http(BASE, transport=httpx2.MockTransport(server)) as http:
        wire = Wire(http, TOKEN, recorder)
        await wire.me()
        active = await wire.active_policy()
        await wire.bundle(active)
        await wire.register(REGISTRATION)
        await wire.heartbeat("lt-0001", beat_at=BEAT_AT, sent_at=SENT_AT, state="running")
        await wire.heartbeat("lt-0001", beat_at=BEAT_AT, sent_at=SENT_AT, state="waiting")
        state, etag = await wire.kill_state(
            team="team-01", repo="git.example.test/org/app", pipeline="feature-delivery"
        )
        assert state is not None and etag == '"t1"'
        again, _ = await wire.kill_state(
            team="team-01", repo="git.example.test/org/app", pipeline="feature-delivery", etag=etag
        )
        assert again is None  # a 304
        await wire.ingest("lt-0001", ENTRIES)
    routes = {route for route, _ in recorder.statuses.items()}
    assert "POST /v1/runs/{run_id}/events" in routes
    return server.seen


def test_wire_requests_equal_the_cli_client():
    assert asyncio.run(_wire_requests()) == _cli_requests()


async def _retried() -> tuple[Server, Recorder]:
    server = Server()
    server.too_many = 2
    recorder = Recorder()
    async with make_http(BASE, transport=httpx2.MockTransport(server)) as http:
        await Wire(http, TOKEN, recorder).heartbeat(
            "lt-0001", beat_at=BEAT_AT, sent_at=SENT_AT, state="running"
        )
    return server, recorder


def test_wire_retries_a_short_429_like_the_cli():
    server, recorder = asyncio.run(_retried())
    assert len(server.seen) == 3  # RETRIES_429 = 2 retries after the first try
    assert recorder.statuses["POST /v1/runs/{run_id}/heartbeat"] == {429: 2, 200: 1}


async def _gap() -> Refused:
    server = Server()
    async with make_http(BASE, transport=httpx2.MockTransport(server)) as http:
        wire = Wire(http, TOKEN, Recorder())
        with pytest.raises(Refused) as caught:
            await wire.ingest("lt-0001", [{"seq": 9, "hash": "h", "event": "step_started"}])
    return caught.value


def test_wire_refusal_carries_the_servers_head():
    refused = asyncio.run(_gap())
    assert refused.status == 409 and refused.code == "ingest.gap"
    assert refused.head == (3, "sha256:" + "c" * 64)
