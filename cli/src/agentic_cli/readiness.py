"""The readiness report `agentic init` prints: every check, what it found, and a fix for
each one that fails (M1-53, spec §8.1 step 6, §24).

An item's status is one of:

  * `ok`      - fine;
  * `missing` - init is not ready until it is fixed (exit 1 after the report);
  * `warn`    - works with a limitation, which the detail and fix explain;
  * `info`    - for the person to know; never blocks (e.g. the Docker Desktop licence).

`prerequisites` checks this machine: Python, git (2.40+ for worktrees), the sandbox
(`agentic.sandbox.readiness`, the org's pinned `srt`, D38), a container engine (with a
Docker Desktop licence note, DF-105), Git Bash on Windows (DF-73), and the credential.
Nothing here installs anything.
"""

import platform as _platform
import re
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

from rich.console import Console

from agentic.auth import TOKENLEAN_KEY, CredentialError, Credentials, env_var
from agentic.policy import OrgPolicy
from agentic.sandbox.readiness import readiness

Status = Literal["ok", "missing", "warn", "info"]
GIT_MIN = (2, 40)
GIT_BASH_DEFAULT = Path("C:/Program Files/Git/bin/bash.exe")
DOCKER_DESKTOP_NOTE = (
    "Docker Desktop needs a paid licence in organisations with more than 250 employees or "
    "USD 10 million revenue; check your org's licence, or use Podman, Rancher Desktop or "
    "Docker Engine inside WSL2/Linux"
)
_MARK = {
    "ok": "[green]ok[/green]",
    "missing": "[red]missing[/red]",
    "warn": "[yellow]warn[/yellow]",
}


@dataclass(frozen=True)
class Item:
    name: str
    status: Status
    detail: str
    fix: str = ""


@dataclass
class Report:
    items: list[Item]

    @property
    def ready(self) -> bool:
        return all(item.status != "missing" for item in self.items)

    def to_json(self) -> dict:
        return {"ready": self.ready, "checks": [asdict(item) for item in self.items]}

    def show(self, console: Console) -> None:
        head = "[green]ready[/green]" if self.ready else "[red]NOT ready[/red]"
        console.print(f"Readiness: {head}")
        for item in self.items:
            mark = _MARK.get(item.status, "[blue]info[/blue]")
            console.print(f"  [{mark}] {item.name}: ", end="")
            console.print(item.detail, markup=False)
            if item.fix and item.status != "ok":
                for line in item.fix.splitlines():
                    console.print(f"      {line}", markup=False)


def _run(argv: list[str]) -> tuple[int, str]:
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as error:
        return 1, str(error)
    return done.returncode, (done.stdout + done.stderr).strip()


def prerequisites(
    *,
    policy: OrgPolicy,
    which: Callable[[str], str | None] = shutil.which,
    run: Callable[[list[str]], tuple[int, str]] = _run,
    system: str | None = None,
    kernel_release: str | None = None,
    credentials: Credentials | None = None,
    tree: Path | None = None,
) -> list[Item]:
    system = system if system is not None else _platform.system()
    items = [_python(), _git(which, run)]
    items += _sandbox(policy, which, run, system, kernel_release, tree)
    items.append(_container_engine(which, run))
    if system == "Windows":
        items.append(_git_bash(which))
    items.append(_credential(credentials or Credentials()))
    return items


def _python() -> Item:
    version = ".".join(map(str, sys.version_info[:3]))
    ok = sys.version_info >= (3, 12)
    return Item("python", "ok" if ok else "missing", version, "install Python 3.12")


def _git(which, run) -> Item:
    fix = "install git 2.40 or later (git-scm.com, or your package manager)"
    if which("git") is None:
        return Item("git", "missing", "not found", fix)
    code, out = run(["git", "--version"])
    found = re.search(r"(\d+)\.(\d+)", out)
    if code != 0 or not found:
        return Item("git", "missing", f"cannot read its version ({out[:60]})", fix)
    version = (int(found[1]), int(found[2]))
    return Item("git", "ok" if version >= GIT_MIN else "missing", out.strip(), fix)


def _sandbox(policy, which, run, system, kernel_release, tree) -> list[Item]:
    report = readiness(
        system=system,
        kernel_release=kernel_release,
        which=which,
        run=run,
        tree=tree,
        srt_version=policy.sandbox.srt_version,
    )
    # Without a sandbox, solo runs go ahead on policy guards (with a warning) and the
    # regimes in sandbox.required_for refuse; it blocks init only if solo needs one too.
    failing: Status = "missing" if "solo" in policy.sandbox.required_for else "warn"
    refusing = ", ".join(policy.sandbox.required_for) or "no"
    items = []
    for check in report.checks:
        name = f"sandbox: {check.name}"
        if check.ok:
            items.append(Item(name, "ok", check.detail, check.fix))
        else:
            detail = f"{check.detail} ({refusing} runs refuse without it)"
            items.append(Item(name, failing, detail, check.fix))
    return items


def _container_engine(which, run) -> Item:
    if which("docker") is not None:
        code, out = run(["docker", "version", "--format", "{{.Server.Platform.Name}}"])
        name = out.strip().splitlines()[0] if code == 0 and out.strip() else "docker"
        detail = f"docker ({name})"
        if "desktop" in name.lower():
            detail += f". Licence note: {DOCKER_DESKTOP_NOTE}"
        return Item("container engine", "info", detail)
    if which("podman") is not None:
        code, out = run(["podman", "--version"])
        return Item("container engine", "info", out.strip() if code == 0 else "podman")
    return Item("container engine", "info", "none found (only container-based tools need one)")


def _git_bash(which) -> Item:
    found = which("bash")
    if found is None and GIT_BASH_DEFAULT.is_file():
        found = str(GIT_BASH_DEFAULT)
    fix = (
        "install Git for Windows (it includes Git Bash): backends that delegate a step to a "
        "command-line coding agent run its shell commands through bash"
    )
    return Item("git bash", "ok" if found else "warn", found or "not found", fix)


def _credential(credentials: Credentials) -> Item:
    fix = f"run `agentic login`, or set {env_var(TOKENLEAN_KEY)}"
    try:
        found = credentials.find(TOKENLEAN_KEY)
    except CredentialError as error:
        return Item("credential", "missing", str(error), fix)
    if found is None:
        return Item("credential", "missing", "no gateway key", fix)
    return Item("credential", "ok", f"gateway key from the {found.source}")
