"""Gate UX (M1-55): approve, reject --feedback, edit (re-validated before approval), show
(handoff and diff), and the waiting list across runs (DF-99). Each verb is checked in the
ledgers: an approvals.jsonl entry and a gate_decided event."""

import json
import sys
from pathlib import Path

import pytest
from _cli_support import FakeGateway, answer, write

from agentic_cli import runtime
from agentic_cli.exit_codes import ExitCode

KEY = "tl-" + "fake-gateway-key-0003"  # gitleaks:allow (not a real key)
SUMMARY = "One line saying what this handoff concludes."

EDITOR_SCRIPT = """\
import os, sys
path = sys.argv[1]
with open(path, encoding="utf-8") as handle:
    text = handle.read()
text = text.replace(os.environ["EDIT_FROM"], os.environ["EDIT_TO"])
with open(path, "w", encoding="utf-8", newline="\\n") as handle:
    handle.write(text)
"""


@pytest.fixture
def gateway(workspace, monkeypatch) -> FakeGateway:
    fake = FakeGateway(
        {
            "ba": [answer("requirements"), answer("requirements", summary="Second try.")],
            "architect": [answer("design")],
        }
    )
    monkeypatch.setenv("AGENTIC_GATEWAY_URL", "https://tokenlean.example")
    monkeypatch.setenv("AGENTIC_TOKENLEAN_API_KEY", KEY)
    monkeypatch.setattr(runtime, "network_transport", fake.transport)
    return fake


@pytest.fixture
def editor(tmp_path: Path, monkeypatch):
    script = write(tmp_path / "fake_editor.py", EDITOR_SCRIPT)
    monkeypatch.delenv("VISUAL", raising=False)
    monkeypatch.setenv("EDITOR", f'"{sys.executable}" "{script}"')

    def change(old: str, new: str) -> None:
        monkeypatch.setenv("EDIT_FROM", old)
        monkeypatch.setenv("EDIT_TO", new)

    return change


def started(cli) -> str:
    result = cli("--json", "run", "deliver", "--input", "request=CSV export")
    assert result.code == ExitCode.PAUSED, result.err
    return result.json()["run_id"]


def ledgers(workspace: Path, run_id: str) -> tuple[list[dict], list[str]]:
    folder = workspace / ".agentic" / "local" / "runs" / run_id

    def lines(name: str) -> list[dict]:
        path = folder / name
        text = path.read_text("utf-8") if path.is_file() else ""
        return [json.loads(line) for line in text.splitlines()]

    events = lines("events.jsonl")
    return lines("approvals.jsonl"), [f"{e['event']}:{e.get('decision', '')}" for e in events]


def test_approve_records_the_decision_and_continues(workspace, gateway, cli):
    run_id = started(cli)
    result = cli("--json", "approve", run_id, "--notes", "looks right")
    assert result.code == ExitCode.OK, result.err
    assert result.json()["status"] == "completed"
    entries, events = ledgers(workspace, run_id)
    assert [(e["gate"], e["verdict"], e["approver"]) for e in entries] == [
        ("requirements-approval", "approve", "dev@example.invalid")
    ]
    assert "gate_decided:approve" in events
    assert [r["agent"] for r in gateway.requests] == ["ba", "architect"]


def test_reject_sends_the_feedback_back_to_the_step(workspace, gateway, cli):
    run_id = started(cli)
    result = cli("--json", "reject", run_id, "--feedback", "cover the CSV header")
    assert result.code == ExitCode.PAUSED, result.err
    shown = result.json()
    assert shown["pending"]["id"] == "requirements-approval"
    assert [h["step"] for h in shown["history"]] == ["requirements", "requirements"]
    entries, events = ledgers(workspace, run_id)
    assert entries[-1]["verdict"] == "reject" and entries[-1]["notes"] == "cover the CSV header"
    assert "gate_decided:reject" in events


def test_reject_needs_feedback(workspace, gateway, cli):
    run_id = started(cli)
    assert cli("reject", run_id).code == ExitCode.USAGE


def test_edit_revalidates_then_approves(workspace, gateway, cli, editor):
    run_id = started(cli)
    editor(SUMMARY, "Edited by a person.")
    result = cli("--json", "edit", run_id)
    assert result.code == ExitCode.OK, result.err
    entries, _ = ledgers(workspace, run_id)
    assert entries[-1]["verdict"] == "approve"
    pinned = list(entries[-1]["artifacts"])
    assert pinned == ["handoffs/requirements-1.md.edited-1.md"]
    edited = workspace / ".agentic" / "local" / "runs" / run_id / pinned[0]
    assert "Edited by a person." in edited.read_text("utf-8")


def test_an_invalid_edit_is_refused_and_nothing_is_recorded(workspace, gateway, cli, editor):
    run_id = started(cli)
    editor("status: complete", "status: maybe")
    result = cli("edit", run_id)
    assert result.code == ExitCode.REFUSED
    assert "not a valid handoff" in result.err
    entries, events = ledgers(workspace, run_id)
    assert entries == [] and not [e for e in events if e.startswith("gate_decided")]
    assert cli("--json", "status", run_id).json()["status"] == "awaiting_gate"


def test_an_unchanged_edit_is_refused(workspace, gateway, cli, editor):
    run_id = started(cli)
    editor("text that is not there", "x")
    result = cli("edit", run_id)
    assert result.code == ExitCode.REFUSED and "nothing changed" in result.err


def test_a_decision_on_another_gate_is_refused(workspace, gateway, cli):
    run_id = started(cli)
    result = cli("approve", run_id, "--gate", "design-approval")
    assert result.code == ExitCode.REFUSED and "requirements-approval" in result.err


def test_approve_when_not_at_a_gate_is_refused(workspace, gateway, cli):
    run_id = started(cli)
    cli("approve", run_id)
    result = cli("approve", run_id)
    assert result.code == ExitCode.REFUSED and "not waiting at a gate" in result.err


def test_show_renders_the_pinned_handoff(workspace, gateway, cli):
    run_id = started(cli)
    shown = cli("--json", "show", run_id).json()
    assert shown["gate"] == "requirements-approval"
    assert [h["path"] for h in shown["handoffs"]] == ["handoffs/requirements-1.md"]
    assert SUMMARY in shown["handoffs"][0]["text"]
    human = cli("show", run_id).out
    assert "requirements-approval" in human and SUMMARY in human


BUILD = """name: build
version: 1.0.0
regime: team
steps:
  - id: build
    agent: builder
  - gate: code-review
    approver_role: approver
"""


def test_show_includes_the_diff_of_the_gated_steps(workspace, policy_repo, gateway, cli):
    write(policy_repo / "pipelines" / "build.yaml", BUILD)
    write(
        policy_repo / "agents" / "builder.md",
        "---\nname: builder\ntype: general\nversion: 1.0.0\ndescription: Builds.\n"
        "tools: [read_files, edit_files]\nconsumes: []\n---\nBuild it.\n",
    )
    gateway.answers["builder"] = [
        {"tool": "write_file", "args": {"path": "src/new.py", "text": "y = 2\n"}},
        "built it",
    ]
    result = cli("--json", "run", "build")
    assert result.code == ExitCode.PAUSED, result.err
    run_id = result.json()["run_id"]
    shown = cli("--json", "show", run_id).json()
    assert "src/new.py" in shown["diff"] and "+y = 2" in shown["diff"]
    assert "src/new.py" in cli("show", run_id).out


def test_waiting_lists_gates_across_runs(workspace, gateway, cli):
    first, second = started(cli), started(cli)
    rows = cli("--json", "waiting").json()
    assert {r["run_id"] for r in rows} == {first, second}
    assert all(r["gate"] == "requirements-approval" for r in rows)
    assert all(r["approver_role"] == "team-lead" for r in rows)
    cli("approve", first)
    assert [r["run_id"] for r in cli("--json", "waiting").json()] == [second]
    assert second in cli("waiting").out
