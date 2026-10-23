"""Gate UX (M1-55): approve, reject --feedback, edit (re-validated before approval), show
(handoff and diff), and the waiting list across runs (DF-99). Each verb is checked in the
ledgers: an approvals.jsonl entry and a gate_decided event."""

import json
import sys
from pathlib import Path

import pytest
from _cli_support import FakeGateway, answer, git, write

from agentic_cli import runtime
from agentic_cli.errors import CliError
from agentic_cli.exit_codes import ExitCode

KEY = "tl-" + "fake-gateway-key-0003"  # gitleaks:allow (not a real key)
SUMMARY = "One line saying what this handoff concludes."

EDITOR_SCRIPT = """\
import os, sys
path = sys.argv[1]
if os.environ.get("EDIT_MARKER"):
    open(os.environ["EDIT_MARKER"], "w").close()
with open(path, encoding="utf-8-sig") as handle:
    text = handle.read()
text = text.replace(os.environ["EDIT_FROM"], os.environ["EDIT_TO"])
encoding = os.environ.get("EDIT_ENCODING", "utf-8")
with open(path, "w", encoding=encoding, newline="\\n") as handle:
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
    assert drafts(workspace, run_id) == []  # nothing worth keeping


def drafts(workspace: Path, run_id: str) -> list[Path]:
    folder = workspace / ".agentic" / "local" / "state" / run_id / "edit"
    return sorted(p for p in folder.rglob("*") if p.is_file()) if folder.is_dir() else []


def test_edit_checks_the_gateway_before_the_editor_opens(
    workspace, gateway, cli, editor, tmp_path, monkeypatch
):
    """A missing credential is found before the person spends time editing (DF-220)."""
    run_id = started(cli)
    monkeypatch.delenv("AGENTIC_TOKENLEAN_API_KEY")
    marker = tmp_path / "editor-ran"
    monkeypatch.setenv("EDIT_MARKER", str(marker))
    editor(SUMMARY, "Edited by a person.")
    result = cli("edit", run_id)
    assert result.code == ExitCode.REFUSED and "agentic login" in result.err
    assert not marker.exists()


def test_a_refused_edit_is_kept_and_the_next_edit_continues_from_it(
    workspace, gateway, cli, editor
):
    run_id = started(cli)
    editor("status: complete", "status: maybe")
    result = cli("edit", run_id)
    assert result.code == ExitCode.REFUSED and "not a valid handoff" in result.err
    [draft] = drafts(workspace, run_id)
    assert "status: maybe" in draft.read_text("utf-8") and draft.name in result.err
    # the person had also changed the summary; the next edit starts from their draft
    draft.write_text(draft.read_text("utf-8").replace(SUMMARY, "Fixed by a person."), "utf-8")
    editor("status: maybe", "status: complete")
    result = cli("--json", "edit", run_id)
    assert result.code == ExitCode.OK, result.err
    entries, _ = ledgers(workspace, run_id)
    edited = workspace / ".agentic" / "local" / "runs" / run_id / list(entries[-1]["artifacts"])[0]
    assert "Fixed by a person." in edited.read_text("utf-8")
    assert drafts(workspace, run_id) == []  # decided: the draft is gone


def test_an_edit_recorded_before_a_later_failure_does_not_point_at_a_stale_draft(
    workspace, gateway, cli, editor, monkeypatch
):
    """DF-348: the engine recorded the edit, then the run failed further on. The draft is
    no longer wanted (the next `agentic edit` would not offer it) and the message must not
    say it was kept."""
    from agentic_cli.commands import gates

    run_id = started(cli)
    real = gates.drive

    def then_fail(*args, **kwargs):
        real(*args, **kwargs)  # the decision is recorded and the run goes on
        raise CliError("the next step failed", ExitCode.ERROR, hint="see agentic logs")

    monkeypatch.setattr(gates, "drive", then_fail)
    editor(SUMMARY, "Edited by a person.")
    result = cli("edit", run_id)
    assert result.code == ExitCode.ERROR and "the next step failed" in result.err
    assert "kept" not in result.err
    assert drafts(workspace, run_id) == []
    entries, _ = ledgers(workspace, run_id)
    assert entries[-1]["verdict"] == "approve"  # the edit itself was recorded


def test_an_edit_saved_in_another_encoding_is_refused_and_kept(
    workspace, gateway, cli, editor, monkeypatch
):
    run_id = started(cli)
    monkeypatch.setenv("EDIT_ENCODING", "cp1252")
    editor(SUMMARY, "Résumé of the change.")
    result = cli("--json", "edit", run_id)
    assert result.code == ExitCode.INVALID, result.err
    error = json.loads(result.err.strip().splitlines()[-1])
    assert "UTF-8" in error["error"] and "agentic edit" in error["hint"]
    assert len(drafts(workspace, run_id)) == 1
    assert ledgers(workspace, run_id)[0] == []


def test_an_edit_saved_with_a_byte_order_mark_is_accepted(
    workspace, gateway, cli, editor, monkeypatch
):
    """Windows editors may save UTF-8 with a BOM; the handoff is read without it."""
    run_id = started(cli)
    monkeypatch.setenv("EDIT_ENCODING", "utf-8-sig")
    editor(SUMMARY, "Edited in Notepad.")
    result = cli("--json", "edit", run_id)
    assert result.code == ExitCode.OK, result.err


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


def built(policy_repo, gateway, cli, text: str, again: list | None = None) -> str:
    write(policy_repo / "pipelines" / "build.yaml", BUILD)
    write(
        policy_repo / "agents" / "builder.md",
        "---\nname: builder\ntype: general\nversion: 1.0.0\ndescription: Builds.\n"
        "tools: [read_files, edit_files]\nconsumes: []\n---\nBuild it.\n",
    )
    gateway.answers["builder"] = [
        {"tool": "write_file", "args": {"path": "src/new.py", "text": text}},
        "built it",
        *(again or []),
    ]
    result = cli("--json", "run", "build")
    assert result.code == ExitCode.PAUSED, result.err
    return result.json()["run_id"]


MARKER_SCRIPT = "import sys\nopen(sys.argv[1], 'w').close()\n"


def test_show_never_runs_a_diff_driver(workspace, policy_repo, gateway, cli, tmp_path, monkeypatch):
    """An external diff or textconv program configured for the repository is not run
    when a person looks at a gate (DF-222 e)."""
    run_id = built(policy_repo, gateway, cli, "y = 2\n")
    script = write(tmp_path / "marker.py", MARKER_SCRIPT).as_posix()
    marker = (tmp_path / "driver-ran").as_posix()
    command = f'"{Path(sys.executable).as_posix()}" "{script}" "{marker}"'
    monkeypatch.setenv("GIT_EXTERNAL_DIFF", command)
    git(workspace, "config", "diff.spy.textconv", command)
    write(workspace / ".git" / "info" / "attributes", "* diff=spy\n")
    shown = cli("--json", "show", run_id).json()
    assert "+y = 2" in shown["diff"]
    assert not Path(marker).exists()


def test_show_cuts_a_huge_diff_on_the_terminal_with_a_note(workspace, policy_repo, gateway, cli):
    # far over the review batch budget (M2-24): split once, then a person sees it at the
    # diff-budget gate - the builder writes it again when asked to split
    huge = {"tool": "write_file", "args": {"path": "src/new.py", "text": "x = 1\n" * 60_000}}
    run_id = built(policy_repo, gateway, cli, huge["args"]["text"], again=[huge, "built it"])
    assert cli("--json", "show", run_id).json()["gate"] == "diff-budget"
    human = cli("--no-color", "show", run_id).out
    assert "diff cut at 256 KiB" in human and "agentic --json show" in human
    assert human.count("+x = 1") < 60_000
    shown = cli("--json", "show", run_id).json()  # all of it, for a script or a UI
    assert shown["diff"].count("+x = 1") == 60_000


ESCAPES = "\x1b]0;pwned\x07\x1b[1A\x1b[2K\x9b2K"


def test_show_does_not_pass_terminal_control_sequences_through(
    workspace, policy_repo, gateway, cli
):  # F2: an agent cannot hide lines from the approver
    write(policy_repo / "pipelines" / "build.yaml", BUILD)
    write(
        policy_repo / "agents" / "builder.md",
        "---\nname: builder\ntype: general\nversion: 1.0.0\ndescription: Builds.\n"
        "tools: [read_files, edit_files]\nconsumes: []\n---\nBuild it.\n",
    )
    gateway.answers["builder"] = [
        {"tool": "write_file", "args": {"path": "src/new.py", "text": f"y = '{ESCAPES}hidden'\n"}},
        f"built it{ESCAPES}fake approval line",
    ]
    result = cli("--json", "run", "build")
    assert result.code == ExitCode.PAUSED, result.err
    run_id = result.json()["run_id"]
    shown = cli("show", run_id).out
    assert "hidden" in shown and "fake approval line" in shown
    assert not any(c in shown for c in "\x1b\x07\x9b"), repr(shown)
    logs = cli("logs", run_id).out
    assert not any(c in logs for c in "\x1b\x07\x9b"), repr(logs)


def test_waiting_lists_gates_across_runs(workspace, gateway, cli):
    first, second = started(cli), started(cli)
    rows = cli("--json", "waiting").json()
    assert {r["run_id"] for r in rows} == {first, second}
    assert all(r["gate"] == "requirements-approval" for r in rows)
    assert all(r["approver_role"] == "team-lead" for r in rows)
    cli("approve", first)
    assert [r["run_id"] for r in cli("--json", "waiting").json()] == [second]
    assert second in cli("waiting").out
