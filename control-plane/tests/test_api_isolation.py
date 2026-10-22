"""Generated API isolation tests (Review Focus 6, DR-5): every `/v1` route that names an
object answers for another tenant's object exactly as for a missing one, and list routes
never show it. Cases are registered in `isolation.ENDPOINT_CASES`."""

import json
import uuid

import pytest
from isolation import ENDPOINT_CASES

from agentic_cp.app import create_app
from agentic_cp.settings import Settings

pytestmark = pytest.mark.anyio

OBJECT_ROUTES = [c for c in ENDPOINT_CASES if not c.must_not_list]
LIST_ROUTES = [c for c in ENDPOINT_CASES if c.must_not_list]
#: Routes whose path parameter is a credential, not an object id: each has its own test
#: (`test_webhook_token_resolves_only_its_tenant`).
CREDENTIAL_ROUTES = {"POST /v1/policy/webhook/{hook_token}"}


def _fill(path: str, values: dict[str, str]) -> str:
    return path.format(**values)


@pytest.mark.parametrize("case", OBJECT_ROUTES, ids=[c.id for c in OBJECT_ROUTES])
async def test_api_isolation(api, case):
    values = await case.make_object_in_b(api)
    missing = {key: _missing(key, value) for key, value in values.items()}
    token = api.a.admin_token
    other = await api.call(case.method, _fill(case.path, values), token, case.filled_body(values))
    absent = await api.call(
        case.method, _fill(case.path, missing), token, case.filled_body(missing)
    )
    assert other.status_code == absent.status_code == case.expect_status, (other.text, absent.text)
    assert _normalised(other.json(), values) == _normalised(absent.json(), missing)


@pytest.mark.parametrize("case", LIST_ROUTES, ids=[c.id for c in LIST_ROUTES])
async def test_api_list_isolation(api, case):
    values = await case.make_object_in_b(api)
    reply = await api.call(case.method, case.path, api.a.admin_token, case.body)
    assert reply.status_code == 200
    for value in values.values():
        assert value not in reply.text


def test_every_object_route_has_an_isolation_case():
    """Every `/v1` route with a path parameter is covered (and every list route that
    returns tenant rows), so a new route without a case fails here."""
    app = create_app(Settings.from_env({"AGENTIC_CP_DATABASE_URL": "postgresql://x@h/db"}))
    routes = set()
    for path, operations in app.openapi()["paths"].items():
        if not path.startswith("/v1/") or "{" not in path:
            continue
        for method in operations:
            routes.add(f"{method.upper()} {path}")
    covered = {f"{c.method} {c.path}" for c in OBJECT_ROUTES} | CREDENTIAL_ROUTES
    assert routes <= covered, sorted(routes - covered)


def _missing(key: str, value: str) -> str:
    """A value of the same shape that names nothing."""
    if _looks_like_id(value):
        return str(uuid.uuid4())
    if key == "email":
        return f"missing-{uuid.uuid4().hex[:8]}@absent.example.test"
    return f"missing-{uuid.uuid4().hex[:8]}"


def _looks_like_id(value: str) -> bool:
    try:
        uuid.UUID(value)
    except ValueError:
        return False
    return True


def _normalised(body: dict, values: dict[str, str]) -> str:
    text = json.dumps(body, sort_keys=True)
    for key, value in values.items():
        text = text.replace(value, f"<{key}>")
    return text
