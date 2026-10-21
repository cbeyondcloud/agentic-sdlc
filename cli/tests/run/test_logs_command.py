"""`agentic logs <run> [--step] [--level] [--follow]` (M1-47): records redacted again on
read, JSON (one document, or JSON Lines while following), and a follow that stops when
the run stops running. The runs and log files are written directly; no model is called."""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from agentic.engine.state import Cursor, RunState, RunStore
from agentic.telemetry.logs import LogTail
from agentic_cli.commands import runs
from agentic_cli.exit_codes import ExitCode
from agentic_cli.workspace import RUNS

SECRET = "acme-" + "planted-secret-0042"  # gitleaks:allow (not a real key)


def save_run(repo: Path, run_id: str, status: str) -> Path:
    now = datetime.now(UTC)
    state = RunState(
        run_id=run_id,
        pipeline="deliver",
        pipeline_version="1.0.0",
        pipeline_digest="sha256:" + "0" * 64,
        policy_version="sha256:" + "1" * 64,
        catalog_digest="sha256:" + "2" * 64,
        regime="team",
        starter="dev@example.invalid",
        status=status,
        cursor=Cursor(index=0),
        started_at=now,
        updated_at=now,
    )
    store = RunStore(repo / RUNS)
    store.save(state)
    return store.path(run_id).parent


def log(run_dir: Path, step: str, seq: int, message: str, level: str = "info") -> None:
    record = {
        "at": f"2026-10-21T10:00:00.{seq:06d}+00:00",
        "seq": seq,
        "level": level,
        "logger": "agentic.engine",
        "run_id": run_dir.name,
        "step": step,
        "iteration": 1,
        "message": message,
        "fields": {"n": seq},
        "error": None,
    }
    path = run_dir / "logs" / f"{step}-1.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(record) + "\n")


@pytest.fixture
def no_sleep(monkeypatch):
    """Replace the follow loop's sleep; the test's callback runs between polls."""
    between: list = []

    def sleep(seconds: float) -> None:
        assert seconds == runs.FOLLOW_INTERVAL_S
        if between:
            between.pop(0)()

    monkeypatch.setattr(runs, "_sleep", sleep)
    return between


def test_logs_redact_again_on_read(workspace, cli, monkeypatch):
    monkeypatch.setenv("ACME_API_KEY", SECRET)
    run_dir = save_run(workspace, "r-old", "completed")
    log(run_dir, "build", 1, f"old writer kept {SECRET}")
    human = cli("logs", "r-old")
    assert human.code == ExitCode.OK, human.err
    assert SECRET not in human.out and "old writer kept" in human.out
    shown = cli("--json", "logs", "r-old")
    assert SECRET not in shown.out
    assert shown.json()[0]["fields"] == {"n": 1}


def test_follow_on_a_finished_run_prints_everything_and_exits_0(workspace, cli, no_sleep):
    run_dir = save_run(workspace, "r-done", "completed")
    log(run_dir, "build", 1, "first")
    log(run_dir, "review", 2, "second")
    result = cli("logs", "r-done", "--follow")
    assert result.code == ExitCode.OK, result.err
    assert result.out.index("first") < result.out.index("second")
    assert "completed" in result.err  # how the run ended, on stderr


def test_follow_prints_records_written_while_running_then_stops(workspace, cli, no_sleep):
    run_dir = save_run(workspace, "r-live", "running")
    log(run_dir, "build", 1, "before follow")

    def more() -> None:
        log(run_dir, "build", 2, "while running")

    def finish() -> None:
        log(run_dir, "review", 3, "last words")  # written before the run is saved as paused
        save_run(workspace, "r-live", "awaiting_gate")

    no_sleep.extend([more, finish])
    result = cli("--json", "logs", "r-live", "--follow")
    assert result.code == ExitCode.PAUSED, result.err  # the run's own exit code (DF-285)
    lines = [json.loads(line) for line in result.out.splitlines()]  # JSON Lines
    assert [r["message"] for r in lines] == ["before follow", "while running", "last words"]
    assert no_sleep == []  # both callbacks ran: it kept following while running


def test_follow_reads_the_status_before_the_poll_so_the_last_records_are_kept(
    workspace, cli, no_sleep, monkeypatch
):
    """The run writes its last records and stops right AFTER a poll returned (review F2):
    a follow that polled first and read the status second would stop without them."""
    run_dir = save_run(workspace, "r-race", "running")
    log(run_dir, "build", 1, "before follow")
    original = LogTail.poll
    finished: list[bool] = []

    def poll(self, final: bool = False) -> list[dict]:
        records = original(self, final)
        if not finished:  # the run finishes between this poll and the next status read
            finished.append(True)
            log(run_dir, "build", 2, "late record")
            log(run_dir, "build", 3, "last line, no newline")
            path = run_dir / "logs" / "build-1.jsonl"
            path.write_bytes(path.read_bytes().rstrip(b"\n"))  # the writer is mid-flush
            save_run(workspace, "r-race", "completed")
        return records

    monkeypatch.setattr(LogTail, "poll", poll)
    result = cli("--json", "logs", "r-race", "--follow")
    assert result.code == ExitCode.OK, result.err
    messages = [json.loads(line)["message"] for line in result.out.splitlines()]
    assert messages == ["before follow", "late record", "last line, no newline"]


@pytest.mark.parametrize(
    ("status", "code"),
    [("failed", ExitCode.ERROR), ("stopped", ExitCode.STOPPED), ("completed", ExitCode.OK)],
)
def test_follow_exits_with_how_the_run_ended(workspace, cli, no_sleep, status, code):
    """A script waiting on `logs --follow` learns the outcome from the exit code, as it
    would from `run` (DF-285 a); plain `logs` is a read and exits 0."""
    run_dir = save_run(workspace, f"r-{status}", status)
    log(run_dir, "build", 1, "done")
    assert cli("--json", "logs", f"r-{status}", "--follow").code == code
    assert cli("logs", f"r-{status}", "--follow").code == code
    assert cli("logs", f"r-{status}").code == ExitCode.OK


def test_follow_with_a_step_and_level_filter(workspace, cli, no_sleep):
    run_dir = save_run(workspace, "r-f", "failed")
    log(run_dir, "build", 1, "noise", level="debug")
    log(run_dir, "build", 2, "kept", level="warning")
    log(run_dir, "review", 3, "other step", level="error")
    result = cli("--json", "logs", "r-f", "--follow", "--step", "build", "--level", "info")
    assert [json.loads(line)["message"] for line in result.out.splitlines()] == ["kept"]


def test_logs_of_an_unknown_run_is_an_error_with_a_hint(workspace, cli):
    result = cli("logs", "no-such-run", "--follow")
    assert result.code == ExitCode.ERROR and "agentic runs" in result.err


def test_a_terminal_control_sequence_in_a_log_is_made_inert(workspace, cli):
    run_dir = save_run(workspace, "r-esc", "completed")
    log(run_dir, "build", 1, "title \x1b]0;pwned\x07 done")
    result = cli("logs", "r-esc")
    assert "\x1b" not in result.out and "\x07" not in result.out


def test_a_half_written_line_of_a_running_run_is_not_shown_as_an_error(workspace, cli):
    run_dir = save_run(workspace, "r-run", "running")
    log(run_dir, "build", 1, "whole")
    with (run_dir / "logs" / "build-1.jsonl").open("a", encoding="utf-8") as handle:
        handle.write('{"at": "2026-10-21T10:00:01')  # the writer is mid-record
    records = cli("--json", "logs", "r-run").json()
    assert [r["message"] for r in records] == ["whole"]
    save_run(workspace, "r-run", "failed")  # it stopped there: now the line is shown
    assert len(cli("--json", "logs", "r-run").json()) == 2
