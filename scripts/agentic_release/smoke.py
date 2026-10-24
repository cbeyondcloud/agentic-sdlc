"""Smoke-test a published release: install it from the index into a clean virtualenv and
use it the way a developer's first session does.

1. `uv venv` a new environment (nothing from this checkout is on its path).
2. Optionally (`deps_from_lock`) install the third-party dependencies exactly as uv.lock
   pins them, from uv's cache: for the tests, whose local index holds only the release.
   The org's index proxies PyPI, so a real smoke run resolves everything from it.
3. `uv pip install --index-url <index> agentic-cli==<version>`.
4. `agentic --version` must print the released version.
5. In a new git repo with its own AGENTIC_HOME and no AGENTIC_* settings from outside:
   `agentic init --standalone` installs the bundled template, the template's placeholder
   models are named, and `agentic run feature-delivery --dry-run` must pass.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

#: The template's placeholder model names (cli standalone.PLACEHOLDER).
PLACEHOLDER = "change-me"
CONFIG = "version: 1\nclassification: internal\npipelines: [feature-delivery]\n"
#: A stand-in gateway key: the dry run calls no model and no gateway.
SMOKE_KEY = "release-smoke-no-gateway"


class SmokeError(RuntimeError):
    """The published release does not install or run."""


def _run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", **kwargs)


def _checked(cmd: list[str], what: str, **kwargs) -> subprocess.CompletedProcess:
    done = _run(cmd, **kwargs)
    if done.returncode != 0:
        raise SmokeError(f"{what} failed ({done.returncode}):\n{done.stderr or done.stdout}")
    return done


def _bin(venv: Path, name: str) -> Path:
    if os.name == "nt":
        return venv / "Scripts" / f"{name}.exe"
    return venv / "bin" / name


def clean_env(home: Path) -> dict[str, str]:
    """This process's environment without any agentic or uv project setting, with
    AGENTIC_HOME at `home`."""
    dropped = ("AGENTIC_", "UV_PROJECT", "VIRTUAL_ENV", "PYTHONPATH", "UV_PUBLISH")
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith(dropped)}
    env["AGENTIC_HOME"] = str(home)
    env["AGENTIC_TOKENLEAN_API_KEY"] = SMOKE_KEY
    return env


def check_version(agentic: Path, version: str) -> None:
    done = _run([str(agentic), "--version"])
    shown = done.stdout.strip()
    if done.returncode != 0 or shown != f"agentic {version}":
        raise SmokeError(f"`agentic --version` printed {shown!r}, expected 'agentic {version}'")


def install(index_url: str, version: str, venv: Path, deps_from_lock: Path | None) -> Path:
    """A new venv with agentic-cli==version from the index; returns the agentic command."""
    _checked(["uv", "venv", str(venv), "--python", sys.executable, "-q"], "uv venv")
    if deps_from_lock is not None:
        env = dict(os.environ, UV_PROJECT_ENVIRONMENT=str(venv))
        env.pop("VIRTUAL_ENV", None)
        sync = ["uv", "sync", "--frozen", "--offline", "--no-dev", "--package", "agentic-cli"]
        _checked([*sync, "--no-install-workspace", "-q"], "uv sync", cwd=deps_from_lock, env=env)
    # --no-config: no project's or user's uv settings (such as this repo's exclude-newer
    # window, which a just-published release is inside of) change what gets installed
    pip = ["uv", "pip", "install", "--no-config", "--python", str(_bin(venv, "python"))]
    pip += ["--no-cache", "--index-url", index_url, f"agentic-cli=={version}", "-q"]
    _checked(pip, "uv pip install", cwd=venv.parent)
    return _bin(venv, "agentic")


def _wheel_install(venv: Path) -> bool:
    """Whether the installed CLI is the wheel (with its bundle), not a checkout."""
    found = list(venv.glob("**/site-packages/agentic_cli/_framework/templates/standalone"))
    return bool(found)


def _git(repo: Path, *args: str) -> None:
    _checked(["git", *args], f"git {args[0]}", cwd=repo)


def dry_run(agentic: Path, workdir: Path) -> tuple[dict, Path]:
    """`init --standalone` then `run feature-delivery --dry-run` in a new repo; returns
    the dry run's plan and the installed standalone policy folder."""
    home, repo = workdir / "home", workdir / "repo"
    (repo / ".agentic").mkdir(parents=True)
    env = clean_env(home)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "release-smoke@example.invalid")
    _git(repo, "config", "user.name", "Release smoke")
    _git(repo, "config", "commit.gpgsign", "false")
    (repo / ".agentic" / "config.yaml").write_text(CONFIG, encoding="utf-8", newline="\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "agentic: repo config")
    first = _run([str(agentic), "--json", "init", "--standalone"], cwd=repo, env=env)
    policy = home / "policy" / "standalone"
    tiers = policy / "policy" / "tiers.yaml"
    if not tiers.is_file():
        raise SmokeError(f"init did not install the bundled template:\n{first.stderr}")
    # what the standalone README asks a person to do: name models (no model is called)
    text = tiers.read_text(encoding="utf-8").replace(PLACEHOLDER, "smoke-model")
    tiers.write_text(text, encoding="utf-8", newline="\n")
    org = policy / "policy" / "org.yaml"
    text = org.read_text(encoding="utf-8")
    text = text.replace("required_for: [team, regulated]", "required_for: []")
    org.write_text(text, encoding="utf-8", newline="\n")
    _run([str(agentic), "--json", "init", "--standalone"], cwd=repo, env=env)
    run = [str(agentic), "--json", "run", "feature-delivery", "--dry-run"]
    done = _run([*run, "--input", "request=release smoke"], cwd=repo, env=env)
    if done.returncode != 0:
        raise SmokeError(f"the dry run exited {done.returncode}:\n{done.stdout}\n{done.stderr}")
    return json.loads(done.stdout), policy


def smoke(
    index_url: str, version: str, workdir: Path, *, deps_from_lock: Path | None = None
) -> dict:
    """Install `version` from `index_url` into a clean venv under `workdir` and use it;
    returns what was checked. SmokeError when anything fails."""
    workdir.mkdir(parents=True, exist_ok=True)
    venv = workdir / "venv"
    agentic = install(index_url, version, venv, deps_from_lock)
    check_version(agentic, version)
    plan, policy = dry_run(agentic, workdir)
    return {
        "version": version,
        "installed_from_wheel": _wheel_install(venv),
        "standalone_policy": str(policy),
        "dry_run": plan,
    }
