"""`agentic login --invite <link>` (M2-18): one redemption stores the control-plane token
and the gateway key in the OS keyring - nothing to copy and paste - and records the login;
a link to a plain-http host is refused before anything is sent, and a used link is
refused with what to do."""

import json

import keyring
import pytest
from cp_fakes import CP_URL, TOKEN, FakeControlPlane

from agentic.auth import SERVICE, TOKENLEAN_KEY
from agentic_cli import control_plane
from agentic_cli.config import agentic_home
from agentic_cli.exit_codes import ExitCode
from agentic_cli.main import main

INVITE = "aci_" + "i" * 43  # the shape of an invite token, not a real one
GATEWAY_KEY = "tlk-" + "test" + "-0123456789"
LINK = f"{CP_URL}/invite#{INVITE}"


@pytest.fixture
def plane(home, monkeypatch):
    fake = FakeControlPlane()
    monkeypatch.setattr(control_plane, "make_transport", lambda: fake.transport)
    monkeypatch.delenv("AGENTIC_CONTROL_PLANE_TOKEN", raising=False)
    monkeypatch.delenv("AGENTIC_CONTROL_PLANE_URL", raising=False)
    return fake


def _stored(name):
    return keyring.get_keyring().get_password(SERVICE, name)


def _run(capsys, *args):
    code = main(["--json", "login", *args])
    out = capsys.readouterr()
    return code, json.loads(out.out) if out.out.strip() else None, out.err


def test_an_invite_link_stores_both_secrets_and_records_the_login(plane, capsys):
    plane.add_invite(INVITE, gateway_key=GATEWAY_KEY)
    code, data, err = _run(capsys, "--invite", LINK)
    assert code == ExitCode.OK, err
    assert _stored(control_plane.CONTROL_PLANE_TOKEN) == TOKEN
    assert _stored(TOKENLEAN_KEY) == GATEWAY_KEY
    assert data["tenant"] == "acme" and data["user"] == "dev@acme.example"
    assert data["gateway_key_id"] == "0123456789abcdef"
    assert TOKEN not in json.dumps(data) and GATEWAY_KEY not in json.dumps(data)
    [request] = plane.requests
    assert request.url.path == "/v1/invites/redeem" and "authorization" not in request.headers
    assert json.loads(request.content) == {"token": INVITE}
    record = json.loads((agentic_home() / "control-plane.json").read_text(encoding="utf-8"))
    assert (record["url"], record["tenant"], record["user"]) == (CP_URL, "acme", "dev@acme.example")


def test_the_settings_are_written_to_the_user_config(plane, capsys):  # DF-952
    plane.add_invite(INVITE, gateway_key=GATEWAY_KEY)
    config = agentic_home() / "config.yaml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text("# mine\norg: acme\n", encoding="utf-8")
    assert main(["login", "--invite", LINK]) == ExitCode.OK
    shown = capsys.readouterr().out
    assert f"control_plane_url: {CP_URL}" in shown and "added" in shown
    assert str(config) in shown
    assert config.read_text(encoding="utf-8") == (
        f"# mine\norg: acme\ncontrol_plane_url: {CP_URL}\n"
        "gateway_url: https://gateway.acme.example\n"
    )


def test_with_json_the_result_says_what_was_written(plane, capsys):
    plane.add_invite(INVITE, gateway_key=GATEWAY_KEY)
    code, data, _ = _run(capsys, "--invite", LINK)
    assert code == ExitCode.OK
    assert data["config_written"] is True
    assert data["config_file"] == str(agentic_home() / "config.yaml")
    assert data["config"] == {
        "control_plane_url": CP_URL,
        "gateway_url": "https://gateway.acme.example",
    }


def test_print_config_only_shows_the_lines(plane, capsys):
    plane.add_invite(INVITE, gateway_key=GATEWAY_KEY)
    assert main(["login", "--invite", LINK, "--print-config"]) == ExitCode.OK
    shown = capsys.readouterr().out
    assert "Add to" in shown and f"control_plane_url: {CP_URL}" in shown
    assert not (agentic_home() / "config.yaml").exists()


def test_a_config_that_cannot_be_edited_is_left_alone_and_the_lines_shown(plane, capsys):
    plane.add_invite(INVITE, gateway_key=GATEWAY_KEY)
    config = agentic_home() / "config.yaml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text("org: [unclosed\n", encoding="utf-8")
    assert main(["login", "--invite", LINK]) == ExitCode.OK  # the keys are stored
    captured = capsys.readouterr()
    assert f"control_plane_url: {CP_URL}" in captured.out
    assert "not valid YAML" in captured.err
    assert config.read_text(encoding="utf-8") == "org: [unclosed\n"
    assert _stored(control_plane.CONTROL_PLANE_TOKEN) == TOKEN


def test_print_config_is_only_for_invite(plane, capsys):
    assert main(["login", "--print-config"]) == ExitCode.USAGE


def test_without_a_gateway_key_the_notice_is_passed_on(plane, capsys):
    plane.add_invite(INVITE)
    code, data, _ = _run(capsys, "--invite", LINK)
    assert code == ExitCode.OK and _stored(TOKENLEAN_KEY) is None
    assert data["notice"] == "no gateway key from this control plane"


def test_a_used_link_is_refused_and_nothing_is_stored(plane, capsys):
    plane.add_invite(INVITE)
    plane.used_invites.add(INVITE)
    code, _, err = _run(capsys, "--invite", LINK)
    assert code == ExitCode.REFUSED
    assert "new invite" in json.loads(err)["hint"]
    assert _stored(control_plane.CONTROL_PLANE_TOKEN) is None


def test_a_plain_http_link_is_refused_before_anything_is_sent(plane, capsys):
    code, _, _ = _run(capsys, "--invite", f"http://cp.acme.example/invite#{INVITE}")
    assert code == ExitCode.REFUSED and plane.requests == []


def test_a_bare_token_needs_a_url(plane, capsys):
    plane.add_invite(INVITE)
    assert _run(capsys, "--invite", INVITE)[0] == ExitCode.INVALID
    assert _run(capsys, "--invite", INVITE, "--url", CP_URL)[0] == ExitCode.OK


def test_invite_redemption_is_refused_in_ci(plane, capsys, monkeypatch):
    plane.add_invite(INVITE)
    monkeypatch.setenv("CI", "true")
    assert _run(capsys, "--invite", LINK)[0] == ExitCode.REFUSED
    assert plane.requests == []  # the link is not used up


def test_invite_takes_no_other_key_option(plane, capsys):
    assert main(["login", "--invite", LINK, "--rotate"]) == ExitCode.USAGE
    assert main(["login", "--invite", LINK, "--for", "jira"]) == ExitCode.USAGE


@pytest.mark.parametrize(
    "gateway",
    ["http://gateway.acme.example", "https://user:pw@gateway.acme.example", "not a url"],
)
def test_an_unsafe_gateway_url_is_not_written(plane, capsys, gateway):  # review F6
    plane.add_invite(INVITE, gateway_key=GATEWAY_KEY)["gateway_url"] = gateway
    assert main(["login", "--invite", LINK]) == ExitCode.OK
    captured = capsys.readouterr()
    text = (agentic_home() / "config.yaml").read_text(encoding="utf-8")
    assert "gateway_url" not in text and f"control_plane_url: {CP_URL}" in text
    assert "gateway_url" in captured.err and "pw" not in captured.err + captured.out


def test_an_invite_is_not_used_when_no_key_could_be_stored(plane, capsys, monkeypatch):
    """Review F3: no keyring and a credential file that cannot be written - checked before
    the link is used up."""
    import keyring as keyring_module
    from test_credential_store import NoBackend

    plane.add_invite(INVITE, gateway_key=GATEWAY_KEY)
    monkeypatch.delenv("AGENTIC_CREDENTIAL_STORE", raising=False)
    previous = keyring_module.get_keyring()
    keyring_module.set_keyring(NoBackend())
    try:
        agentic_home().parent.mkdir(parents=True, exist_ok=True)
        agentic_home().write_text("a file where the folder should be", encoding="utf-8")
        code, _, err = _run(capsys, "--invite", LINK)
    finally:
        keyring_module.set_keyring(previous)
    assert code == ExitCode.ERROR and "credential file" in err
    assert plane.requests == []
