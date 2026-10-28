"""Helpers for the release tests (M3-21): a committed copy of what a release builds from,
and a throwaway signing key. Nothing here calls the network."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
#: Root files and folders a release reads (members only as far as a build needs them).
COPIED = (
    "pyproject.toml",
    "uv.lock",
    "LICENSE",
    "THIRD_PARTY_NOTICES.md",
    ".gitattributes",
    "templates/standalone",
    "library/agents",
    "library/pipelines",
    "library/skills",
)
MEMBER_PARTS = ("pyproject.toml", "src")
MEMBERS = ("core", "commons", "cli", "control-plane")
_JUNK = shutil.ignore_patterns("__pycache__", "*.pyc", "*.egg-info", ".pytest_cache")


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout


def make_release_repo(dest: Path) -> Path:
    """A git repo at `dest` holding the files a release builds from, committed once."""
    dest.mkdir(parents=True)
    for rel in COPIED:
        _copy(REPO / rel, dest / rel)
    for member in MEMBERS:
        for part in MEMBER_PARTS:
            _copy(REPO / member / part, dest / member / part)
    git(dest, "init", "-q", "-b", "main")
    git(dest, "config", "user.email", "release@example.invalid")
    git(dest, "config", "user.name", "Release")
    git(dest, "config", "commit.gpgsign", "false")
    git(dest, "config", "core.autocrlf", "false")
    git(dest, "add", "-A")
    git(dest, "commit", "-q", "-m", "release: the tree a release builds from")
    return dest


IDENTITY = "release@example.invalid"


def new_key(folder: Path, name: str = "release") -> tuple[Path, str]:
    """A throwaway OpenSSH Ed25519 private key file and its public key line."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    key = Ed25519PrivateKey.generate()
    path = folder / f"{name}_ed25519"
    path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.OpenSSH,
            serialization.NoEncryption(),
        )
    )
    public = key.public_key().public_bytes(
        serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH
    )
    return path, public.decode()


def allowed(public: str, identity: str = IDENTITY, options: str = "") -> str:
    """An allowed_signers file naming `public` for `identity`."""
    return f"# release signers\n{identity} {options + ' ' if options else ''}{public} ci\n"


#: Settings a release shell holds that no build backend, installer or installed CLI may
#: see: index, signing and git credentials, and paths that would load a checkout.
PLANTED = (
    "UV_PUBLISH_PASSWORD",
    "UV_PUBLISH_TOKEN",
    "AGENTIC_RELEASE_SIGNING_KEY",
    "AGENTIC_RELEASE_SIGNING_KEY_PASSWORD",
    "GIT_TOKEN",
    "GH_TOKEN",
    "PYTHONPATH",
    "VIRTUAL_ENV",
)


def plant(monkeypatch) -> None:
    """Set every PLANTED variable, as a release shell or CI step would."""
    for name in PLANTED:
        monkeypatch.setenv(name, "planted-by-the-test")


def leaked(env: dict[str, str] | None) -> set[str]:
    """The PLANTED variables a subprocess was given (None means all of os.environ)."""
    assert env is not None, "the subprocess inherits the whole environment"
    return {name.upper() for name in env} & set(PLANTED)


def _copy(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if source.is_dir():
        shutil.copytree(source, target, ignore=_JUNK)
    else:
        shutil.copy2(source, target)
