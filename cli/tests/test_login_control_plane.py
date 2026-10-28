"""`agentic login --for control-plane` (SP15 Task 10, DF-182 control-plane half): the token
is verified through `/v1/me` before it is stored; the tenant, user and roles are shown."""

from __future__ import annotations

import io
import json

import keyring
import pytest
from cp_fakes import CP_URL, TOKEN, FakeControlPlane

from agentic.auth import SERVICE
from agentic_cli import control_plane
from agentic_cli.control_plane import CONTROL_PLANE_TOKEN, login_record
from agentic_cli.exit_codes import ExitCode
from agentic_cli.main import main


@pytest.fixture
def fake(monkeypatch, home):
    plane = FakeControlPlane()
    monkeypatch.setattr(control_plane, "make_transport", lambda: plane.transport)
    monkeypatch.setenv("AGENTIC_CONTROL_PLANE_URL", CP_URL)
    return plane


def _stored():
    return keyring.get_keyring().get_password(SERVICE, CONTROL_PLANE_TOKEN)


def test_login_for_control_plane_verifies_before_storing(fake, home, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO(TOKEN + "\n"))
    assert main(["--json", "login", "--for", "control-plane", "--key-stdin"]) == ExitCode.OK
    out = json.loads(capsys.readouterr().out)
    assert out == {
        "stored": CONTROL_PLANE_TOKEN,
        "where": "keyring",
        "verified": True,
        "url": CP_URL,
        "tenant": "acme",
        "user": "dev@acme.example",
        "org_roles": [],
        "teams": {"payments": ["developer"]},
    }
    assert _stored() == TOKEN
    assert login_record(home / ".agentic")["tenant"] == "acme"
    assert [r.url.path for r in fake.requests] == ["/v1/me"]


def test_login_shows_tenant_user_and_roles(fake, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO(TOKEN + "\n"))
    assert main(["login", "--for", "control-plane", "--key-stdin"]) == ExitCode.OK
    shown = capsys.readouterr()
    assert f"Signed in to {CP_URL} (Acme) as dev@acme.example" in shown.out
    assert "payments: developer" in shown.out
    assert TOKEN not in shown.out + shown.err


def test_login_for_control_plane_refuses_a_rejected_token_and_stores_nothing(
    fake, home, monkeypatch, capsys
):
    monkeypatch.setattr("sys.stdin", io.StringIO("acp_" + "w" * 43 + "\n"))
    assert main(["--json", "login", "--for", "control-plane", "--key-stdin"]) == ExitCode.REFUSED
    assert _stored() is None
    assert login_record(home / ".agentic") is None


def test_login_for_control_plane_without_url_is_invalid(home, monkeypatch, capsys):
    monkeypatch.delenv("AGENTIC_CONTROL_PLANE_URL", raising=False)
    monkeypatch.setattr("sys.stdin", io.StringIO(TOKEN + "\n"))
    assert main(["--json", "login", "--for", "control-plane", "--key-stdin"]) == ExitCode.INVALID
    hint = json.loads(capsys.readouterr().err)["hint"]
    assert "control_plane_url" in hint and "AGENTIC_CONTROL_PLANE_URL" in hint


def test_login_url_option_overrides_for_this_command(fake, home, monkeypatch, capsys):
    monkeypatch.delenv("AGENTIC_CONTROL_PLANE_URL", raising=False)
    monkeypatch.setattr("sys.stdin", io.StringIO(TOKEN + "\n"))
    argv = ["--json", "login", "--for", "control-plane", "--url", CP_URL + "/", "--key-stdin"]
    assert main(argv) == ExitCode.OK
    assert login_record(home / ".agentic")["url"] == CP_URL
    assert not (home / ".agentic" / "config.yaml").exists()  # ND-13: config not written


def test_unreachable_control_plane_stores_nothing(fake, monkeypatch):
    fake.down = True
    monkeypatch.setattr("sys.stdin", io.StringIO(TOKEN + "\n"))
    assert main(["login", "--for", "control-plane", "--key-stdin"]) == ExitCode.ERROR
    assert _stored() is None


def test_a_bad_url_is_refused_before_the_token_is_asked_for(home, monkeypatch, capsys):
    """DF-498: an http:// URL to another machine is refused before the token is read."""
    monkeypatch.setenv("AGENTIC_CONTROL_PLANE_URL", "http://cp.acme.example")
    read = []
    monkeypatch.setattr("agentic_cli.commands.login._read", lambda *a: read.append(a) or TOKEN)
    assert main(["--json", "login", "--for", "control-plane"]) == ExitCode.REFUSED
    assert read == [] and "https" in json.loads(capsys.readouterr().err)["error"]


def test_a_rejected_token_at_login_says_so_instead_of_repeating_the_command(
    fake, monkeypatch, capsys
):
    """DF-498: the 401 hint at login does not tell the user to run the command they ran."""
    monkeypatch.setattr("sys.stdin", io.StringIO("acp_" + "w" * 43 + "\n"))
    assert main(["--json", "login", "--for", "control-plane", "--key-stdin"]) == ExitCode.REFUSED
    hint = json.loads(capsys.readouterr().err)["hint"]
    assert "agentic login" not in hint and "token" in hint
