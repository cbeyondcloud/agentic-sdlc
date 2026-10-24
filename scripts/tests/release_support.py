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


def _copy(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if source.is_dir():
        shutil.copytree(source, target, ignore=_JUNK)
    else:
        shutil.copy2(source, target)
