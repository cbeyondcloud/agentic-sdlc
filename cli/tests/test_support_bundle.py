"""`agentic support-bundle` (M3-09): a zip for the platform team - versions, doctor's report,
recent structured logs, run summaries and the config with secrets removed - that holds no
secret and no personal data. Secrets and PII are planted everywhere the bundle reads; none
of them may reach any member of the zip, and a hit left after redaction fails closed."""

from __future__ import annotations

import json
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml
from test_doctor import GATEWAY, KEY, Gateway, machine_run, present

from agentic.engine.state import Cursor, RunState, RunStore
from agentic_cli import doctor, support_bundle
from agentic_cli.commands import doctor as doctor_command
from agentic_cli.exit_codes import ExitCode
from agentic_cli.main import main

NOW = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)
# planted secrets and personal data, built from pieces so no scanner flags this file
ENV_SECRET = "s3rv1ce-" + "t0ken-" + "value-9137"
GITHUB_LIKE = "ghp_" + "Pl4nted" + "Secret" + "0123456789abcdefABCD"  # gitleaks:allow
CONFIG_SECRET = "c0nfig-" + "p4ss-" + "7781"
EMAIL = "jane.doe" + "@" + "example.org"
CARD = "4111 1111" + " 1111 1111"
PLANTED = (KEY, ENV_SECRET, GITHUB_LIKE, CONFIG_SECRET, EMAIL, CARD, CARD.replace(" ", ""))


@pytest.fixture(autouse=True)
def setup(home, repo, policy_repo, monkeypatch):
    monkeypatch.chdir(repo)
    monkeypatch.setenv("AGENTIC_POLICY_REPO", str(policy_repo))
    monkeypatch.setenv("AGENTIC_TOKENLEAN_API_KEY", KEY)
    monkeypatch.setenv("MY_SERVICE_TOKEN", ENV_SECRET)
    monkeypatch.delenv("AGENTIC_CONTROL_PLANE_TOKEN", raising=False)
    monkeypatch.delenv("AGENTIC_CONTROL_PLANE_URL", raising=False)
    folder = home / ".agentic"
    folder.mkdir(parents=True, exist_ok=True)
    config = {
        "gateway_url": GATEWAY,
        "user": EMAIL,
        "api_key": CONFIG_SECRET,
        "notes": f"my token is {GITHUB_LIKE}",
        "jira": {"url": "https://acme.atlassian.net", "password": CONFIG_SECRET},
    }
    (folder / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
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
    monkeypatch.setattr(doctor_command, "gateway_transport", Gateway().transport)
    monkeypatch.setattr(support_bundle, "utc_now", lambda: NOW)


def make_run(repo: Path, run_id: str, days_old: int, log_lines: list[dict]) -> None:
    when = NOW - timedelta(days=days_old)
    state = RunState(
        run_id=run_id,
        pipeline="feature-delivery",
        pipeline_version="1.0.0",
        pipeline_digest="sha256:" + "0" * 64,
        policy_version="git:" + "c" * 40,
        catalog_digest="sha256:" + "1" * 64,
        regime="solo",
        starter=EMAIL,
        status="stopped",
        cursor=Cursor(index=0),
        started_at=when,
        updated_at=when,
        ended_by=EMAIL,
        end_reason=f"card {CARD} and key {KEY} pasted by mistake",
        inputs={"request": f"use {GITHUB_LIKE} to call it"},
    )
    store = RunStore(repo / ".agentic" / "local" / "runs")
    store.save(state)
    logs = store.path(run_id).parent / "logs"
    logs.mkdir()
    text = "".join(json.dumps(line) + "\n" for line in log_lines)
    (logs / "dev-1.jsonl").write_text(text, encoding="utf-8")


def log(message: str, **fields) -> dict:
    return {
        "at": NOW.isoformat(),
        "seq": 1,
        "level": "warning",
        "logger": "agentic.agents",
        "run_id": "r-1",
        "step": "dev",
        "iteration": 1,
        "message": message,
        "fields": fields,
        "error": None,
    }


def members(path: Path) -> dict[str, str]:
    with zipfile.ZipFile(path) as archive:
        return {name: archive.read(name).decode("utf-8") for name in archive.namelist()}


def test_planted_secrets_and_pii_never_reach_the_bundle(repo: Path, tmp_path: Path, capsys):
    make_run(
        repo,
        "r-1",
        1,
        [
            log(f"calling with {ENV_SECRET}", contact=EMAIL),
            log(f"Authorization: Bearer {KEY}"),
            log("ok", note=f"{GITHUB_LIKE}"),
        ],
    )
    output = tmp_path / "bundle.zip"

    code = main(["--json", "support-bundle", "--output", str(output)])

    assert code == ExitCode.OK, capsys.readouterr().err
    result = json.loads(capsys.readouterr().out)
    assert Path(result["path"]) == output
    found = members(output)
    assert "doctor.json" in found and "manifest.json" in found
    assert "config/user-config.yaml" in found
    assert "runs/r-1/summary.json" in found
    assert "logs/r-1/dev-1.jsonl" in found
    for name, text in found.items():
        for planted in PLANTED:
            assert planted not in text, f"{planted!r} reached {name}"
    config = yaml.safe_load(found["config/user-config.yaml"])
    assert config["api_key"] == "<removed>"
    assert config["jira"]["password"] == "<removed>"
    assert config["gateway_url"] == GATEWAY  # not secret: kept for the platform team


def test_bundle_lists_its_contents_and_versions(repo: Path, tmp_path: Path, capsys):
    make_run(repo, "r-1", 1, [log("hello")])
    output = tmp_path / "bundle.zip"
    assert main(["support-bundle", "--output", str(output)]) == ExitCode.OK
    manifest = json.loads(members(output)["manifest.json"])
    assert manifest["versions"]["agentic"]
    assert manifest["versions"]["python"]
    assert "doctor.json" in manifest["contents"]
    assert "redacted" in manifest["note"]
    assert str(output) in capsys.readouterr().out


def test_only_recent_runs_and_log_tails_are_included(repo: Path, tmp_path: Path, monkeypatch):
    monkeypatch.setattr(support_bundle, "LOG_RUNS", 2)
    monkeypatch.setattr(support_bundle, "LOG_LINES", 3)
    monkeypatch.setattr(support_bundle, "SUMMARY_RUNS", 3)
    for n in range(5):
        make_run(repo, f"r-{n}", n, [log(f"line {i}") for i in range(10)])
    output = tmp_path / "bundle.zip"
    assert main(["support-bundle", "--output", str(output)]) == ExitCode.OK
    found = members(output)
    assert sorted(n for n in found if n.startswith("runs/")) == [
        "runs/r-0/summary.json",
        "runs/r-1/summary.json",
        "runs/r-2/summary.json",
    ]
    assert sorted(n for n in found if n.startswith("logs/")) == [
        "logs/r-0/dev-1.jsonl",
        "logs/r-1/dev-1.jsonl",
    ]
    lines = found["logs/r-0/dev-1.jsonl"].splitlines()
    assert [json.loads(line)["message"] for line in lines] == ["line 7", "line 8", "line 9"]


def test_no_ledger_handoff_or_source_goes_in(repo: Path, tmp_path: Path):
    make_run(repo, "r-1", 1, [log("x")])
    folder = repo / ".agentic" / "local" / "runs" / "r-1"
    (folder / "design.md").write_text("handoff text", encoding="utf-8")
    (folder / "events.jsonl").write_text("{}\n", encoding="utf-8")
    output = tmp_path / "bundle.zip"
    assert main(["support-bundle", "--output", str(output)]) == ExitCode.OK
    names = set(members(output))
    assert not [n for n in names if n.endswith(("design.md", "events.jsonl", "app.py"))]


def test_a_hit_after_redaction_fails_closed(repo: Path, tmp_path: Path, monkeypatch, capsys):
    make_run(repo, "r-1", 1, [log("x")])
    monkeypatch.setattr(support_bundle, "_leftover", lambda text, check: "secret github-token")
    output = tmp_path / "bundle.zip"
    code = main(["support-bundle", "--output", str(output)])
    assert code == ExitCode.ERROR
    assert "nothing was written" in capsys.readouterr().err
    assert not output.exists()
    assert not list(tmp_path.glob("*.tmp*"))


def test_output_path_refuses_overwrite(tmp_path: Path, capsys):
    output = tmp_path / "bundle.zip"
    output.write_bytes(b"keep me")
    assert main(["support-bundle", "--output", str(output)]) == ExitCode.REFUSED
    assert output.read_bytes() == b"keep me"


def test_home_folder_is_shortened(repo: Path, tmp_path: Path, home: Path, monkeypatch):
    monkeypatch.setattr(support_bundle, "_home", lambda: home)
    output = tmp_path / "bundle.zip"
    assert main(["support-bundle", "--output", str(output)]) == ExitCode.OK
    for text in members(output).values():
        assert str(home) not in text
        assert json.dumps(str(home))[1:-1] not in text


def test_outside_a_repository_still_makes_a_bundle(tmp_path: Path, monkeypatch):
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    monkeypatch.chdir(outside)
    assert main(["support-bundle"]) == ExitCode.OK
    (made,) = outside.glob("agentic-support-*.zip")
    assert "doctor.json" in members(made)
