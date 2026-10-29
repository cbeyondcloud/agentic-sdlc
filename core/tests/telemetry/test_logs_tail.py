"""Following a run's step logs while it runs (`agentic logs --follow`, M1-47): only new,
whole records, oldest first; a truncated file is read again; records redacted on read."""

import json
from pathlib import Path

from agentic.telemetry.logs import LogTail, read_logs, redact_record

SECRET = "tl-" + "planted-secret-value-77"  # gitleaks:allow (not a real key)


def record(step: str, seq: int, message: str, level: str = "info", **extra) -> dict:
    return {
        "at": f"2026-10-21T10:00:00.{seq:06d}+00:00",
        "seq": seq,
        "level": level,
        "logger": "agentic.test",
        "run_id": "r-1",
        "step": step,
        "iteration": 1,
        "message": message,
        "fields": {},
        "error": None,
        **extra,
    }


def append(run_dir: Path, name: str, *records: dict, raw: bytes = b"") -> None:
    path = run_dir / "logs" / f"{name}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("ab") as handle:
        for r in records:
            handle.write(json.dumps(r).encode("utf-8") + b"\n")
        handle.write(raw)


def messages(records: list[dict]) -> list[str]:
    return [r["message"] for r in records]


def test_poll_returns_only_new_records_oldest_first_across_files(tmp_path: Path):
    tail = LogTail(tmp_path)
    assert tail.poll() == []  # no logs folder yet
    append(tmp_path, "design-1", record("design", 2, "b"))
    append(tmp_path, "build-1", record("build", 1, "a"))
    assert messages(tail.poll()) == ["a", "b"]
    assert tail.poll() == []
    append(tmp_path, "design-1", record("design", 3, "c"))
    assert messages(tail.poll()) == ["c"]


def test_tail_reads_whole_lines_only(tmp_path: Path):
    tail = LogTail(tmp_path)
    line = json.dumps(record("s", 1, "whole")).encode("utf-8")
    append(tmp_path, "s-1", raw=line[:10])
    assert tail.poll() == []  # half written: held back until its newline arrives
    append(tmp_path, "s-1", raw=line[10:] + b"\n")
    assert messages(tail.poll()) == ["whole"]


def test_tail_restarts_a_truncated_file(tmp_path: Path):
    tail = LogTail(tmp_path)
    append(tmp_path, "s-1", record("s", 1, "one"), record("s", 2, "two"))
    assert messages(tail.poll()) == ["one", "two"]
    (tmp_path / "logs" / "s-1.jsonl").write_bytes(b"")
    append(tmp_path, "s-1", record("s", 3, "again"))
    assert messages(tail.poll()) == ["again"]


def test_a_final_poll_shows_a_partial_last_line_as_an_error(tmp_path: Path):
    append(tmp_path, "s-1", record("s", 1, "ok"), raw=b'{"cut off')
    assert messages(LogTail(tmp_path).poll()) == ["ok"]
    final = read_logs(tmp_path)
    assert sorted(messages(final)) == ["ok", "s-1.jsonl:2: not JSON"]
    assert [r["level"] for r in final if r["message"] != "ok"] == ["error"]


def test_step_and_level_filters(tmp_path: Path):
    append(tmp_path, "build-1", record("build", 1, "b1", level="debug"))
    append(tmp_path, "build-2", record("build", 2, "b2", level="warning"))
    append(tmp_path, "builder-1", record("builder", 3, "other step"))
    assert messages(LogTail(tmp_path, step="build").poll()) == ["b1", "b2"]
    assert messages(LogTail(tmp_path, step="build-2").poll()) == ["b2"]
    assert messages(LogTail(tmp_path, step="build", min_level="info").poll()) == ["b2"]


def test_crlf_in_a_file_does_not_break_reading(tmp_path: Path):
    """A log copied through a Windows tool may have CRLF line ends."""
    path = tmp_path / "logs" / "s-1.jsonl"
    path.parent.mkdir(parents=True)
    path.write_bytes(json.dumps(record("s", 1, "crlf")).encode("utf-8") + b"\r\n")
    assert messages(LogTail(tmp_path).poll()) == ["crlf"]


def test_redact_record_masks_text_and_keeps_numbers():
    def mask(text: str) -> str:
        return text.replace(SECRET, "<redacted>")

    found = record(
        "s",
        1,
        f"key {SECRET}",
        fields={"header": f"Bearer {SECRET}", "n": 2, "nested": [{"k": SECRET}]},
        error=f"Traceback ... {SECRET}",
    )
    clean = redact_record(found, mask)
    assert SECRET not in json.dumps(clean)
    assert clean["fields"]["n"] == 2 and clean["seq"] == 1 and clean["iteration"] == 1
    assert clean["message"] == "key <redacted>"
    assert found["message"] == f"key {SECRET}"  # the input is not changed


def test_redact_record_never_returns_raw_text_when_the_redactor_fails():
    def broken(text: str) -> str:
        raise ValueError("boom")

    clean = redact_record(record("s", 1, f"key {SECRET}"), broken)
    assert SECRET not in json.dumps(clean)


def test_held_counts_the_bytes_of_unfinished_last_lines(tmp_path: Path):
    """DF-282: what a non-final poll held back, so `agentic logs` can say it is there."""
    tail = LogTail(tmp_path)
    assert tail.held == 0
    append(tmp_path, "a-1", record("a", 1, "whole"), raw=b'{"at": "2026')
    append(tmp_path, "b-1", raw=b'{"at"')
    assert messages(tail.poll()) == ["whole"]
    assert tail.held == 17
    tail.poll(final=True)
    assert tail.held == 0
