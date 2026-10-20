"""The readiness report's prerequisite checks (M1-53): every check is listed, and every
failing one carries a fix. Fakes stand in for the machine (which, run, OS)."""

import json
from pathlib import Path

import pytest
from cli_helpers import FIXTURES
from rich.console import Console

from agentic.auth import Credentials
from agentic.policy import load_policy
from agentic_cli.readiness import Item, Report, prerequisites

KEY = "AGENTIC_TOKENLEAN_API_KEY"


class NoRing:
    def get_password(self, service, username):
        return None

    def set_password(self, service, username, password):
        raise AssertionError("not used")


@pytest.fixture
def policy():
    return load_policy(FIXTURES / "policy" / "policy")


def machine(present: set[str], answers: dict[str, tuple[int, str]]):
    def which(name: str) -> str | None:
        return f"/usr/bin/{name}" if name in present else None

    def run(argv: list[str]) -> tuple[int, str]:
        key = " ".join([Path(argv[0]).name, *argv[1:2]])
        return answers.get(key, (1, f"unexpected {argv}"))

    return which, run


LINUX_OK = {
    "git --version": (0, "git version 2.47.1"),
    "node --version": (0, "v22.11.0"),
    "srt --version": (0, "0.0.78"),
    "srt --settings": (0, ""),
}


def checks(policy, system="Linux", present=None, answers=None, env=None, release="6.8.0"):
    which, run = machine(
        present if present is not None else {"git", "node", "srt", "rg", "bwrap", "socat"},
        answers if answers is not None else LINUX_OK,
    )
    creds = Credentials(environ=env if env is not None else {KEY: "k"}, keyring=NoRing())
    return {
        item.name: item
        for item in prerequisites(
            policy=policy,
            which=which,
            run=run,
            system=system,
            kernel_release=release,
            credentials=creds,
        )
    }


def test_a_ready_linux_machine(policy):
    items = checks(policy)
    assert items["python"].status == "ok" and items["git"].status == "ok"
    assert items["sandbox: smoke"].status == "ok" and items["credential"].status == "ok"
    assert Report(list(items.values())).ready


def test_an_old_git_is_missing_with_a_fix(policy):
    items = checks(policy, answers={**LINUX_OK, "git --version": (0, "git version 2.30.0")})
    assert items["git"].status == "missing" and "2.40" in items["git"].fix


def test_native_windows_reports_the_wsl_guidance_with_the_org_pin(policy):  # DF-67
    pinned = policy.model_copy(
        update={"sandbox": policy.sandbox.model_copy(update={"srt_version": "0.0.90"})}
    )
    items = checks(pinned, system="Windows", present={"git", "bash"})
    sandbox = items["sandbox: sandbox"]
    assert sandbox.status == "warn" and "wsl --install" in sandbox.fix and "0.0.90" in sandbox.fix


def test_a_sandbox_required_for_solo_runs_is_missing(policy):
    strict = policy.model_copy(
        update={"sandbox": policy.sandbox.model_copy(update={"required_for": ["solo", "team"]})}
    )
    assert checks(strict, system="Windows", present={"git"})["sandbox: sandbox"].status == "missing"


def test_git_bash_is_checked_on_windows(policy, monkeypatch, tmp_path):  # DF-73
    monkeypatch.setattr("agentic_cli.readiness.GIT_BASH_DEFAULT", tmp_path / "no-bash.exe")
    items = checks(policy, system="Windows", present={"git"})
    assert items["git bash"].status == "warn" and "Git for Windows" in items["git bash"].fix
    assert "git bash" not in checks(policy)  # Linux and macOS have a POSIX shell


def test_docker_desktop_gets_a_licence_note_that_never_blocks(policy):  # DF-105
    answers = {**LINUX_OK, "docker version": (0, "Docker Desktop 4.37.1 (178610)")}
    present = {"git", "node", "srt", "rg", "bwrap", "socat", "docker"}
    items = checks(policy, present=present, answers=answers)
    engine = items["container engine"]
    assert engine.status == "info" and "licence" in engine.detail.lower()
    assert Report(list(items.values())).ready


def test_podman_or_no_engine_is_information_only(policy):
    answers = {**LINUX_OK, "podman --version": (0, "podman version 5.2.0")}
    present = {"git", "node", "srt", "rg", "bwrap", "socat", "podman"}
    assert "podman" in checks(policy, present=present, answers=answers)["container engine"].detail
    assert checks(policy)["container engine"].status == "info"


def test_no_credential_is_missing_with_login_as_the_fix(policy):
    item = checks(policy, env={})["credential"]
    assert item.status == "missing" and "agentic login" in item.fix


def test_every_failing_item_has_a_fix(policy):
    items = checks(policy, system="Windows", present=set(), env={})
    failing = [i for i in items.values() if i.status in ("missing", "warn")]
    assert failing and all(i.fix for i in failing)


def test_the_report_as_json_and_text():
    report = Report([Item("git", "ok", "2.47"), Item("credential", "missing", "none", "login")])
    data = report.to_json()
    assert data["ready"] is False and data["checks"][1]["fix"] == "login"
    json.dumps(data)
    console = Console(record=True, width=120)
    report.show(console)
    text = console.export_text()
    assert "NOT ready" in text and "login" in text and "git" in text
