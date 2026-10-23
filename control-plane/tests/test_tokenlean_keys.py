"""The gateway key client against TokenLean's documented `/admin/keys` API (ST-9), through a
mock transport (no live TokenLean): what it sends, how it reads answers, and that the admin
key never shows in an error."""

import json

import httpx2
import pytest

from agentic_cp.keys.gateway import (
    GatewayUnavailable,
    NoGatewayKeys,
    TokenLeanKeys,
    gateway_keys_for,
)
from agentic_cp.settings import Settings

pytestmark = pytest.mark.anyio

ADMIN_KEY = "adm-" + "not-a-real-key-0000"
KEY = "tlk-" + "f" * 32
KEY_ID = "0123456789abcdef"


def _client(handler) -> TokenLeanKeys:
    return TokenLeanKeys(
        "https://gateway.acme.example", ADMIN_KEY, transport=httpx2.MockTransport(handler)
    )


async def test_create_posts_the_binding_and_reads_the_key_once():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx2.Response(
            201,
            json={"key": KEY, "key_id": KEY_ID, "tenant_id": "acme", "notice": "shown once"},
        )

    issued = await _client(handler).create(
        tenant="acme", team="payments", user="dev@acme.example", label="agentic dev@acme.example"
    )
    assert (issued.key, issued.key_id) == (KEY, KEY_ID)
    assert KEY not in repr(issued)
    [request] = seen
    assert request.method == "POST" and request.url.path == "/admin/keys"
    assert request.headers["authorization"] == f"Bearer {ADMIN_KEY}"
    assert json.loads(request.content) == {
        "tenant_id": "acme",
        "team": "payments",
        "user": "dev@acme.example",
        "label": "agentic dev@acme.example",
    }


async def test_create_without_a_team_leaves_it_out():
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        return httpx2.Response(201, json={"key": KEY, "key_id": KEY_ID})

    await _client(handler).create(tenant="acme", team=None, user="a@acme.example", label="x")
    assert "team" not in seen[0]


async def test_rotate_and_revoke_use_the_key_id():
    seen = []

    def handler(request):
        seen.append((request.method, request.url.path))
        if request.url.path.endswith("/rotate"):
            return httpx2.Response(
                200, json={"key": KEY, "key_id": "fedcba9876543210", "replaced_key_id": KEY_ID}
            )
        return httpx2.Response(200, json={"revoked": True, "key_id": KEY_ID})

    client = _client(handler)
    rotated = await client.rotate(KEY_ID)
    await client.revoke(KEY_ID)
    assert rotated.key_id == "fedcba9876543210"
    assert seen == [
        ("POST", f"/admin/keys/{KEY_ID}/rotate"),
        ("DELETE", f"/admin/keys/{KEY_ID}"),
    ]


async def test_revoking_a_key_the_gateway_no_longer_has_counts_as_revoked():
    await _client(lambda request: httpx2.Response(404, json={"detail": "nope"})).revoke(KEY_ID)


@pytest.mark.parametrize("status", [401, 403, 409, 422, 500, 503])
async def test_other_answers_are_unavailable_and_never_show_the_admin_key(status):
    client = _client(lambda request: httpx2.Response(status, json={"detail": ADMIN_KEY}))
    with pytest.raises(GatewayUnavailable) as caught:
        await client.revoke(KEY_ID)
    assert ADMIN_KEY not in str(caught.value) and str(status) in str(caught.value)


async def test_a_network_failure_is_unavailable():
    def handler(request):
        raise httpx2.ConnectError("refused", request=request)

    with pytest.raises(GatewayUnavailable):
        await _client(handler).create(tenant="acme", team=None, user="a@acme.example", label="x")


async def test_a_malformed_answer_is_unavailable():
    client = _client(lambda request: httpx2.Response(201, json={"key": KEY}))
    with pytest.raises(GatewayUnavailable):
        await client.create(tenant="acme", team=None, user="a@acme.example", label="x")


def test_the_deployment_picks_the_client_from_its_settings():
    base = {"AGENTIC_CP_DATABASE_URL": "postgresql://x@h/db"}
    assert isinstance(gateway_keys_for(Settings.from_env(base)), NoGatewayKeys)
    configured = Settings.from_env(
        {
            **base,
            "AGENTIC_CP_GATEWAY_ADMIN_URL": "https://gateway.acme.example",
            "AGENTIC_CP_GATEWAY_ADMIN_KEY": ADMIN_KEY,
        }
    )
    keys = gateway_keys_for(configured)
    assert isinstance(keys, TokenLeanKeys) and keys.configured and ADMIN_KEY not in repr(keys)
