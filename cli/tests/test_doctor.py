"""`agentic doctor` (M3-09): every prerequisite `agentic init` checks plus runtime health,
each with a fix and an exit code (DF-366 withheld names, DF-654 tier models, D33 policy
repo protection). Fakes only: the machine is described, the gateway, control plane and
GitHub answer from memory (recorded GitHub responses)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from pathlib import Path

import httpx2
import pytest
import yaml
from cp_fakes import CP_URL, TOKEN, FakeControlPlane

from agentic.hosting.github import GitHubHost, api_url_for
from agentic_cli import control_plane, doctor, hosting
from agentic_cli.commands import doctor as doctor_command
from agentic_cli.exit_codes import ExitCode
from agentic_cli.main import main

NOW = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)
GATEWAY = "https://gw.example.invalid"
KEY = "tl-" + "x" * 30  # gitleaks:allow - not a real key
SERVED = ["claude-opus", "claude-sonnet", "gemini-flash", "qwen-coder", "gpt-5"]
GITHUB_TOKEN = "ghp_" + "Fake" + "Doctor" + "0123456789abcdef"  # gitleaks:allow
FIXTURES = Path(__file__).resolve().parents[2] / "core" / "tests" / "fixtures" / "github"
POLICY_URL = "https://github.com/acme/agentic-org-policy.git"


def present(name: str) -> str:
    return f"/usr/bin/{name}"


def machine_run(argv: list[str]) -> tuple[int, str]:
    program = Path(argv[0]).name
    answers = {
        "git": "git version 2.47.1",
        "node": "v22.11.0",
        "uv": "uv 0.9.2",
        "docker": "Docker Engine - Community",
        "podman": "podman version 5.0.0",
    }
    if program == "srt":
        return (0, "0.0.78") if argv[1] == "--version" else (0, "")
    return 0, answers[program]


class Gateway:
    def __init__(self, status: int = 200, models=SERVED, skew: timedelta = timedelta(0)):
        self.status, self.models, self.skew = status, models, skew
        self.requests: list[httpx2.Request] = []

    def transport(self) -> httpx2.MockTransport:
        def handle(request: httpx2.Request) -> httpx2.Response:
            self.requests.append(request)
            body = {"object": "list", "data": [{"id": m} for m in self.models]}
            date = format_datetime(NOW + self.skew, usegmt=True)
            return httpx2.Response(self.status, json=body, headers={"Date": date})

        return httpx2.MockTransport(handle)


@pytest.fixture
def gateway() -> Gateway:
    return Gateway()


@pytest.fixture(autouse=True)
def setup(home, repo, policy_repo, gateway, monkeypatch):
    monkeypatch.chdir(repo)
    monkeypatch.setenv("AGENTIC_POLICY_REPO", str(policy_repo))
    monkeypatch.setenv("AGENTIC_TOKENLEAN_API_KEY", KEY)
    monkeypatch.delenv("AGENTIC_GATEWAY_URL", raising=False)
    monkeypatch.delenv("AGENTIC_CONTROL_PLANE_TOKEN", raising=False)
    monkeypatch.delenv("AGENTIC_CONTROL_PLANE_URL", raising=False)
    monkeypatch.delenv("HTTPS_PROXY", raising=False)
    write_config(home, gateway_url=GATEWAY)
    monkeypatch.setattr(
        doctor,
        "default_deps",
        lambda: doctor.DoctorDeps(
            which=present,
            run=machine_run,
            system="Linux",
            kernel_release="6.8.0",
            disk_usage=lambda where: type("U", (), {"free": 50 * 1024**3})(),
            now=lambda: NOW,
        ),
    )
    monkeypatch.setattr(doctor_command, "gateway_transport", gateway.transport)


def write_config(home: Path, **settings) -> None:
    folder = home / ".agentic"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "config.yaml").write_text(yaml.safe_dump(settings), encoding="utf-8")


def run_doctor(capsys) -> tuple[int, dict]:
    code = main(["--json", "doctor"])
    captured = capsys.readouterr()
    assert captured.out.strip(), captured.err
    return code, json.loads(captured.out)


def check(data: dict, name: str) -> dict:
    return next(c for c in data["checks"] if c["name"] == name)


def statuses(data: dict) -> dict[str, str]:
    return {c["name"]: c["status"] for c in data["checks"]}


# ── every prerequisite ────────────────────────────────────────────────────────


def test_doctor_reports_every_prerequisite(capsys):
    code, data = run_doctor(capsys)

    found = statuses(data)
    for name in (
        "python",
        "git",
        "sandbox: node",
        "sandbox: srt",
        "sandbox: ripgrep",
        "sandbox: bubblewrap",
        "sandbox: socat",
        "sandbox: smoke",
        "container engine",
        "credential",
        "uv",
        "repository",
        "policy",
        "policy: models",
        "policy: cache",
        "keyring",
        "gateway",
        "gateway: models",
        "control plane",
        "clock",
        "disk space",
        "local data",
        "withheld variables",
        "policy repo protection",
    ):
        assert name in found, name
    assert code == ExitCode.OK, [c for c in data["checks"] if c["status"] == "fail"]
    assert data["healthy"] is True
    assert found["gateway"] == "ok" and found["gateway: models"] == "ok"
    assert found["repository"] == "warn"  # init never ran in this test repository
    assert all(c["fix"] for c in data["checks"] if c["status"] in ("fail", "warn"))


def test_human_output_names_each_check_and_its_fix(capsys):
    write_config(Path(doctor_home()), gateway_url=GATEWAY)
    assert main(["doctor"]) == ExitCode.OK
    out = capsys.readouterr().out
    assert "agentic doctor" in out and "healthy" in out
    assert "repository" in out and "run `agentic init`" in out


def doctor_home() -> Path:
    from agentic_cli.config import agentic_home

    return agentic_home().parent


def test_exit_code_one_when_a_check_fails(gateway, capsys):
    gateway.status = 401
    code, data = run_doctor(capsys)
    assert code == ExitCode.ERROR
    assert data["healthy"] is False
    failing = check(data, "gateway")
    assert failing["status"] == "fail"
    assert "agentic login" in failing["fix"]
    assert KEY not in json.dumps(data)


def test_no_gateway_configured_fails(home, capsys):
    write_config(home)
    code, data = run_doctor(capsys)
    assert code == ExitCode.ERROR
    assert "gateway_url" in check(data, "gateway")["fix"]


def test_gateway_tier_model_not_served_fails(gateway, capsys):
    gateway.models = ["claude-opus", "claude-sonnet"]
    code, data = run_doctor(capsys)
    assert code == ExitCode.ERROR
    models = check(data, "gateway: models")
    assert models["status"] == "fail"
    assert "fast (google/gemini-flash)" in models["detail"]
    assert "local (ollama/qwen-coder)" in models["detail"]


def test_a_team_override_model_is_checked_for_that_team(gateway, home, capsys, monkeypatch):
    monkeypatch.setenv("AGENTIC_TEAM", "payments")
    gateway.models = ["claude-opus", "claude-sonnet", "gemini-flash", "qwen-coder"]
    _, data = run_doctor(capsys)
    assert "deep-reasoning (openai/gpt-5)" in check(data, "gateway: models")["detail"]


def test_clock_skew_detected(gateway, capsys):
    gateway.skew = timedelta(minutes=-10)
    code, data = run_doctor(capsys)
    assert code == ExitCode.ERROR
    clock = check(data, "clock")
    assert clock["status"] == "fail"
    assert "600 s ahead of" in clock["detail"]


def test_small_clock_skew_warns(gateway, capsys):
    gateway.skew = timedelta(seconds=90)
    code, data = run_doctor(capsys)
    assert code == ExitCode.OK
    assert check(data, "clock")["status"] == "warn"


def test_low_disk_space_fails(capsys, monkeypatch):
    deps = doctor.default_deps()
    deps.disk_usage = lambda where: type("U", (), {"free": 200 * 1024**2})()
    monkeypatch.setattr(doctor, "default_deps", lambda: deps)
    code, data = run_doctor(capsys)
    assert code == ExitCode.ERROR
    assert "agentic gc" in check(data, "disk space")["fix"]


def test_a_missing_sandbox_tool_is_reported_with_its_fix(capsys, monkeypatch):
    deps = doctor.default_deps()
    deps.which = lambda name: None if name == "bwrap" else present(name)
    monkeypatch.setattr(doctor, "default_deps", lambda: deps)
    _, data = run_doctor(capsys)
    bwrap = check(data, "sandbox: bubblewrap")
    assert bwrap["status"] == "warn"  # solo runs go unsandboxed; team runs refuse
    assert "apt install -y bubblewrap" in bwrap["fix"]


def test_native_windows_points_to_wsl2(capsys, monkeypatch):
    deps = doctor.default_deps()
    deps.system = "Windows"
    monkeypatch.setattr(doctor, "default_deps", lambda: deps)
    _, data = run_doctor(capsys)
    sandbox = check(data, "sandbox")
    assert "wsl --install" in sandbox["fix"]
    assert "git bash" in statuses(data)


def test_initialised_repository_is_ok(repo, capsys):
    state = repo / ".agentic" / "local" / "init.json"
    state.parent.mkdir(parents=True)
    state.write_text(json.dumps({"initialised_at": "2026-10-24T10:00:00", "ready": True}))
    _, data = run_doctor(capsys)
    assert check(data, "repository")["status"] == "ok"


def test_outside_a_repository_machine_checks_still_run(tmp_path, monkeypatch, capsys):
    outside = tmp_path / "nowhere"
    outside.mkdir()
    monkeypatch.chdir(outside)
    _, data = run_doctor(capsys)
    found = statuses(data)
    assert found["repository"] == "skip" and found["python"] == "ok"
    assert found["gateway"] == "ok"


def org_cache(home: Path, policy_repo: Path, fetched: datetime, expires: datetime) -> None:
    import shutil

    cache = home / ".agentic" / "policy" / "acme"
    shutil.copytree(policy_repo, cache / "repo")
    stamp = {"fetched_at": fetched.isoformat(), "expires_at": expires.isoformat()}
    (cache / "fetched.json").write_text(json.dumps(stamp), encoding="utf-8")


def test_expired_org_policy_cache_fails(home, policy_repo, monkeypatch, capsys):
    monkeypatch.delenv("AGENTIC_POLICY_REPO")
    write_config(home, org="acme", policy_repo=POLICY_URL, gateway_url=GATEWAY)
    real_now = datetime.now(UTC)
    org_cache(home, policy_repo, real_now - timedelta(days=3), real_now - timedelta(days=2))
    code, data = run_doctor(capsys)
    cache = check(data, "policy: cache")
    assert cache["status"] == "fail" and "expired" in cache["detail"]
    assert code == ExitCode.ERROR


def test_fresh_org_policy_cache_is_ok(home, policy_repo, monkeypatch, capsys):
    monkeypatch.delenv("AGENTIC_POLICY_REPO")
    write_config(home, org="acme", policy_repo=POLICY_URL, gateway_url=GATEWAY)
    real_now = datetime.now(UTC)
    org_cache(home, policy_repo, real_now - timedelta(hours=1), real_now + timedelta(hours=23))
    _, data = run_doctor(capsys)
    assert check(data, "policy: cache")["status"] == "ok"
    assert check(data, "policy")["detail"].startswith("org acme")


# ── DF-366: withheld environment variables ────────────────────────────────────


def test_withheld_names_listed_without_values(capsys, monkeypatch):
    secret = "pr0xy-" + "pw"
    monkeypatch.setenv("HTTPS_PROXY", f"http://me:{secret}@proxy.corp:8080")
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/agent.sock")
    _, data = run_doctor(capsys)
    withheld = check(data, "withheld variables")
    assert withheld["status"] == "info"
    assert "HTTPS_PROXY" in withheld["detail"] and "SSH_AUTH_SOCK" in withheld["detail"]
    assert "sandbox.pass_env" in withheld["fix"]
    text = json.dumps(data)
    assert secret not in text and "proxy.corp" not in text


# ── the control plane ─────────────────────────────────────────────────────────


@pytest.fixture
def plane(home, monkeypatch):
    fake = FakeControlPlane()
    inner = fake.transport

    def dated(request):
        response = inner.handle_request(request)
        response.headers["Date"] = format_datetime(NOW, usegmt=True)
        return response

    monkeypatch.setattr(control_plane, "make_transport", lambda: httpx2.MockTransport(dated))
    write_config(home, org="acme", control_plane_url=CP_URL, gateway_url=GATEWAY)
    return fake


def test_control_plane_signed_in(plane, monkeypatch, capsys):
    monkeypatch.setenv("AGENTIC_CONTROL_PLANE_TOKEN", TOKEN)
    _, data = run_doctor(capsys)
    signed = check(data, "control plane")
    assert signed["status"] == "ok"
    assert "dev@acme.example" in signed["detail"] and "acme" in signed["detail"]
    assert check(data, "clock")["status"] == "ok"


def test_control_plane_not_signed_in(plane, capsys):
    code, data = run_doctor(capsys)
    assert code == ExitCode.ERROR
    signed = check(data, "control plane")
    assert signed["status"] == "fail"
    assert "agentic login --for control-plane" in signed["fix"]


def test_control_plane_down(plane, monkeypatch, capsys):
    monkeypatch.setenv("AGENTIC_CONTROL_PLANE_TOKEN", TOKEN)
    plane.down = True
    code, data = run_doctor(capsys)
    assert code == ExitCode.ERROR
    assert "cannot reach" in check(data, "control plane")["detail"]


# ── D33: the policy repo's protection ─────────────────────────────────────────


def github_answers(monkeypatch, protection: tuple[int, str], rules: tuple[int, str]):
    seen: list[str] = []

    def handle(request: httpx2.Request) -> httpx2.Response:
        seen.append(request.url.path)
        path = request.url.path
        if path == "/repos/acme/agentic-org-policy":
            return httpx2.Response(200, json={"default_branch": "main"})
        status, name = protection if path.endswith("/protection") else rules
        return httpx2.Response(status, json=json.loads((FIXTURES / name).read_text("utf-8")))

    kind = hosting.HOST_KINDS["github"]
    monkeypatch.setitem(
        hosting.HOST_KINDS,
        "github",
        hosting.HostKind(
            make=lambda url, token: GitHubHost(url, token, transport=httpx2.MockTransport(handle)),
            api_url=api_url_for,
            credential=kind.credential,
        ),
    )
    return seen


def org_config(home: Path) -> None:
    write_config(home, org="acme", policy_repo=POLICY_URL, gateway_url=GATEWAY)


def test_policy_repo_protection_cannot_check_without_token(home, monkeypatch, capsys):
    org_config(home)
    seen = github_answers(
        monkeypatch, (200, "protection-code-owners.json"), (200, "rules-no-review.json")
    )
    code, data = run_doctor(capsys)
    protection = check(data, "policy repo protection")
    assert protection["status"] == "skip"
    assert "cannot check" in protection["detail"]
    assert "agentic login --for github" in protection["fix"]
    assert seen == []
    assert code == ExitCode.OK


def test_policy_repo_protection_ok(home, monkeypatch, capsys):
    org_config(home)
    monkeypatch.setenv("AGENTIC_GITHUB_TOKEN", GITHUB_TOKEN)
    github_answers(monkeypatch, (200, "protection-code-owners.json"), (200, "rules-no-review.json"))
    code, data = run_doctor(capsys)
    protection = check(data, "policy repo protection")
    assert protection["status"] == "ok"
    assert "github.com/acme/agentic-org-policy main" in protection["detail"]
    assert code == ExitCode.OK


def test_policy_repo_protection_unprotected_fails(home, monkeypatch, capsys):
    org_config(home)
    monkeypatch.setenv("AGENTIC_GITHUB_TOKEN", GITHUB_TOKEN)
    github_answers(
        monkeypatch, (404, "protection-not-protected.json"), (200, "rules-no-review.json")
    )
    code, data = run_doctor(capsys)
    protection = check(data, "policy repo protection")
    assert protection["status"] == "fail"
    assert "D33" in protection["detail"]
    assert "Code Owners" in protection["fix"]
    assert code == ExitCode.ERROR
    assert GITHUB_TOKEN not in json.dumps(data)


def test_policy_repo_protection_without_code_owners_fails(home, monkeypatch, capsys):
    org_config(home)
    monkeypatch.setenv("AGENTIC_GITHUB_TOKEN", GITHUB_TOKEN)
    github_answers(
        monkeypatch, (200, "protection-no-code-owners.json"), (200, "rules-no-review.json")
    )
    _, data = run_doctor(capsys)
    assert check(data, "policy repo protection")["status"] == "fail"


def test_policy_repo_protection_without_admin_cannot_tell(home, monkeypatch, capsys):
    org_config(home)
    monkeypatch.setenv("AGENTIC_GITHUB_TOKEN", GITHUB_TOKEN)
    github_answers(monkeypatch, (403, "protection-no-admin.json"), (200, "rules-no-review.json"))
    _, data = run_doctor(capsys)
    protection = check(data, "policy repo protection")
    assert protection["status"] == "skip"
    assert "cannot check" in protection["detail"]


def test_policy_repo_protection_from_rulesets(home, monkeypatch, capsys):
    org_config(home)
    monkeypatch.setenv("AGENTIC_GITHUB_TOKEN", GITHUB_TOKEN)
    github_answers(monkeypatch, (403, "protection-no-admin.json"), (200, "rules-code-owners.json"))
    _, data = run_doctor(capsys)
    assert check(data, "policy repo protection")["status"] == "ok"


def test_standalone_skips_the_policy_repo_check(capsys):
    _, data = run_doctor(capsys)
    assert check(data, "policy repo protection")["status"] == "skip"
