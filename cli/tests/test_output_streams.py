"""Output on a console or pipe that is not UTF-8 (a Windows pipe or legacy code page)
never crashes a command, and JSON stays exact there (M1-47: logs readable on Windows and
macOS terminals)."""

import io
import json

from agentic_cli import output
from agentic_cli.output import Output, safe_streams


def cp1252_stream() -> io.TextIOWrapper:
    return io.TextIOWrapper(io.BytesIO(), encoding="cp1252", newline="\n")


def test_safe_streams_replace_unencodable_text():
    stream = cp1252_stream()
    safe_streams(stream)
    stream.write("step → done 日本\n")  # would raise UnicodeEncodeError before
    stream.flush()
    assert stream.buffer.getvalue().decode("cp1252") == "step ? done ??\n"


def test_safe_streams_leave_a_utf8_stream_alone():
    stream = io.TextIOWrapper(io.BytesIO(), encoding="utf-8")
    safe_streams(stream)
    assert stream.errors == "strict"


def test_json_is_ascii_escaped_on_a_stream_that_is_not_utf8(monkeypatch, capsys):
    monkeypatch.setattr(output, "_utf8", lambda stream: False)
    Output(json_mode=True).result({"message": "step → 日本"})
    printed = capsys.readouterr().out
    assert printed.isascii() and json.loads(printed) == {"message": "step → 日本"}


def test_stream_prints_one_compact_json_line_per_record(capsys):
    out = Output(json_mode=True)
    out.stream({"a": 1}, "ignored in JSON mode")
    out.stream({"b": "→"}, "ignored")
    lines = capsys.readouterr().out.splitlines()
    assert [json.loads(line) for line in lines] == [{"a": 1}, {"b": "→"}]


def test_stream_prints_the_text_without_json(capsys):
    Output(json_mode=False, color=False).stream({"a": 1}, "a line\n")
    assert capsys.readouterr().out == "a line\n"
