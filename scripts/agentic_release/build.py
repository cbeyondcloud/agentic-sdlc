"""Reproducible release builds of the four workspace packages.

Each package is built from a staged copy holding only its committed files (`git ls-files`,
so a stray untracked file never ships), plus the repo's LICENSE and THIRD_PARTY_NOTICES.md
as the wheel's licence files. The CLI's copy also gets the standalone template and the
default library under `agentic_cli/_framework/`, so an installed CLI needs no checkout
(DF-138, DF-188, DF-341). `uv build` (the uv_build backend) then writes archives with fixed
timestamps and sorted entries: two builds of one commit are byte-identical, and with
`.gitattributes eol=lf` a Windows and a Linux build agree too.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import tomllib
from dataclasses import dataclass
from pathlib import Path

from agentic_release.envs import scrubbed_env
from agentic_release.version import MEMBERS, check_versions

#: The repo files every wheel carries as its licence files.
LICENCE_FILES = ("LICENSE", "THIRD_PARTY_NOTICES.md")
#: What the CLI wheel bundles, under agentic_cli/_framework/ (the paths kept as they are).
BUNDLE = ("templates/standalone", "library/agents", "library/pipelines", "library/skills")
BUNDLE_DEST = Path("src") / "agentic_cli" / "_framework"
#: The parts of a member a build reads.
MEMBER_PARTS = ("pyproject.toml", "src")


class BuildError(RuntimeError):
    """The release cannot be built from this tree."""


def _git(repo: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, encoding="utf-8"
    )
    if done.returncode != 0:
        raise BuildError(f"git {' '.join(args)} failed in {repo}: {done.stderr.strip()}")
    return done.stdout


def tracked_files(repo: Path, folder: str) -> list[str]:
    """The committed files under `folder` (POSIX paths relative to `repo`), as they are in
    the working tree; deleted ones are left out."""
    listing = _git(repo, "ls-files", "--cached", "-z", "--", folder)
    return sorted(name for name in listing.split("\0") if name and (repo / name).is_file())


def dirty(repo: Path) -> bool:
    """Whether a committed file has uncommitted changes (untracked files never ship)."""
    return bool(_git(repo, "status", "--porcelain", "--untracked-files=no").strip())


def commit_epoch(repo: Path) -> int:
    """The HEAD commit's time: the release's SOURCE_DATE_EPOCH."""
    return int(_git(repo, "log", "-1", "--format=%ct").strip())


def _copy_tracked(repo: Path, folder: str, dest: Path, strip: str = "") -> None:
    for name in tracked_files(repo, folder):
        target = dest / name[len(strip) :] if strip else dest / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(repo / name, target)


def stage(repo: Path, member: str, dest: Path) -> Path:
    """A buildable copy of `member` at `dest`: its committed pyproject and src, the
    licence files declared in its metadata, and (the CLI) the framework bundle."""
    for part in MEMBER_PARTS:
        _copy_tracked(repo, f"{member}/{part}", dest, strip=f"{member}/")
    pyproject = dest / "pyproject.toml"
    text = pyproject.read_text(encoding="utf-8")
    if '\nlicense = "' not in text:
        raise BuildError(f"{member}/pyproject.toml: no `license = ...` line in [project]")
    files = ", ".join(f'"{name}"' for name in LICENCE_FILES)
    line = next(ln for ln in text.splitlines() if ln.startswith("license = "))
    text = text.replace(line, f"{line}\nlicense-files = [{files}]", 1)
    pyproject.write_text(text, encoding="utf-8", newline="\n")
    for name in LICENCE_FILES:
        if name not in tracked_files(repo, name):
            raise BuildError(f"{name} is not committed at the repo root")
        shutil.copyfile(repo / name, dest / name)
    if member == "cli":
        for folder in BUNDLE:
            files_in = tracked_files(repo, folder)
            if not files_in:
                raise BuildError(f"{folder}: nothing committed to bundle into the CLI")
            for name in files_in:
                target = dest / BUNDLE_DEST / name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(repo / name, target)
    return dest


def build_packages(repo: Path, out: Path, *, allow_dirty: bool = False) -> list[Path]:
    """Build every member's sdist and wheel into `out`; returns the files, sorted by name.

    Refuses version drift (see `version.check_versions`) and, unless `allow_dirty`,
    uncommitted changes to committed files. `out` must not hold anything else."""
    problems = check_versions(repo)
    if problems:
        raise BuildError("the versions disagree:\n  " + "\n  ".join(problems))
    if dirty(repo) and not allow_dirty:
        raise BuildError(
            "the tree has uncommitted changes: commit them, or pass --allow-dirty for a "
            "build that is not a release"
        )
    if out.exists() and any(out.iterdir()):
        raise BuildError(f"{out} is not empty: give an empty or new folder")
    env = scrubbed_env(SOURCE_DATE_EPOCH=str(commit_epoch(repo)))
    with tempfile.TemporaryDirectory(prefix="agentic-release-") as scratch:
        built = Path(scratch) / "dist"
        for member in MEMBERS:
            staged = stage(repo, member, Path(scratch) / "stage" / member)
            _uv_build(staged, built, env)
        out.mkdir(parents=True, exist_ok=True)
        for path in sorted(built.iterdir()):
            shutil.copyfile(path, out / path.name)
    return sorted(out.iterdir(), key=lambda p: p.name)


@dataclass(frozen=True)
class GitSource:
    """A locked package that comes from a git repository, not an index."""

    name: str
    version: str
    url: str
    commit: str


def git_sources(lock: Path) -> list[GitSource]:
    """The git-sourced packages in `lock` (uv.lock), with the commit each is pinned to."""
    data = tomllib.loads(lock.read_text(encoding="utf-8"))
    found = []
    for package in data.get("package", []):
        source = package.get("source", {}).get("git")
        if not source:
            continue
        url, _, commit = source.partition("#")
        found.append(
            GitSource(package["name"], str(package["version"]), url.split("?", 1)[0], commit)
        )
    return sorted(found, key=lambda s: s.name)


def build_git_sources(
    lock: Path, out: Path, workdir: Path, url_map: dict[str, str] | None = None
) -> list[Path]:
    """Build every git-sourced package of `lock` at its locked commit into `out`: the org's
    internal index needs them (they are on no public index). `url_map` points a locked URL
    at a mirror. Git never prompts: a host that wants credentials fails the build."""
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    built: list[Path] = []
    for source in git_sources(lock):
        checkout = workdir / source.name
        url = (url_map or {}).get(source.url, source.url)
        # the files exactly as committed, whatever this machine's autocrlf says
        clone = ["clone", "--quiet", "--no-checkout", "--config", "core.autocrlf=false"]
        _run_git([*clone, url, str(checkout)], workdir, env, "clone")
        _run_git(["checkout", "--quiet", "--detach", source.commit], checkout, env, "checkout")
        found = tomllib.loads((checkout / "pyproject.toml").read_text(encoding="utf-8"))
        version = str(found.get("project", {}).get("version", ""))
        if version != source.version:
            raise BuildError(
                f"{source.name} at {source.commit[:12]} is version {version}, but uv.lock "
                f"says {source.version}: run `uv lock` and commit it"
            )
        target = workdir / f"{source.name}-dist"
        # the dependency's own build backend runs here: no release secret reaches it
        _uv_build(checkout, target, scrubbed_env())
        out.mkdir(parents=True, exist_ok=True)
        for path in sorted(target.iterdir()):
            shutil.copyfile(path, out / path.name)
            built.append(out / path.name)
    return sorted(built, key=lambda p: p.name)


def _run_git(args: list[str], cwd: Path, env: dict[str, str], what: str) -> None:
    cwd.mkdir(parents=True, exist_ok=True)
    done = subprocess.run(
        ["git", *args], cwd=cwd, env=env, capture_output=True, text=True, encoding="utf-8"
    )
    if done.returncode != 0:
        raise BuildError(f"git {what} failed: {done.stderr.strip()}")


def _uv_build(source: Path, out: Path, env: dict[str, str]) -> None:
    """`uv build` of one staged project: the sdist, then the wheel built from it. `env`
    comes from `scrubbed_env`: the build backend is index code."""
    cmd = ["uv", "build", str(source), "--out-dir", str(out), "--no-create-gitignore", "-q"]
    done = subprocess.run(cmd, env=env, capture_output=True, text=True, encoding="utf-8")
    if done.returncode != 0:
        raise BuildError(f"uv build failed for {source.name}:\n{done.stderr.strip()}")
