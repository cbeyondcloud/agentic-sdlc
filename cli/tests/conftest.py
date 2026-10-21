"""Shared fixtures for the CLI tests: a temporary AGENTIC_HOME and HOME, a temporary git
repo, and a copy of the sample org policy."""

import shutil
from pathlib import Path

import keyring
import pytest
from cli_helpers import FIXTURES, make_repo
from keyring.backend import KeyringBackend

CI_MARKERS = ("CI", "GITHUB_ACTIONS", "TF_BUILD", "JENKINS_URL", "GITLAB_CI", "BUILDKITE")
#: a proxy would route the local fake GitHub's 127.0.0.1 traffic elsewhere
NETWORK_VARS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")


class EmptyKeyring(KeyringBackend):
    """A keyring with nothing in it, so no test reads (or writes) the developer's own."""

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


@pytest.fixture(autouse=True)
def no_real_secrets(monkeypatch: pytest.MonkeyPatch):
    """Every CLI test: an empty keyring, no hosting token and no proxy from the machine."""
    previous = keyring.get_keyring()
    keyring.set_keyring(EmptyKeyring())
    monkeypatch.delenv("AGENTIC_GITHUB_TOKEN", raising=False)
    for name in NETWORK_VARS:
        monkeypatch.delenv(name, raising=False)
    yield
    keyring.set_keyring(previous)


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A private user home and AGENTIC_HOME, so nothing touches the real ones."""
    user = tmp_path / "home"
    user.mkdir()
    monkeypatch.setenv("HOME", str(user))
    monkeypatch.setenv("USERPROFILE", str(user))
    monkeypatch.setenv("AGENTIC_HOME", str(user / ".agentic"))
    for marker in CI_MARKERS:
        monkeypatch.delenv(marker, raising=False)
    monkeypatch.delenv("AGENTIC_TOKENLEAN_API_KEY", raising=False)
    return user


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return make_repo(tmp_path / "work" / "app")


@pytest.fixture
def policy_repo(tmp_path: Path) -> Path:
    """A policy repo folder (policy/) copied from the core fixtures."""
    root = tmp_path / "org-policy"
    shutil.copytree(FIXTURES / "policy" / "policy", root / "policy")
    return root
