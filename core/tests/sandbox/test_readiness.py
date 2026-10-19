"""Tests for sandbox readiness (what is missing on this machine, and how to fix it) and
the regime decision (M1-33, M1-32; D38: srt everywhere, Windows via WSL2)."""

from pathlib import Path

import pytest

from agentic.policy import SandboxPolicy
from agentic.sandbox.readiness import (
    WINDOWS_GUIDANCE,
    Check,
    Readiness,
    decide_sandbox,
    readiness,
)

WSL_KERNEL = "5.15.167.4-microsoft-standard-WSL2"


def present(name: str) -> str:
    return f"/usr/bin/{name}"


def answers(node="v22.11.0", srt="0.0.78", smoke=0):
    def run(argv: list[str]) -> tuple[int, str]:
        program = Path(argv[0]).name
        if program == "node":
            return 0, node + "\n"
        if program == "srt" and argv[1] == "--version":
            return 0, srt + "\n"
        if program == "srt":
            return smoke, "" if smoke == 0 else "bwrap: setting up uid map: Permission denied"
        raise AssertionError(f"unexpected command {argv}")

    return run


def boom(argv):
    raise AssertionError(f"nothing should run on native Windows: {argv}")


def failing(r: Readiness) -> set[str]:
    return {c.name for c in r.checks if not c.ok}


# --- platforms ---


def test_native_windows_gets_the_exact_guidance():
    r = readiness(system="Windows", which=lambda name: None, run=boom)
    assert (r.platform, r.available) == ("windows", False)
    assert "wsl --install" in r.text()
    assert "@anthropic-ai/sandbox-runtime@0.0.78" in r.text()
    assert WINDOWS_GUIDANCE.splitlines()[0] in r.text()


def test_linux_with_everything_is_available():
    r = readiness(system="Linux", kernel_release="6.8.0-45-generic", which=present, run=answers())
    assert (r.platform, r.available) == ("linux", True)
    assert {c.name for c in r.checks} >= {"node", "srt", "ripgrep", "bubblewrap", "socat", "smoke"}


def test_wsl_is_detected_and_a_repo_on_mnt_c_is_flagged():
    r = readiness(
        system="Linux",
        kernel_release=WSL_KERNEL,
        which=present,
        run=answers(),
        tree=Path("/mnt/c/src/app"),
    )
    assert (r.platform, r.available) == ("wsl", False)
    assert failing(r) == {"tree location"}
    assert "~/src" in r.text()


def test_wsl_with_the_repo_on_the_wsl_disk_is_available():
    r = readiness(
        system="Linux",
        kernel_release=WSL_KERNEL,
        which=present,
        run=answers(),
        tree=Path("/home/dev/src/app"),
    )
    assert (r.platform, r.available) == ("wsl", True)


def test_macos_needs_no_bwrap_or_socat():
    which = lambda name: None if name in ("bwrap", "socat") else present(name)  # noqa: E731
    r = readiness(system="Darwin", which=which, run=answers())
    assert (r.platform, r.available) == ("macos", True)
    assert "bubblewrap" not in {c.name for c in r.checks}


def test_an_unknown_os_has_no_sandbox():
    r = readiness(system="FreeBSD", which=present, run=answers())
    assert (r.platform, r.available) == ("other", False)


# --- each prerequisite ---


def test_old_node_is_flagged():
    r = readiness(system="Linux", kernel_release="6.8", which=present, run=answers(node="v18.19.0"))
    assert failing(r) == {"node", "smoke"}  # the smoke test needs a usable node first
    assert "20.11" in r.text()


def test_wrong_srt_version_is_flagged():
    r = readiness(system="Linux", kernel_release="6.8", which=present, run=answers(srt="0.0.80"))
    assert "srt" in failing(r)
    assert "0.0.78" in r.text()


def test_missing_tools_are_named_with_their_fix():
    which = lambda name: None if name in ("socat", "rg") else present(name)  # noqa: E731
    r = readiness(system="Linux", kernel_release="6.8", which=which, run=answers())
    assert failing(r) == {"socat", "ripgrep"}
    assert "apt install -y socat" in r.text()


def test_missing_srt_skips_the_smoke_test():
    which = lambda name: None if name == "srt" else present(name)  # noqa: E731
    r = readiness(system="Linux", kernel_release="6.8", which=which, run=answers())
    assert failing(r) == {"srt", "smoke"}


def test_a_failing_smoke_names_user_namespaces():
    r = readiness(system="Linux", kernel_release="6.8", which=present, run=answers(smoke=1))
    assert failing(r) == {"smoke"}
    assert "apparmor_restrict_unprivileged_userns" in r.text()


# --- the regime decision ---

AVAILABLE = Readiness(platform="linux", checks=(Check("srt", True, "0.0.78"),))
UNAVAILABLE = Readiness(platform="windows", checks=(Check("sandbox", False, "native Windows"),))


def test_an_available_sandbox_is_used():
    d = decide_sandbox(AVAILABLE, "team", SandboxPolicy())
    assert (d.use_srt, d.label, d.refusal, d.warning) == (True, "srt 0.0.78", None, None)


def test_regulated_refuses_without_a_sandbox():
    d = decide_sandbox(UNAVAILABLE, "regulated", SandboxPolicy())
    assert d.refusal and "regulated" in d.refusal and not d.use_srt


@pytest.mark.parametrize("regime", ["team", "solo"])
def test_other_regimes_run_unsandboxed_with_a_warning(regime):
    d = decide_sandbox(UNAVAILABLE, regime, SandboxPolicy())
    assert (d.use_srt, d.label, d.refusal) == (False, "none", None)
    assert d.warning and "policy guards only" in d.warning


def test_policy_can_require_the_sandbox_for_team():
    d = decide_sandbox(UNAVAILABLE, "team", SandboxPolicy(required_for=["team", "regulated"]))
    assert d.refusal
