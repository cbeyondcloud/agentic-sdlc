"""`agentic login` (M1-53): the gateway key goes into the OS keyring through
`agentic.auth.Credentials`; refused in CI; the value is never printed."""

import io
import json

import keyring
import pytest
from keyring.backend import KeyringBackend

from agentic.auth import SERVICE, TOKENLEAN_KEY
from agentic_cli.exit_codes import ExitCode
from agentic_cli.main import main

FAKE_KEY = "tl-" + "test" + "-0123456789"  # built from pieces: not a real key shape


class MemoryKeyring(KeyringBackend):
    priority = 1

    def __init__(self) -> None:
        super().__init__()
        self.stored: dict[tuple[str, str], str] = {}

    def get_password(self, service, username):
        return self.stored.get((service, username))

    def set_password(self, service, username, password):
        self.stored[(service, username)] = password

    def delete_password(self, service, username):
        self.stored.pop((service, username), None)


@pytest.fixture
def ring(home):
    previous = keyring.get_keyring()
    memory = MemoryKeyring()
    keyring.set_keyring(memory)
    yield memory
    keyring.set_keyring(previous)


def test_login_stores_the_key_from_stdin(ring, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO(FAKE_KEY + "\n"))
    assert main(["--json", "login", "--key-stdin"]) == ExitCode.OK
    assert ring.stored == {(SERVICE, TOKENLEAN_KEY): FAKE_KEY}
    shown = capsys.readouterr()
    assert FAKE_KEY not in shown.out + shown.err
    assert json.loads(shown.out)["stored"] == TOKENLEAN_KEY


def test_login_prompts_without_echo(ring, monkeypatch):
    asked = {}

    def prompt(text, hide_input=False, **kwargs):
        asked["hidden"] = hide_input
        return FAKE_KEY

    monkeypatch.setattr("typer.prompt", prompt)
    assert main(["login"]) == ExitCode.OK
    assert asked["hidden"] is True and ring.stored[(SERVICE, TOKENLEAN_KEY)] == FAKE_KEY


def test_login_refuses_an_empty_key(ring, monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO("\n"))
    assert main(["login", "--key-stdin"]) == ExitCode.INVALID
    assert ring.stored == {}


def test_login_is_refused_in_ci(ring, monkeypatch, capsys):
    monkeypatch.setenv("CI", "true")
    monkeypatch.setattr("sys.stdin", io.StringIO(FAKE_KEY))
    assert main(["--json", "login", "--key-stdin"]) == ExitCode.REFUSED
    assert ring.stored == {}
    assert "AGENTIC_TOKENLEAN_API_KEY" in json.loads(capsys.readouterr().err)["hint"]


def test_a_keyring_failure_is_an_error_with_a_hint(home, monkeypatch, capsys):
    class Broken(MemoryKeyring):
        def set_password(self, service, username, password):
            raise RuntimeError("locked")

    previous = keyring.get_keyring()
    keyring.set_keyring(Broken())
    try:
        monkeypatch.setattr("sys.stdin", io.StringIO(FAKE_KEY))
        assert main(["login", "--key-stdin"]) == ExitCode.ERROR
    finally:
        keyring.set_keyring(previous)
    assert "AGENTIC_TOKENLEAN_API_KEY" in capsys.readouterr().err


GITHUB_TOKEN = "ghp_" + "login" + "Fake0123456789"  # gitleaks:allow - built from pieces


def test_login_for_github_stores_the_hosting_token(ring, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO(GITHUB_TOKEN + "\n"))
    assert main(["--json", "login", "--for", "github", "--key-stdin"]) == ExitCode.OK
    assert ring.stored == {(SERVICE, "github_token"): GITHUB_TOKEN}
    shown = capsys.readouterr()
    assert GITHUB_TOKEN not in shown.out + shown.err
    assert json.loads(shown.out)["stored"] == "github_token"


def test_login_for_a_configured_host_stores_its_credential(ring, home, monkeypatch):
    (home / ".agentic").mkdir()
    (home / ".agentic" / "config.yaml").write_text(
        "git_hosts:\n  ghe.example.com:\n    kind: github\n    credential: ghe_token\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("sys.stdin", io.StringIO(GITHUB_TOKEN + "\n"))
    assert main(["login", "--for", "ghe.example.com", "--key-stdin"]) == ExitCode.OK
    assert ring.stored == {(SERVICE, "ghe_token"): GITHUB_TOKEN}


def test_login_for_something_unknown_is_a_usage_error(ring, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO(GITHUB_TOKEN + "\n"))
    assert main(["login", "--for", "svn", "--key-stdin"]) == ExitCode.USAGE
    assert ring.stored == {} and "gateway" in capsys.readouterr().err


def test_login_for_jira_stores_the_jira_token(ring, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO(GITHUB_TOKEN + "\n"))
    assert main(["--json", "login", "--for", "jira", "--key-stdin"]) == ExitCode.OK
    assert ring.stored == {(SERVICE, "jira_token"): GITHUB_TOKEN}
    shown = capsys.readouterr()
    assert GITHUB_TOKEN not in shown.out + shown.err
    assert json.loads(shown.out)["stored"] == "jira_token"


def test_login_for_jira_stores_a_configured_credential_name(ring, home, monkeypatch):
    (home / ".agentic").mkdir()
    (home / ".agentic" / "config.yaml").write_text(
        "jira:\n  url: https://jira.acme.example\n  credential: jira_dc\n", encoding="utf-8"
    )
    monkeypatch.setattr("sys.stdin", io.StringIO(GITHUB_TOKEN + "\n"))
    assert main(["login", "--for", "jira", "--key-stdin"]) == ExitCode.OK
    assert ring.stored == {(SERVICE, "jira_dc"): GITHUB_TOKEN}


def test_login_for_github_in_ci_names_its_variable(ring, monkeypatch, capsys):
    monkeypatch.setenv("CI", "true")
    assert main(["--json", "login", "--for", "github", "--key-stdin"]) == ExitCode.REFUSED
    assert "AGENTIC_GITHUB_TOKEN" in json.loads(capsys.readouterr().err)["hint"]
