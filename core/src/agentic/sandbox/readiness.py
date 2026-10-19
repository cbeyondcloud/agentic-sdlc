"""Is this machine ready to sandbox agent commands - and if not, exactly what to do.

`readiness()` checks the prerequisites of `srt` for the OS it runs on (D38, spike
S6) and ends with a smoke test: `srt` really starting a sandbox, which catches a
kernel that refuses unprivileged user namespaces. `agentic init` and `doctor`
print its report (M1-53, M3-09). Native Windows has no supported sandbox: the
report says how to run the framework inside WSL2 instead.

`decide_sandbox()` turns the report and the run's regime into a decision: use
`srt`, refuse to run (a regime in `sandbox.required_for`), or run on policy guards
only with a warning.
"""

import platform as _platform
import re
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from agentic.policy import SandboxPolicy
from agentic.sandbox.rules import SandboxRules
from agentic.sandbox.srt import SRT_VERSION, SrtSandbox

Platform = Literal["macos", "linux", "wsl", "windows", "other"]
NODE_MIN = (20, 11)

WINDOWS_GUIDANCE = """\
Native Windows has no supported sandbox (D38). Run agentic inside WSL2:
  1. In PowerShell: wsl --install -d Ubuntu-24.04
  2. In Ubuntu: sudo apt update && sudo apt install -y bubblewrap socat ripgrep nodejs npm
  3. In Ubuntu: sudo npm install -g @anthropic-ai/sandbox-runtime@0.0.78
  4. Ubuntu 24.04+: sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0
  5. Install agentic inside Ubuntu and clone repos to the WSL disk (~/src), not /mnt/c"""

USERNS_FIX = (
    "srt could not start a sandbox. On Ubuntu 24.04+ allow unprivileged user namespaces: "
    "sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0"
)


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str
    fix: str = ""


@dataclass(frozen=True)
class Readiness:
    platform: Platform
    checks: tuple[Check, ...]

    @property
    def available(self) -> bool:
        return self.platform in ("macos", "linux", "wsl") and all(c.ok for c in self.checks)

    def text(self) -> str:
        head = "ready" if self.available else "NOT ready"
        lines = [f"Sandbox ({self.platform}): {head}"]
        for check in self.checks:
            lines.append(f"  [{'ok' if check.ok else 'missing'}] {check.name}: {check.detail}")
            if not check.ok and check.fix:
                lines += [f"      {line}" for line in check.fix.splitlines()]
        return "\n".join(lines)


@dataclass(frozen=True)
class SandboxDecision:
    use_srt: bool
    label: str  # recorded on the run: "srt 0.0.78" or "none"
    refusal: str | None  # the run must not start
    warning: str | None  # the run starts on policy guards only


def _run(argv: list[str]) -> tuple[int, str]:
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as error:
        return 1, str(error)
    return done.returncode, (done.stdout + done.stderr).strip()


def readiness(
    *,
    system: str | None = None,
    kernel_release: str | None = None,
    which: Callable[[str], str | None] = shutil.which,
    run: Callable[[list[str]], tuple[int, str]] = _run,
    tree: Path | None = None,
    srt_version: str = SRT_VERSION,
) -> Readiness:
    """The sandbox prerequisites of this machine (or of the one described)."""
    system = system if system is not None else _platform.system()
    release = kernel_release if kernel_release is not None else _platform.release()
    kind = _platform_of(system, release)
    if kind == "windows":
        return Readiness("windows", (Check("sandbox", False, "native Windows", WINDOWS_GUIDANCE),))
    if kind == "other":
        fix = "agentic sandboxes commands on macOS, Linux and WSL2 only"
        return Readiness("other", (Check("sandbox", False, f"unsupported OS {system}", fix),))

    apt = kind in ("linux", "wsl")
    checks = [_node(which, run, apt), _srt(which, run, srt_version, apt)]
    rg_fix = "sudo apt install -y ripgrep" if apt else "brew install ripgrep"
    checks.append(_tool(which, "rg", "ripgrep", rg_fix))
    if apt:
        checks.append(_tool(which, "bwrap", "bubblewrap", "sudo apt install -y bubblewrap"))
        checks.append(_tool(which, "socat", "socat", "sudo apt install -y socat"))
    checks.append(_smoke(which, run, ready=all(c.ok for c in checks[:2])))
    if kind == "wsl" and tree is not None:
        on_windows_disk = Path(tree).as_posix().startswith("/mnt/")
        checks.append(
            Check(
                "tree location",
                not on_windows_disk,
                str(tree),
                "clone the repo into the WSL disk (e.g. ~/src), not /mnt/<drive>: Windows "
                "drives are slow and their permissions do not hold inside the sandbox",
            )
        )
    return Readiness(kind, tuple(checks))


def decide_sandbox(ready: Readiness, regime: str, policy: SandboxPolicy) -> SandboxDecision:
    """Use srt when the machine is ready; otherwise refuse or warn, by regime."""
    if ready.available:
        return SandboxDecision(True, f"srt {policy.srt_version}", None, None)
    if regime in policy.required_for:
        refusal = (
            f"the {regime!r} regime needs an OS sandbox and this machine has none ready "
            f"(org policy sandbox.required_for):\n{ready.text()}"
        )
        return SandboxDecision(False, "none", refusal, None)
    warning = (
        f"no OS sandbox on this machine: the {regime!r} run's commands are checked by "
        f"policy guards only.\n{ready.text()}"
    )
    return SandboxDecision(False, "none", None, warning)


def _platform_of(system: str, release: str) -> Platform:
    if system == "Darwin":
        return "macos"
    if system == "Linux":
        return "wsl" if "microsoft" in release.lower() else "linux"
    if system == "Windows":
        return "windows"
    return "other"


def _node(which, run, apt: bool) -> Check:
    fix = "install Node.js 20.11 or later (" + (
        "sudo apt install -y nodejs npm, or NodeSource for a newer one)"
        if apt
        else "brew install node)"
    )
    if which("node") is None:
        return Check("node", False, "not found", fix)
    code, out = run(["node", "--version"])
    found = re.match(r"v?(\d+)\.(\d+)", out.strip())
    if code != 0 or not found:
        return Check("node", False, f"cannot read its version ({out[:80]})", fix)
    version = (int(found[1]), int(found[2]))
    return Check("node", version >= NODE_MIN, out.strip(), fix)


def _srt(which, run, srt_version: str, apt: bool) -> Check:
    sudo = "sudo " if apt else ""
    fix = f"{sudo}npm install -g @anthropic-ai/sandbox-runtime@{srt_version}"
    if which("srt") is None:
        return Check("srt", False, "not found", fix)
    code, out = run(["srt", "--version"])
    found = out.strip().splitlines()[-1].strip() if out.strip() else ""
    if code != 0 or found != srt_version:
        return Check("srt", False, f"version {found or '?'}, the org pins {srt_version}", fix)
    return Check("srt", True, found)


def _tool(which, program: str, name: str, fix: str) -> Check:
    path = which(program)
    return Check(name, path is not None, path or "not found", fix)


def _smoke(which, run, *, ready: bool) -> Check:
    if not ready or which("srt") is None:
        return Check("smoke", False, "not run: node and srt are needed first", USERNS_FIX)
    with tempfile.TemporaryDirectory(prefix="agentic-srt-") as folder:
        box = SrtSandbox(SandboxRules(write=(Path(folder),)), Path(folder))
        box._write_settings()
        code, out = run(["srt", "--settings", str(box.settings), "--", "bash", "-c", "true"])
    if code != 0:
        return Check("smoke", False, out[-200:] or f"exit {code}", USERNS_FIX)
    return Check("smoke", True, "srt started a sandbox")
