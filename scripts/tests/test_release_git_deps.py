"""Git-sourced internal dependencies (M3-21): Zyroflo's sdlc-harness is not on any public
index, so a release builds it from the commit uv.lock pins and publishes it with the rest."""

import subprocess
import sys
import zipfile
from pathlib import Path

import pytest
from release_support import REPO, git, leaked, plant

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agentic_release import build  # noqa: E402

FAKE_URL = "https://git.example.invalid/acme/fake-harness"
PYPROJECT = """[project]
name = "fake-harness"
version = "{version}"
requires-python = ">=3.12"

[build-system]
requires = ["uv_build>=0.12,<0.13"]
build-backend = "uv_build"
"""


def commit_version(repo: Path, version: str) -> str:
    pyproject = repo / "pyproject.toml"
    pyproject.write_text(PYPROJECT.format(version=version), encoding="utf-8", newline="\n")
    module = repo / "src" / "fake_harness"
    module.mkdir(parents=True, exist_ok=True)
    (module / "__init__.py").write_bytes(f'RELEASE = "{version}"\n'.encode())
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", f"release {version}")
    return git(repo, "rev-parse", "HEAD").strip()


@pytest.fixture
def upstream(tmp_path: Path) -> tuple[Path, str]:
    """A fake harness repo whose 0.3.0 commit the lock pins; HEAD has moved on to 0.4.0."""
    repo = tmp_path / "fake-harness"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "dev@example.invalid")
    git(repo, "config", "user.name", "Dev")
    git(repo, "config", "commit.gpgsign", "false")
    pinned = commit_version(repo, "0.3.0")
    commit_version(repo, "0.4.0")
    return repo, pinned


def lock(tmp_path: Path, commit: str, version: str = "0.3.0") -> Path:
    path = tmp_path / "uv.lock"
    path.write_text(
        "version = 1\n\n[[package]]\n"
        f'name = "fake-harness"\nversion = "{version}"\n'
        f'source = {{ git = "{FAKE_URL}?rev={commit}#{commit}" }}\n\n'
        '[[package]]\nname = "httpx"\nversion = "1.0.0"\n'
        'source = { registry = "https://pypi.org/simple" }\n',
        encoding="utf-8",
    )
    return path


def test_the_real_lock_names_sdlc_harness_at_a_commit():
    found = {s.name: s for s in build.git_sources(REPO / "uv.lock")}
    assert set(found) == {"sdlc-harness"}
    assert found["sdlc-harness"].url == "https://github.com/cbeyondcloud/sdlc-harness"
    assert len(found["sdlc-harness"].commit) == 40


def test_builds_the_locked_commit_not_the_branch_head(upstream, tmp_path: Path):
    repo, pinned = upstream
    out = tmp_path / "out"
    files = build.build_git_sources(
        lock(tmp_path, pinned), out, tmp_path / "work", url_map={FAKE_URL: str(repo)}
    )
    assert sorted(f.name for f in files) == [
        "fake_harness-0.3.0-py3-none-any.whl",
        "fake_harness-0.3.0.tar.gz",
    ]
    with zipfile.ZipFile(out / "fake_harness-0.3.0-py3-none-any.whl") as archive:
        assert archive.read("fake_harness/__init__.py") == b'RELEASE = "0.3.0"\n'


def test_a_version_that_differs_from_the_lock_is_refused(upstream, tmp_path: Path):
    repo, pinned = upstream
    with pytest.raises(build.BuildError, match="0.9.0"):
        build.build_git_sources(
            lock(tmp_path, pinned, version="0.9.0"),
            tmp_path / "out",
            tmp_path / "work",
            url_map={FAKE_URL: str(repo)},
        )


def test_an_unknown_commit_is_refused(upstream, tmp_path: Path):
    repo, _ = upstream
    with pytest.raises(build.BuildError, match="checkout"):
        build.build_git_sources(
            lock(tmp_path, "0" * 40),
            tmp_path / "out",
            tmp_path / "work",
            url_map={FAKE_URL: str(repo)},
        )


def test_git_is_given_no_prompt(monkeypatch, upstream, tmp_path: Path):
    """A release in CI must fail, not hang, when the git host asks for credentials."""
    seen = []
    real = subprocess.run

    def spy(cmd, **kwargs):
        if cmd[0] == "git":
            seen.append(kwargs.get("env", {}).get("GIT_TERMINAL_PROMPT"))
        return real(cmd, **kwargs)

    monkeypatch.setattr(build.subprocess, "run", spy)
    repo, pinned = upstream
    build.build_git_sources(
        lock(tmp_path, pinned),
        tmp_path / "out",
        tmp_path / "work",
        url_map={FAKE_URL: str(repo)},
    )
    assert seen and set(seen) == {"0"}


def test_the_build_backend_never_sees_release_secrets(monkeypatch, upstream, tmp_path: Path):
    """`uv build` runs the dependency's own build backend and build requirements: no index,
    signing or git credential, and no path to a checkout, may reach it."""
    plant(monkeypatch)
    seen = []
    real = subprocess.run

    def spy(cmd, **kwargs):
        if cmd[0] == "uv":
            seen.append(leaked(kwargs.get("env")))
        return real(cmd, **kwargs)

    monkeypatch.setattr(build.subprocess, "run", spy)
    repo, pinned = upstream
    build.build_git_sources(
        lock(tmp_path, pinned),
        tmp_path / "out",
        tmp_path / "work",
        url_map={FAKE_URL: str(repo)},
    )
    assert seen and all(not names for names in seen), seen
