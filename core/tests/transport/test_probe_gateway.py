"""`probe_gateway` (M3-09, DF-654): one `GET /v1/models` that says whether the gateway is
reachable, whether it accepts the key, which models it serves and what time it says it is
(for doctor's clock-skew check). Recorded response shapes, no live calls."""

from datetime import UTC, datetime

import httpx2

from agentic.transport import probe_gateway, verify_gateway_key

KEY = "tl-" + "k" * 24  # gitleaks:allow
DATE = "Sun, 25 Oct 2026 12:00:00 GMT"
MODELS = {
    "object": "list",
    "data": [
        {"id": "openrouter/anthropic/claude-sonnet-5.5", "object": "model"},
        {"id": "local/qwen3-coder", "object": "model"},
    ],
}


def transport(
    status: int, body: object = None, headers: dict | None = None, seen: list | None = None
):
    def handler(request: httpx2.Request) -> httpx2.Response:
        if seen is not None:
            seen.append(request)
        return httpx2.Response(status, json=body, headers=headers or {})

    return httpx2.MockTransport(handler)


def test_accepted_with_models_and_date():
    seen: list = []
    probe = probe_gateway(
        "https://gw.example.invalid/", KEY, transport=transport(200, MODELS, {"Date": DATE}, seen)
    )
    assert probe.verdict == "accepted"
    assert probe.models == ("openrouter/anthropic/claude-sonnet-5.5", "local/qwen3-coder")
    assert probe.server_date == datetime(2026, 10, 25, 12, 0, tzinfo=UTC)
    (request,) = seen
    assert str(request.url) == "https://gw.example.invalid/v1/models"
    assert request.headers["Authorization"] == f"Bearer {KEY}"


def test_rejected_key():
    probe = probe_gateway("https://gw.example.invalid", KEY, transport=transport(401, {}))
    assert probe.verdict == "rejected"
    assert probe.models is None
    assert "401" in probe.detail


def test_unreachable():
    def refuse(request):
        raise httpx2.ConnectError("refused")

    probe = probe_gateway("https://gw.example.invalid", KEY, transport=httpx2.MockTransport(refuse))
    assert probe.verdict == "unknown"
    assert "ConnectError" in probe.detail
    assert probe.server_date is None


def test_a_malformed_gateway_url_is_unknown_not_a_crash():
    """`agentic doctor` is what people run when the config is wrong: a typo in
    gateway_url is a finding, never a traceback."""
    for url in ("http://[::1", "https://gw.example.invalid:44x3"):
        probe = probe_gateway(url, KEY, transport=transport(200, MODELS))
        assert probe.verdict == "unknown", url
        assert "not a valid URL" in probe.detail
        assert KEY not in probe.detail


def test_a_body_without_a_model_list_still_accepts_the_key():
    probe = probe_gateway("https://gw.example.invalid", KEY, transport=transport(200, ["odd"]))
    assert probe.verdict == "accepted"
    assert probe.models is None


def test_server_error_is_unknown():
    probe = probe_gateway("https://gw.example.invalid", KEY, transport=transport(503, {}))
    assert probe.verdict == "unknown"
    assert "503" in probe.detail


def test_the_key_never_appears_in_the_detail():
    def echo(request):
        raise httpx2.ConnectError(f"failed with {request.headers['Authorization']}")

    probe = probe_gateway("https://gw.example.invalid", KEY, transport=httpx2.MockTransport(echo))
    assert KEY not in probe.detail


def test_verify_gateway_key_keeps_its_answers():
    assert (
        verify_gateway_key("https://gw.example.invalid", KEY, transport=transport(200, MODELS))
        == "accepted"
    )
    assert (
        verify_gateway_key("https://gw.example.invalid", KEY, transport=transport(403, {}))
        == "rejected"
    )
    assert (
        verify_gateway_key("https://gw.example.invalid", KEY, transport=transport(500, {}))
        == "unknown"
    )
