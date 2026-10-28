"""Helpers for the CLI tests: temporary git repos, file snapshots and directory links."""

import os
import subprocess
from collections.abc import Mapping
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "core" / "tests" / "fixtures"
GIT = ["-c", "user.email=dev@example.invalid", "-c", "user.name=Dev", "-c", "commit.gpgsign=false"]


def git(where: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-C", str(where), *GIT, *args], capture_output=True, text=True, check=True
    )
    return done.stdout.strip()


def make_repo(path: Path, *, origin: str | None = "https://github.com/acme/app.git") -> Path:
    (path / "src").mkdir(parents=True)
    (path / "src" / "app.py").write_text("x = 1\n", encoding="utf-8")
    (path / "README.md").write_text("# app\n", encoding="utf-8")
    git(path, "init", "-q", "-b", "main")
    git(path, "add", "-A")
    git(path, "commit", "-qm", "base")
    if origin:
        git(path, "remote", "add", "origin", origin)
    return path


def commit_file(repo: Path, rel: str, text: str) -> None:
    target = repo / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    git(repo, "add", rel)
    git(repo, "commit", "-qm", f"add {rel}")


def git_bytes(where: Path, *args: str) -> bytes:
    done = subprocess.run(["git", "-C", str(where), *GIT, *args], capture_output=True, check=True)
    return done.stdout


def commit_bytes(repo: Path, rel: str, data: bytes) -> None:
    """Commit `data` exactly as given: no line-ending conversion on the way in."""
    target = repo / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    git(repo, "-c", "core.autocrlf=false", "add", rel)
    git(repo, "commit", "-qm", f"add {rel}")


def snapshot(*roots: Path) -> dict[str, bytes]:
    """Every file under the roots (path -> bytes), for 'nothing else was written' tests."""
    files: dict[str, bytes] = {}
    for root in roots:
        for path in root.rglob("*"):
            if path.is_file() and ".git" not in path.relative_to(root).parts:
                files[str(path)] = path.read_bytes()
    return files


def link_dir(link: Path, target: Path) -> None:
    """A directory link: a junction on Windows (no privilege needed), a symlink elsewhere."""
    if os.name == "nt":
        subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True,
            check=True,
        )
    else:
        link.symlink_to(target, target_is_directory=True)


def skip_unless_graph_required(reason: str, environ: Mapping[str, str] = os.environ) -> None:
    """Skip a domain-graph test for `reason`, or fail it where scripts/check.py sets
    AGENTIC_REQUIRE_ZYROFLO_GRAPH=1 (DF-463; as commons/tests/graph_ci.py)."""
    if environ.get("AGENTIC_REQUIRE_ZYROFLO_GRAPH", "").strip() == "1":
        pytest.fail(f"{reason} (AGENTIC_REQUIRE_ZYROFLO_GRAPH=1)")
    pytest.skip(reason)
