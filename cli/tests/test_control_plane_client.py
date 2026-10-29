"""The CLI's control-plane client (SP15 Task 10): bearer and version headers, never a
tenant; problem documents become CLI errors with the server's hint; the bundle is digest
checked; the token is only ever sent to the URL it was verified against (R18)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import httpx2
import pytest
from cp_fakes import CP_URL, TOKEN, FakeControlPlane

from agentic import auth
from agentic.orgapi import CLI_VERSION_HEADER
from agentic_cli import __version__
from agentic_cli.control_plane import (
    CONTROL_PLANE_TOKEN,
    ChainRejected,
    ControlPlaneClient,
    ControlPlaneRefused,
    ControlPlaneUnavailable,
    KillStateReply,
    client_for,
    record_login,
)
from agentic_cli.exit_codes import ExitCode


@pytest.fixture
def fake(tmp_path):
    root = tmp_path / "policy-repo"
    (root / "policy").mkdir(parents=True)
    (root / "policy" / "org.yaml").write_text("allowed_repo_patterns: []\n", encoding="utf-8")
    return FakeControlPlane(policy_root=root)


def _client(fake, token=TOKEN):
    return ControlPlaneClient(CP_URL, token, transport=fake.transport)


def test_client_sends_bearer_and_cli_version_headers(fake):
    _client(fake).me()
    sent = fake.requests[-1]
    assert sent.headers["authorization"] == f"Bearer {TOKEN}"
    assert sent.headers[CLI_VERSION_HEADER] == __version__
    assert sent.headers["user-agent"] == f"agentic-cli/{__version__}"


def test_client_never_sends_a_tenant(fake):
    client = _client(fake)
    client.me()
    client.check("agent.edit.repo", team="payments", repo="github.com/acme/app")
    client.ingest("run-1", [{"seq": 1, "hash": "h", "prev": ""}])
    for request in fake.requests:
        names = {k.lower() for k in request.headers} | set(request.url.params)
        assert not any("tenant" in name for name in names), names
        body = json.loads(request.content) if request.content else {}
        assert "tenant" not in json.dumps(body)


def test_problem_json_becomes_cli_error_with_hint(fake):
    with pytest.raises(ControlPlaneRefused) as caught:
        _client(fake, token="acp_" + "w" * 43).me()
    assert caught.value.code == ExitCode.REFUSED
    assert "agentic login --for control-plane" in caught.value.hint


def test_unreachable_control_plane_is_a_clear_error(fake):
    fake.down = True
    with_password = "https://u:" + "pw" + "@cp.acme.example"
    client = ControlPlaneClient(with_password, TOKEN, transport=fake.transport)
    with pytest.raises(ControlPlaneUnavailable) as caught:
        client.me()
    assert caught.value.code == ExitCode.ERROR
    assert "cp.acme.example" in str(caught.value) and "pw" not in str(caught.value)


def test_bundle_digest_is_checked(fake):
    client = _client(fake)
    active = client.active_policy()
    assert client.bundle(active) == fake.bundle()
    fake.bundle_override = b"tampered"
    with pytest.raises(ControlPlaneRefused, match="digest"):
        client.bundle(active)


def test_plain_http_is_refused_except_on_loopback():
    with pytest.raises(ControlPlaneRefused):
        ControlPlaneClient("http://cp.acme.example", TOKEN)
    ControlPlaneClient("http://127.0.0.1:8080", TOKEN).close()


def test_run_url_points_at_the_run_api(fake):
    assert _client(fake).run_url("run-1") == f"{CP_URL}/v1/runs/run-1"


def test_token_is_not_sent_to_a_different_url(fake, home, monkeypatch):
    """R18: a keyring token is bound to the URL `agentic login` verified."""
    auth.Credentials().store(CONTROL_PLANE_TOKEN, TOKEN)
    record_login(home / ".agentic", CP_URL, "acme", "dev@acme.example")
    env = {"AGENTIC_CONTROL_PLANE_URL": "https://evil.example"}
    with pytest.raises(ControlPlaneRefused) as caught:
        client_for(env, {}, home / ".agentic", transport=fake.transport)
    assert "agentic login --for control-plane" in caught.value.hint
    assert fake.requests == []
    same = client_for({"AGENTIC_CONTROL_PLANE_URL": CP_URL + "/"}, {}, home / ".agentic")
    assert same is not None and same.base_url == CP_URL


def test_an_environment_token_needs_no_login_record(fake, home):
    env = {"AGENTIC_CONTROL_PLANE_URL": CP_URL, "AGENTIC_CONTROL_PLANE_TOKEN": TOKEN}
    client = client_for(env, {}, home / ".agentic", transport=fake.transport)
    assert client.me().tenant.slug == "acme"


def test_no_url_means_no_client(home):
    assert client_for({}, {}, home / ".agentic") is None


def test_no_token_is_refused_with_the_login_hint(home):
    with pytest.raises(ControlPlaneRefused) as caught:
        client_for({"AGENTIC_CONTROL_PLANE_URL": CP_URL}, {}, home / ".agentic")
    assert "agentic login --for control-plane" in caught.value.hint


def test_ingest_refusals_carry_head_and_problems(fake):
    fake.refuse_ingest = {
        "status": 422,
        "title": "x",
        "detail": "broken",
        "code": "ingest.chain_broken",
        "problems": ["event 3: hash"],
    }

    with pytest.raises(ChainRejected) as caught:
        _client(fake).ingest("run-1", [{"seq": 1}])
    assert caught.value.problems == ["event 3: hash"]
    fake.refuse_ingest = {
        "status": 409,
        "title": "x",
        "detail": "gap",
        "code": "ingest.gap",
        "head": {"seq": 2, "hash": "h2"},
    }
    with pytest.raises(ChainRejected) as caught:
        _client(fake).ingest("run-1", [{"seq": 5}])
    assert caught.value.code_name == "ingest.gap" and caught.value.head == (2, "h2")


def test_transport_errors_never_leak_the_token(fake):
    def boom(request):
        raise httpx2.ReadTimeout(f"timed out with {request.headers['authorization']}")

    client = ControlPlaneClient(CP_URL, TOKEN, transport=httpx2.MockTransport(boom))
    with pytest.raises(ControlPlaneUnavailable) as caught:
        client.me()
    assert TOKEN not in str(caught.value)


def test_heartbeat_sends_both_times_and_the_state(fake):
    """M3-07: the beat's own time and its sending time, so the server judges a late beat by
    its true age."""
    taken = datetime(2026, 10, 29, 10, 0, tzinfo=UTC)
    sent = taken + timedelta(seconds=7)
    reply = _client(fake).heartbeat("run-1", beat_at=taken, sent_at=sent, state="waiting")
    assert reply.counted and reply.status == "open"
    [beat] = fake.heartbeats
    assert beat == {
        "run_id": "run-1",
        "beat_at": taken.isoformat(),
        "sent_at": sent.isoformat(),
        "state": "waiting",
    }
    assert fake.requests[-1].extensions["timeout"]["read"] == 5.0  # never a long wait


def test_kill_state_without_an_answer_is_unavailable(fake, monkeypatch):
    """DF-706: no assert (gone under python -O): an answer without a state is a clear
    ControlPlaneUnavailable."""
    client = _client(fake)
    monkeypatch.setattr(client, "kill_state_reply", lambda **kw: KillStateReply(None, "t"))
    with pytest.raises(ControlPlaneUnavailable, match="no kill-switch state"):
        client.kill_state(team="payments", repo=None, pipeline=None)
