"""Shared fixtures for the CLI tests: a temporary AGENTIC_HOME and HOME, a temporary git
repo, and a copy of the sample org policy."""

import shutil
from pathlib import Path

import pytest
from cli_helpers import FIXTURES, make_repo

CI_MARKERS = ("CI", "GITHUB_ACTIONS", "TF_BUILD", "JENKINS_URL", "GITLAB_CI", "BUILDKITE")


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
