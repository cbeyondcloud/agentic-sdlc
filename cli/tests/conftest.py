"""CLI test fixtures; the helpers live in _cli_support (a unique module name, so they never
collide with another folder's conftest)."""

from pathlib import Path

import pytest
from _cli_support import make_policy_repo, make_repo, runner


@pytest.fixture
def policy_repo(tmp_path: Path) -> Path:
    return make_policy_repo(tmp_path / "org-policy")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return make_repo(tmp_path / "repo")


@pytest.fixture
def workspace(tmp_path: Path, repo: Path, policy_repo: Path, monkeypatch) -> Path:
    """A target repository as the current folder, with the org policy repo, team and user
    configured through the environment (never the developer's real ~/.agentic)."""
    monkeypatch.chdir(repo)
    monkeypatch.setenv("AGENTIC_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("AGENTIC_POLICY_REPO", str(policy_repo))
    monkeypatch.setenv("AGENTIC_TEAM", "payments")
    for name in ("AGENTIC_USER", "AGENTIC_GATEWAY_URL", "AGENTIC_TOKENLEAN_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    return repo


@pytest.fixture
def cli(capsys):
    """cli(*args) -> Result(code, out, err): runs `agentic` in this process."""
    return runner(capsys)
