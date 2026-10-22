"""The app factory: health without a database, RFC 9457 problems for every error, and an
OpenAPI document that matches the committed snapshot."""

import json
from pathlib import Path

import httpx
import pytest

from agentic.orgapi import RequestModel
from agentic_cp.app import create_app
from agentic_cp.settings import Settings

pytestmark = pytest.mark.anyio

SNAPSHOT = Path(__file__).resolve().parents[1] / "openapi.json"
# a DSN nothing listens on: any database access would fail
UNREACHABLE = {"AGENTIC_CP_DATABASE_URL": "postgresql+asyncpg://nobody@127.0.0.1:9/none"}


def _app():
    return create_app(Settings.from_env(UNREACHABLE))


def _client(app) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    return httpx.AsyncClient(transport=transport, base_url="http://cp.test")


async def test_healthz_is_ok_without_database():
    async with _client(_app()) as client:
        reply = await client.get("/healthz")
    assert reply.status_code == 200
    assert reply.json() == {"status": "ok"}


async def test_unknown_route_is_problem_json():
    async with _client(_app()) as client:
        reply = await client.get("/v1/nothing-here")
    assert reply.status_code == 404
    assert reply.headers["content-type"].startswith("application/problem+json")
    body = reply.json()
    assert body["code"] == "not_found"
    assert body["status"] == 404
    assert {"type", "title", "detail"} <= set(body)


class _Body(RequestModel):
    name: str
    count: int


async def test_validation_error_is_problem_json_with_field_names():
    app = _app()

    @app.post("/v1/echo")
    async def echo(body: _Body) -> dict:
        return {"ok": True}

    async with _client(app) as client:
        wrong_type = await client.post("/v1/echo", json={"name": "a", "count": "many"})
        extra = await client.post("/v1/echo", json={"name": "a", "count": 1, "tenant": "b"})
    for reply, field in ((wrong_type, "count"), (extra, "tenant")):
        assert reply.status_code == 422
        assert reply.headers["content-type"].startswith("application/problem+json")
        body = reply.json()
        assert body["code"] == "invalid_request"
        assert field in body["detail"]


async def test_validation_problem_never_echoes_the_submitted_value():
    app = _app()

    @app.post("/v1/echo")
    async def echo(body: _Body) -> dict:
        return {"ok": True}

    secret = "acp_" + "x" * 20
    async with _client(app) as client:
        reply = await client.post("/v1/echo", json={"name": "a", "count": secret})
    assert secret not in reply.text


async def test_unhandled_error_is_500_without_traceback():
    app = _app()

    @app.get("/v1/boom")
    async def boom() -> dict:
        raise RuntimeError("internal detail that must stay inside")

    async with _client(app) as client:
        reply = await client.get("/v1/boom")
    assert reply.status_code == 500
    body = reply.json()
    assert body["code"] == "internal"
    assert "Traceback" not in reply.text
    assert "internal detail" not in reply.text


def test_openapi_snapshot_is_current():
    expected = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    assert _app().openapi() == expected, (
        "control-plane/openapi.json is stale: run `uv run python -m agentic_cp openapi --write`"
    )
