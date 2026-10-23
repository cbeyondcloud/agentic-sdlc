"""Which Jira site a run reads from, as whom, and with which token (M2-50): the `jira:` user
config, the environment overrides, and the token from `agentic.auth.Credentials`."""

from pathlib import Path

import pytest

from agentic.auth import Credentials
from agentic_cli.errors import CliError
from agentic_cli.exit_codes import ExitCode
from agentic_cli.jira import JIRA_TOKEN, JiraSettings, connect_jira, jira_settings
from agentic_cli.workspace import Workspace

TOKEN = "ATATT3x" + "SettingsFake" + "0123456789"  # gitleaks:allow - built from pieces


class Ring:
    def __init__(self, stored: dict[str, str] | None = None) -> None:
        self.stored = stored or {}

    def get_password(self, service, username):
        return self.stored.get(username)

    def set_password(self, service, username, password):
        self.stored[username] = password


def creds(env: dict | None = None, ring: dict | None = None) -> Credentials:
    return Credentials(environ=env or {}, keyring=Ring(ring))


def test_a_cloud_site_with_its_user():
    found = jira_settings(
        {"jira": {"url": "https://acme.atlassian.net/", "user": "mia@a.example"}}, {}
    )

    assert found == JiraSettings(url="https://acme.atlassian.net", user="mia@a.example")
    assert (found.credential, found.api_version, found.transition) == (JIRA_TOKEN, "2", None)


SECRET = "hun" + "ter2"  # built from pieces: not a real credential to a secret scanner


@pytest.mark.parametrize("env", [False, True])
def test_a_site_url_carrying_credentials_is_refused_without_echoing_them(env):  # review F4
    url = f"https://bob:{SECRET}@acme.atlassian.net"
    settings, environ = ({}, {"AGENTIC_JIRA_URL": url}) if env else ({"jira": {"url": url}}, {})
    with pytest.raises(CliError) as refused:
        jira_settings(settings, environ)
    error = refused.value
    assert error.code == ExitCode.INVALID and "jira.user" in error.hint
    assert all(SECRET not in text and "bob" not in text for text in (str(error), error.hint))


def test_a_data_center_site_needs_no_user_and_may_name_everything():
    found = jira_settings(
        {
            "jira": {
                "url": "https://jira.acme.example/jira",
                "credential": "jira_dc_token",
                "api_version": 2,
                "transition": "In Progress",
            }
        },
        {},
    )

    assert found == JiraSettings(
        url="https://jira.acme.example/jira",
        user=None,
        credential="jira_dc_token",
        api_version="2",
        transition="In Progress",
    )


def test_the_environment_overrides_the_site_and_user():
    env = {"AGENTIC_JIRA_URL": "https://ci.atlassian.net", "AGENTIC_JIRA_USER": "bot@a.example"}
    found = jira_settings({"jira": {"url": "https://acme.atlassian.net", "user": "x"}}, env)

    assert (found.url, found.user) == ("https://ci.atlassian.net", "bot@a.example")


@pytest.mark.parametrize(
    "settings, words",
    [
        ({}, "no Jira site"),
        ({"jira": "https://acme.atlassian.net"}, "must be a mapping"),
        ({"jira": {"url": "https://j.example", "project": "X"}}, "unknown setting"),
        ({"jira": {"url": "https://j.example", "credential": "Jira Token"}}, "credential"),
        ({"jira": {"url": "https://j.example", "api_version": "4"}}, "api_version"),
        ({"jira": {"url": "https://acme.atlassian.net"}}, "email"),
        ({"jira": {"url": "https://j.example", "transition": ""}}, "transition"),
    ],
)
def test_bad_settings_are_refused_with_a_config_hint(settings, words):
    with pytest.raises(CliError, match=words) as raised:
        jira_settings(settings, {})
    assert raised.value.code == ExitCode.INVALID
    assert "config.yaml" in (raised.value.hint or "") or "AGENTIC_JIRA_URL" in (
        raised.value.hint or ""
    )


def workspace(tmp_path: Path, settings: dict, env: dict | None = None) -> Workspace:
    return Workspace(tmp_path, tmp_path / "home", env or {}, settings, {})


def test_the_token_comes_from_the_environment_first(tmp_path):
    ws = workspace(tmp_path, {"jira": {"url": "https://jira.acme.example"}})
    with connect_jira(ws, credentials=creds({"AGENTIC_JIRA_TOKEN": TOKEN})) as client:
        assert client.base_url == "https://jira.acme.example"
        assert TOKEN not in repr(client)


def test_the_token_comes_from_the_keyring_under_its_credential_name(tmp_path):
    ws = workspace(tmp_path, {"jira": {"url": "https://j.example", "credential": "jira_dc"}})
    with connect_jira(ws, credentials=creds(ring={"jira_dc": TOKEN})) as client:
        assert client.base_url == "https://j.example"


def test_no_token_is_refused_naming_how_to_store_one(tmp_path):
    ws = workspace(tmp_path, {"jira": {"url": "https://jira.acme.example"}})
    with pytest.raises(CliError) as raised:
        connect_jira(ws, credentials=creds())
    assert raised.value.code == ExitCode.REFUSED
    assert "agentic login --for jira" in raised.value.hint
    assert "AGENTIC_JIRA_TOKEN" in raised.value.hint


def test_a_plain_http_site_is_invalid(tmp_path):
    ws = workspace(tmp_path, {"jira": {"url": "http://jira.acme.example"}})
    with pytest.raises(CliError, match="https") as raised:
        connect_jira(ws, credentials=creds({"AGENTIC_JIRA_TOKEN": TOKEN}))
    assert raised.value.code == ExitCode.INVALID
