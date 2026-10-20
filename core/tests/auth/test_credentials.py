"""Tests for the credential provider: environment first, then the OS keyring (M1-48)."""

import pytest
from keyring.backend import KeyringBackend
from keyring.errors import KeyringError, NoKeyringError

from agentic.auth import TOKENLEAN_KEY, Credential, CredentialError, Credentials, env_var

ENV = "AGENTIC_TOKENLEAN_API_KEY"


class MemoryKeyring(KeyringBackend):
    """A real keyring backend, held in memory, that counts what is asked of it."""

    priority = 1

    def __init__(self, stored: dict[tuple[str, str], str] | None = None) -> None:
        super().__init__()
        self.stored = dict(stored or {})
        self.asked = 0

    def get_password(self, service: str, username: str) -> str | None:
        self.asked += 1
        return self.stored.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.stored[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        self.stored.pop((service, username), None)


class BrokenKeyring(MemoryKeyring):
    def __init__(self, error: KeyringError) -> None:
        super().__init__()
        self.error = error

    def get_password(self, service: str, username: str) -> str | None:
        raise self.error


def test_the_env_var_name():
    assert env_var(TOKENLEAN_KEY) == ENV


def test_env_wins_and_ci_never_touches_the_keyring():
    ring = MemoryKeyring({("agentic-sdlc", TOKENLEAN_KEY): "from-keyring"})
    found = Credentials(environ={ENV: " from-env \n", "CI": "true"}, keyring=ring)

    assert found.get(TOKENLEAN_KEY) == Credential("from-env", "env")
    assert ring.asked == 0


def test_env_wins_outside_ci_too():
    ring = MemoryKeyring({("agentic-sdlc", TOKENLEAN_KEY): "from-keyring"})
    assert Credentials(environ={ENV: "from-env"}, keyring=ring).get(TOKENLEAN_KEY).source == "env"


def test_the_keyring_is_used_otherwise():
    ring = MemoryKeyring({("agentic-sdlc", TOKENLEAN_KEY): "from-keyring"})
    found = Credentials(environ={ENV: "   "}, keyring=ring).get(TOKENLEAN_KEY)  # blank = unset
    assert found == Credential("from-keyring", "keyring")


def test_a_missing_key_gives_an_actionable_error():
    with pytest.raises(CredentialError) as raised:
        Credentials(environ={}, keyring=MemoryKeyring()).get(TOKENLEAN_KEY)
    message = str(raised.value)
    assert ENV in message and "agentic login" in message


def test_in_ci_a_missing_key_says_to_set_the_variable():
    ring = MemoryKeyring({("agentic-sdlc", TOKENLEAN_KEY): "from-keyring"})
    with pytest.raises(CredentialError, match=f"{ENV}.*CI"):
        Credentials(environ={"CI": "1"}, keyring=ring).get(TOKENLEAN_KEY)
    assert ring.asked == 0


@pytest.mark.parametrize(
    "marker", ["TF_BUILD", "JENKINS_URL", "BUILDKITE", "TEAMCITY_VERSION", "GITLAB_CI"]
)
def test_ci_systems_that_do_not_set_ci_are_recognised(marker):  # review
    ring = MemoryKeyring({("agentic-sdlc", TOKENLEAN_KEY): "from-keyring"})
    with pytest.raises(CredentialError, match="CI"):
        Credentials(environ={marker: "True"}, keyring=ring).get(TOKENLEAN_KEY)
    assert ring.asked == 0


@pytest.mark.parametrize("ci", ["", "0", "false", "False", "no"])
def test_a_false_ci_value_is_not_ci(ci):
    ring = MemoryKeyring({("agentic-sdlc", TOKENLEAN_KEY): "k"})
    assert Credentials(environ={"CI": ci}, keyring=ring).get(TOKENLEAN_KEY).source == "keyring"


@pytest.mark.parametrize("error", [NoKeyringError("no backend"), KeyringError("locked")])
def test_a_broken_keyring_gives_an_actionable_error(error):
    with pytest.raises(CredentialError) as raised:
        Credentials(environ={}, keyring=BrokenKeyring(error)).get(TOKENLEAN_KEY)
    message = str(raised.value)
    assert "keyring" in message and ENV in message


def test_find_returns_none_when_absent():
    assert Credentials(environ={}, keyring=MemoryKeyring()).find(TOKENLEAN_KEY) is None


def test_the_value_never_shows_in_repr():
    assert "s3cret" not in repr(Credential("s3cret", "env"))


def test_store_writes_to_the_keyring():
    ring = MemoryKeyring()
    Credentials(environ={}, keyring=ring).store(TOKENLEAN_KEY, "new-key")
    assert ring.stored == {("agentic-sdlc", TOKENLEAN_KEY): "new-key"}


def test_store_refuses_a_blank_value():
    with pytest.raises(CredentialError, match="empty"):
        Credentials(environ={}, keyring=MemoryKeyring()).store(TOKENLEAN_KEY, "  ")


@pytest.mark.parametrize("name", ["", "Has Space", "../x", "UPPER"])
def test_a_credential_name_is_a_lower_case_slug(name):
    with pytest.raises(ValueError, match="credential name"):
        env_var(name)
