"""The gateway routing port (M2-09) against TokenLean's `/admin/routing` contract (PR #57),
on a mock transport - never a live gateway (owner decision 73): GET gives the current
version and fragment; PUT `{fragment, expected_version, comment}` gives the new version, 409
on a version conflict, 422 `invalid_fragment` with each problem's path; a team-bound or
non-admin key is 403. The admin key never appears in an error, a repr or a log record
(Review Focus 4)."""

from __future__ import annotations

import json
import logging

import httpx2
import pytest

from agentic_cp.keys.gateway import GatewayUnavailable
from agentic_cp.routing.gateway import (
    NoGatewayRouting,
    RoutingConflict,
    RoutingRefused,
    RoutingState,
    TokenLeanRouting,
    gateway_routing_for,
)
from agentic_cp.settings import Settings

pytestmark = pytest.mark.anyio

ADMIN_KEY = "tl-admin-" + "k3y" * 8  # gitleaks:allow (a made-up shape, not a real key)
URL = "https://gateway.example.test"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def gateway(handler) -> TokenLeanRouting:
    return TokenLeanRouting(URL, ADMIN_KEY, transport=httpx2.MockTransport(handler))


def answer(version: int, fragment: dict, **extra) -> dict:
    return {"version": version, "fragment": fragment, "action": "applied", **extra}


async def test_read_gives_the_current_version_and_fragment():
    seen = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(200, json=answer(7, {"tenants": {"gw-a": {}}}))

    state = await gateway(handler).read()
    assert state == RoutingState(7, {"tenants": {"gw-a": {}}})
    assert seen[0].method == "GET" and seen[0].url.path == "/admin/routing"
    assert seen[0].headers["Authorization"] == f"Bearer {ADMIN_KEY}"


async def test_no_overlay_yet_is_version_zero():
    state = await gateway(lambda r: httpx2.Response(200, json=answer(0, {}))).read()
    assert state == RoutingState(0, {})


async def test_write_sends_the_fragment_and_the_expected_version():
    seen = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(json.loads(request.content))
        return httpx2.Response(200, json=answer(8, seen[-1]["fragment"]))

    state = await gateway(handler).write({"teams": {}}, expected_version=7, comment="policy x")
    assert seen == [{"fragment": {"teams": {}}, "expected_version": 7, "comment": "policy x"}]
    assert state.version == 8


async def test_read_version_reads_one_stored_version():
    seen = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request.url.path)
        if request.url.path.endswith("/9"):
            return httpx2.Response(200, json=answer(9, {"a": 1}))
        return httpx2.Response(404, json={"detail": "no such version"})

    assert await gateway(handler).read_version(9) == RoutingState(9, {"a": 1})
    assert await gateway(handler).read_version(10) is None
    assert seen[0] == "/admin/routing/versions/9"


async def test_a_version_conflict_names_the_current_version():
    body = {"detail": {"error": "version_conflict", "current_version": 12}}
    with pytest.raises(RoutingConflict) as caught:
        await gateway(lambda r: httpx2.Response(409, json=body)).write(
            {}, expected_version=11, comment=""
        )
    assert caught.value.current_version == 12


async def test_an_invalid_fragment_lists_each_problem_with_its_path():
    body = {
        "detail": {
            "error": "invalid_fragment",
            "errors": [
                {"path": "tenants.gw-a.groups.G6_routing.rules[0].action.model", "message": "x"},
                {"path": "tenants.gw-a.teams.bad name", "message": "not a team name"},
            ],
        }
    }
    with pytest.raises(RoutingRefused) as caught:
        await gateway(lambda r: httpx2.Response(422, json=body)).write(
            {}, expected_version=1, comment=""
        )
    assert caught.value.errors == [
        "tenants.gw-a.groups.G6_routing.rules[0].action.model: x",
        "tenants.gw-a.teams.bad name: not a team name",
    ]


@pytest.mark.parametrize(
    ("status", "words"),
    [(401, "401"), (403, "no team binding"), (503, "503"), (500, "500")],
)
async def test_other_answers_are_unavailable(status, words):
    with pytest.raises(GatewayUnavailable) as caught:
        await gateway(lambda r: httpx2.Response(status, json={"detail": "no"})).read()
    assert words in str(caught.value)


async def test_an_unreachable_gateway_or_a_strange_answer_is_unavailable():
    def down(request):
        raise httpx2.ConnectError("refused", request=request)

    with pytest.raises(GatewayUnavailable):
        await gateway(down).read()
    with pytest.raises(GatewayUnavailable):
        await gateway(lambda r: httpx2.Response(200, text="<html>")).read()
    with pytest.raises(GatewayUnavailable):
        await gateway(lambda r: httpx2.Response(200, json={"version": "x", "fragment": []})).read()


async def test_admin_key_never_appears(caplog):
    caplog.set_level(logging.DEBUG)
    messages = []
    for status in (401, 403, 409, 422, 500):
        try:
            await gateway(lambda r, s=status: httpx2.Response(s, json={"detail": {}})).write(
                {}, expected_version=0, comment=""
            )
        except (GatewayUnavailable, RoutingConflict, RoutingRefused) as error:
            messages.append(str(error))

    def down(request):
        raise httpx2.ConnectError(f"refused {request.headers['Authorization']}", request=request)

    try:
        await gateway(down).read()
    except GatewayUnavailable as error:
        messages.append(str(error))
    assert len(messages) == 6
    assert all(ADMIN_KEY not in message for message in messages)
    assert ADMIN_KEY not in repr(gateway(lambda r: None))
    assert ADMIN_KEY not in caplog.text


async def test_without_a_gateway_admin_nothing_is_sent():
    settings = Settings.from_env({"AGENTIC_CP_DATABASE_URL": "postgresql://x@h/db"})
    routing = gateway_routing_for(settings)
    assert isinstance(routing, NoGatewayRouting) and not routing.configured
    with pytest.raises(GatewayUnavailable):
        await routing.read()
    configured = Settings.from_env(
        {
            "AGENTIC_CP_DATABASE_URL": "postgresql://x@h/db",
            "AGENTIC_CP_GATEWAY_ADMIN_URL": URL,
            "AGENTIC_CP_GATEWAY_ADMIN_KEY": ADMIN_KEY,
        }
    )
    assert isinstance(gateway_routing_for(configured), TokenLeanRouting)
