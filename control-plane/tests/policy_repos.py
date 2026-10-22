"""Local git policy repos for the policy-sync tests: real git, `file://` URLs (the tests'
settings allow local repos, ruling R12), commits with a fixed identity and LF bytes."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
TEMPLATE_POLICY = REPO_ROOT / "templates" / "standalone" / "policy"
_GIT = [
    "git",
    "-c",
    "core.autocrlf=false",
    "-c",
    "core.fsmonitor=false",
    "-c",
    "user.name=Policy Admin",
    "-c",
    "user.email=admin@example.test",
    "-c",
    "commit.gpgsign=false",
    "-c",
    "init.defaultBranch=main",
]


def git(repo: Path, *args: str) -> str:
    done = subprocess.run(
        [*_GIT, *args],
        cwd=repo,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )
    if done.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {done.stderr.strip()}")
    return done.stdout.strip()


def write(repo: Path, rel: str, text: str) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))


def make_policy_repo(path: Path, *, teams: tuple[str, ...] = ("payments",)) -> Path:
    """A working repo holding the standalone template's `policy/` plus one
    `policy/teams/<team>.yaml` per team, committed on `main`."""
    path.mkdir(parents=True)
    git(path, "init", "-q")
    for source in sorted(TEMPLATE_POLICY.iterdir()):
        if source.is_file():
            data = source.read_bytes().replace(b"\r\n", b"\n")
            (path / "policy").mkdir(exist_ok=True)
            (path / "policy" / source.name).write_bytes(data)
    for team in teams:
        write(path, f"policy/teams/{team}.yaml", "{}\n")
    commit(path, "initial policy")
    return path


def commit(repo: Path, message: str) -> str:
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "--allow-empty", "-m", message)
    return git(repo, "rev-parse", "HEAD")


def head(repo: Path) -> str:
    return git(repo, "rev-parse", "HEAD")


def url(repo: Path) -> str:
    return repo.resolve().as_uri()


def break_tiers(repo: Path) -> str:
    """Commit a `tiers.yaml` that does not parse as policy."""
    write(repo, "policy/tiers.yaml", "tiers: [this is not a mapping\n")
    return commit(repo, "break tiers")


def remove(path: Path) -> None:
    def writable(func, target, _error):
        os.chmod(target, 0o700)
        func(target)

    shutil.rmtree(path, onexc=writable)
