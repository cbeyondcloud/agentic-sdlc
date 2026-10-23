"""The gateway key at login and when it is revoked (M2-17, DF-182): `agentic login` checks
the key with the gateway before storing it, `agentic login --rotate` replaces it through
the control plane, and a run paused on a rejected key says to log in again."""

import io
import json
from datetime import UTC, datetime

import httpx2
import keyring
import pytest
from cp_fakes import CP_URL, TOKEN, FakeControlPlane

from agentic.auth import SERVICE, TOKENLEAN_KEY
from agentic.engine.state import Pending, RunState
from agentic_cli import control_plane
from agentic_cli.commands import login as login_command
from agentic_cli.config import agentic_home
from agentic_cli.driving import next_steps
from agentic_cli.exit_codes import ExitCode
from agentic_cli.main import main

GATEWAY = "https://gateway.acme.example"
KEY = "tlk-" + "test" + "-0123456789"  # built from pieces: not a real key shape


def _config(extra: str = "") -> None:
    home = agentic_home()
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(f"gateway_url: {GATEWAY}\n{extra}", encoding="utf-8")


@pytest.fixture
def gateway(home, monkeypatch):
    """A fake gateway answering `GET /v1/models` with `status` (or unreachable)."""
    state = {"status": 200, "seen": []}

    def handle(request):
        state["seen"].append(request)
        if state["status"] is None:
            raise httpx2.ConnectError("refused", request=request)
        return httpx2.Response(state["status"], json={"object": "list", "data": []})

    monkeypatch.setattr(login_command, "gateway_transport", lambda: httpx2.MockTransport(handle))
    monkeypatch.delenv("AGENTIC_GATEWAY_URL", raising=False)
    return state


def _login(monkeypatch, capsys, *args):
    monkeypatch.setattr("sys.stdin", io.StringIO(KEY + "\n"))
    code = main(["--json", "login", "--key-stdin", *args])
    out = capsys.readouterr()
    return code, json.loads(out.out) if out.out.strip() else None, out.err


def _stored():
    return keyring.get_keyring().get_password(SERVICE, TOKENLEAN_KEY)


def test_an_accepted_key_is_stored_as_verified(gateway, monkeypatch, capsys):
    _config()
    code, data, _ = _login(monkeypatch, capsys)
    assert code == ExitCode.OK and data["verified"] is True
    assert _stored() == KEY
    [request] = gateway["seen"]
    assert str(request.url) == f"{GATEWAY}/v1/models"
    assert request.headers["authorization"] == f"Bearer {KEY}"


def test_a_rejected_key_is_not_stored(gateway, monkeypatch, capsys):
    _config()
    gateway["status"] = 401
    code, _, err = _login(monkeypatch, capsys)
    assert code == ExitCode.REFUSED and _stored() is None
    problem = json.loads(err)
    assert "did not accept" in problem["error"] and "invite" in problem["hint"]
    assert KEY not in err


def test_an_unreachable_gateway_stores_the_key_unverified_with_a_warning(
    gateway, monkeypatch, capsys
):
    _config()
    gateway["status"] = None
    code, data, _ = _login(monkeypatch, capsys)
    assert code == ExitCode.OK and data["verified"] is False and _stored() == KEY
    monkeypatch.setattr("sys.stdin", io.StringIO(KEY + "\n"))
    assert main(["login", "--key-stdin"]) == ExitCode.OK  # people see the warning
    assert "could not be checked" in capsys.readouterr().err


def test_without_a_gateway_url_nothing_is_asked(gateway, monkeypatch, capsys):
    code, data, _ = _login(monkeypatch, capsys)
    assert code == ExitCode.OK and data["verified"] is False
    assert gateway["seen"] == []


@pytest.fixture
def plane(home, monkeypatch, gateway):
    fake = FakeControlPlane()
    monkeypatch.setattr(control_plane, "make_transport", lambda: fake.transport)
    monkeypatch.setenv("AGENTIC_CONTROL_PLANE_TOKEN", TOKEN)
    _config(f"org: acme\ncontrol_plane_url: {CP_URL}\n")
    return fake


def test_rotate_stores_the_new_key_from_the_control_plane(plane, monkeypatch, capsys):
    plane.proxy_key = KEY
    code = main(["--json", "login", "--rotate"])
    data = json.loads(capsys.readouterr().out)
    assert code == ExitCode.OK and data["key_id"] == "0123456789abcdef"
    assert _stored() == KEY and KEY not in json.dumps(data)
    assert [r.url.path for r in plane.requests] == ["/v1/proxy-keys/rotate"]


def test_rotate_without_a_key_explains(plane, monkeypatch, capsys):
    code = main(["--json", "login", "--rotate"])
    assert code == ExitCode.REFUSED
    assert "invite" in json.loads(capsys.readouterr().err)["hint"]


def test_rotate_is_only_for_the_gateway_key(plane, capsys):
    assert main(["login", "--rotate", "--for", "control-plane"]) == ExitCode.USAGE


def test_a_run_paused_on_a_rejected_key_says_to_log_in_again():
    state = RunState.model_construct(  # only what next_steps reads
        run_id="r-1",
        status="paused",
        pending=Pending(
            kind="pause",
            id="gateway-key",
            reason="GatewayKeyRejected: the model gateway refused your key",
            since=datetime.now(UTC),
            step="build",
        ),
    )
    steps = next_steps(state)
    assert steps[0].startswith("agentic login") and steps[1] == "agentic resume r-1"
