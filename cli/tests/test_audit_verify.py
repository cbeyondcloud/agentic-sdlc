"""`agentic audit verify` (M2-26): every local run ledger and the control plane's audit
chain; a tampered entry is detected and located (run, file, entry, class of change) and the
command exits 3."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from cp_fakes import CP_URL, TOKEN, FakeControlPlane

import sdlc_commons.ledger as ledger_module
from agentic_cli import audit, control_plane
from agentic_cli.config import agentic_home
from agentic_cli.exit_codes import ExitCode
from agentic_cli.main import main
from sdlc_commons.gates import ZyrofloGates
from sdlc_commons.ledger import RunIdentity, RunLedger

POLICY = "git:" + "b" * 40


@pytest.fixture(autouse=True)
def fresh_identities(monkeypatch):
    monkeypatch.setattr(ledger_module, "_IDENTITIES", {})


def make_run(repo: Path, run_id: str, steps: int = 3) -> RunLedger:
    ledger = RunLedger(repo, run_id)
    ledger.open(
        identity=RunIdentity(team="payments", policy_version=POLICY),
        kind="feature",
        project="app",
        owner="payments",
        criticality="low",
    )
    for n in range(steps):
        ledger.record("step_started", phase="requirements", attempt=n + 1)
    return ledger


def lines_of(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def rewrite(path: Path, entries: list[dict]) -> None:
    text = "".join(json.dumps(e, sort_keys=True, ensure_ascii=False) + "\n" for e in entries)
    path.write_bytes(text.encode("utf-8"))


def verify(capsys, *args: str) -> tuple[int, dict]:
    code = main(["--json", "audit", "verify", *args])
    out = capsys.readouterr().out
    return code, json.loads(out) if out.strip() else {}


def events_file(repo: Path, run_id: str) -> Path:
    return RunLedger(repo, run_id).folder / "events.jsonl"


def findings(data: dict, run_id: str) -> list[tuple]:
    report = next(r for r in data["runs"] if r["run_id"] == run_id)
    return [(f["file"], f["entry"], f["seq"], f["field_class"]) for f in report["findings"]]


def test_intact_runs_exit_0(home, repo, monkeypatch, capsys):
    make_run(repo, "r-1")
    make_run(repo, "r-2", steps=1)
    monkeypatch.chdir(repo)
    code, data = verify(capsys)
    assert code == ExitCode.OK
    assert data["intact"] is True
    assert [(r["run_id"], r["entries"], r["intact"]) for r in data["runs"]] == [
        ("r-1", 4, True),
        ("r-2", 2, True),
    ]


def test_no_runs_is_ok(home, repo, monkeypatch, capsys):
    monkeypatch.chdir(repo)
    code, data = verify(capsys)
    assert code == ExitCode.OK and data["runs"] == []


def test_edited_event_is_located_as_content(home, repo, monkeypatch, capsys):
    make_run(repo, "r-1")
    make_run(repo, "r-ok")
    path = events_file(repo, "r-1")
    entries = lines_of(path)
    entries[1]["attempt"] = 99
    rewrite(path, entries)
    monkeypatch.chdir(repo)
    code, data = verify(capsys)
    assert code == ExitCode.INVALID
    assert data["intact"] is False
    assert findings(data, "r-1") == [("events.jsonl", 2, 2, "content")]
    report = next(r for r in data["runs"] if r["run_id"] == "r-1")
    assert report["problems"], "Zyroflo's own check must agree"
    assert findings(data, "r-ok") == []


def test_rewritten_hash_is_located_as_hash(home, repo, monkeypatch, capsys):
    make_run(repo, "r-1")
    path = events_file(repo, "r-1")
    entries = lines_of(path)
    entries[2]["hash"] = "sha256:" + "0" * 64
    rewrite(path, entries)
    monkeypatch.chdir(repo)
    code, data = verify(capsys, "r-1")
    assert code == ExitCode.INVALID
    assert findings(data, "r-1") == [("events.jsonl", 3, 3, "hash")]


def test_removed_event_is_located(home, repo, monkeypatch, capsys):
    make_run(repo, "r-1")
    path = events_file(repo, "r-1")
    entries = lines_of(path)
    del entries[2]
    rewrite(path, entries)
    monkeypatch.chdir(repo)
    code, data = verify(capsys)
    assert code == ExitCode.INVALID
    assert findings(data, "r-1") == [
        ("events.jsonl", 3, 4, "sequence"),
        ("events.jsonl", 3, 4, "link"),
    ]


def test_tampered_approval_is_located(home, repo, monkeypatch, capsys):
    ledger = make_run(repo, "r-1", steps=1)
    digest = ledger.write_artifact("handoffs/design-1.md", "# design\n")
    ZyrofloGates(ledger).record(
        "design-approval",
        "approve",
        "lead@acme.example",
        {"handoffs/design-1.md": digest},
        step="design",
    )
    path = ledger.folder / "approvals.jsonl"
    entries = lines_of(path)
    entries[0]["approver"] = "mallory@acme.example"
    rewrite(path, entries)
    monkeypatch.chdir(repo)
    code, data = verify(capsys)
    assert code == ExitCode.INVALID
    assert findings(data, "r-1") == [("approvals.jsonl", 1, None, "content")]


def test_torn_tail_is_reported(home, repo, monkeypatch, capsys):
    make_run(repo, "r-1")
    with events_file(repo, "r-1").open("ab") as fh:
        fh.write(b'{"seq": 5, "event": "step_')
    monkeypatch.chdir(repo)
    code, data = verify(capsys)
    assert code == ExitCode.INVALID
    assert findings(data, "r-1") == [("events.jsonl", 5, None, "unreadable")]
    report = data["runs"][0]
    assert any("torn" in problem for problem in report["problems"])


def save_state(repo: Path, run_id: str, status: str) -> None:
    """The run's run.json, as the engine writes it (only its status matters here)."""
    from datetime import UTC, datetime

    from agentic.engine.state import Cursor, RunState, RunStore

    now = datetime.now(UTC)
    state = RunState(
        run_id=run_id,
        pipeline="feature-delivery",
        pipeline_version="1.0.0",
        pipeline_digest="sha256:" + "0" * 64,
        policy_version=POLICY,
        catalog_digest="sha256:" + "1" * 64,
        regime="solo",
        starter="dev@example.invalid",
        status=status,
        cursor=Cursor(index=0),
        started_at=now,
        updated_at=now,
    )
    RunStore(repo / ".agentic" / "local" / "runs").save(state)


def torn(repo: Path, run_id: str) -> None:
    with events_file(repo, run_id).open("ab") as fh:
        fh.write(b'{"seq": 5, "event": "step_')


def test_active_run_with_a_half_written_event_is_not_broken(home, repo, monkeypatch, capsys):
    """DF-618: a running run's ledger is being written; its last line may be caught half
    written. It is retried once, then checked up to the last whole line and called active."""
    make_run(repo, "r-1")
    save_state(repo, "r-1", "running")
    torn(repo, "r-1")
    waits = []
    monkeypatch.setattr(audit, "_sleep", waits.append)
    monkeypatch.chdir(repo)

    code, data = verify(capsys)

    assert code == ExitCode.OK
    report = data["runs"][0]
    assert report["intact"] is True
    assert report["active"] is True
    assert report["entries"] == 4  # opened + 3 steps; the torn fifth is not counted
    assert "still being written" in report["note"]
    assert waits == [audit.TORN_RETRY_S]


def test_active_run_tampering_is_still_located(home, repo, monkeypatch, capsys):
    make_run(repo, "r-1")
    save_state(repo, "r-1", "running")
    path = events_file(repo, "r-1")
    entries = lines_of(path)
    entries[1]["attempt"] = 99
    rewrite(path, entries)
    torn(repo, "r-1")
    monkeypatch.setattr(audit, "_sleep", lambda s: None)
    monkeypatch.chdir(repo)

    code, data = verify(capsys)

    assert code == ExitCode.INVALID
    assert data["runs"][0]["active"] is True
    assert findings(data, "r-1") == [("events.jsonl", 2, 2, "content")]


def test_finished_run_with_a_torn_tail_is_broken(home, repo, monkeypatch, capsys):
    make_run(repo, "r-1")
    save_state(repo, "r-1", "completed")
    torn(repo, "r-1")
    monkeypatch.setattr(audit, "_sleep", lambda s: None)
    monkeypatch.chdir(repo)
    code, data = verify(capsys)
    assert code == ExitCode.INVALID
    assert data["runs"][0]["active"] is False


def test_active_human_output_says_so(home, repo, monkeypatch, capsys):
    make_run(repo, "r-1")
    save_state(repo, "r-1", "running")
    torn(repo, "r-1")
    monkeypatch.setattr(audit, "_sleep", lambda s: None)
    monkeypatch.chdir(repo)
    assert main(["audit", "verify"]) == ExitCode.OK
    assert "intact (active" in capsys.readouterr().out


def test_unknown_run_is_usage_error(home, repo, monkeypatch, capsys):
    make_run(repo, "r-1")
    monkeypatch.chdir(repo)
    assert main(["audit", "verify", "r-404"]) == ExitCode.USAGE
    assert main(["audit", "verify", "../escape"]) == ExitCode.USAGE


def test_human_output_names_run_file_entry_and_class(home, repo, monkeypatch, capsys):
    make_run(repo, "r-1")
    path = events_file(repo, "r-1")
    entries = lines_of(path)
    entries[1]["attempt"] = 7
    rewrite(path, entries)
    monkeypatch.chdir(repo)
    assert main(["audit", "verify"]) == ExitCode.INVALID
    out = capsys.readouterr().out
    assert "r-1" in out and "events.jsonl entry 2 (seq 2): content" in out


# ── the control plane's audit chain ───────────────────────────────────────────


@pytest.fixture
def plane(home, monkeypatch):
    """A signed-in auditor of tenant acme (the token from the environment, as in CI)."""
    fake = FakeControlPlane(allowed={"audit.read.org", "audit.export"})
    monkeypatch.setattr(control_plane, "make_transport", lambda: fake.transport)
    monkeypatch.setenv("AGENTIC_CONTROL_PLANE_TOKEN", TOKEN)
    monkeypatch.delenv("AGENTIC_CONTROL_PLANE_URL", raising=False)
    config = agentic_home()
    config.mkdir(parents=True, exist_ok=True)
    (config / "config.yaml").write_text(
        f"org: acme\ncontrol_plane_url: {CP_URL}\n", encoding="utf-8"
    )
    return fake


def test_control_plane_chain_over_several_pages(plane, monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(audit, "PAGE", 4)
    plane.add_audit(10)
    monkeypatch.chdir(tmp_path)  # no repository needed for the org chain
    code, data = verify(capsys, "--control-plane")
    assert code == ExitCode.OK, data
    chain = data["control_plane"]
    assert chain["intact"] and chain["records"] == 10 and chain["tenant"] == "acme"
    assert chain["head"]["seq"] == 10 and data["runs"] == []
    pages = [r for r in plane.requests if r.url.path == "/v1/audit"]
    assert [r.url.params["after_seq"] for r in pages] == ["0", "4", "8"]


def test_edited_control_plane_record_is_located(plane, monkeypatch, capsys, tmp_path):
    plane.add_audit(5)
    plane.audit[2]["reason"] = "rewritten by someone with database access"
    monkeypatch.chdir(tmp_path)
    code, data = verify(capsys, "--control-plane")
    assert code == ExitCode.INVALID
    found = [(f["seq"], f["field_class"]) for f in data["control_plane"]["findings"]]
    assert found == [(3, "content")]


def test_control_plane_refusal_exits_2(plane, monkeypatch, capsys, tmp_path):
    plane.allowed = set()
    monkeypatch.chdir(tmp_path)
    assert main(["audit", "verify", "--control-plane"]) == ExitCode.REFUSED
    assert "audit.read.org" in capsys.readouterr().err


def test_control_plane_needs_org_mode(home, monkeypatch, capsys, tmp_path):
    monkeypatch.chdir(tmp_path)
    assert main(["audit", "verify", "--control-plane"]) == ExitCode.REFUSED
