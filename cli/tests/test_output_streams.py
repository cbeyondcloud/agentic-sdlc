"""Output on a console or pipe that is not UTF-8 (a Windows pipe or legacy code page)
never crashes a command, and JSON stays exact there (M1-47: logs readable on Windows and
macOS terminals)."""

import io
import json
import os
import subprocess
import sys
from pathlib import Path

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


ENTRY = "from agentic_cli.main import run\nrun()\n"


def test_json_through_a_cp1252_pipe_is_exact_end_to_end(tmp_path):
    """The whole command, as a Windows pipe runs it: Zyroflo's `tools` package turns the
    streams to UTF-8 when it is imported, so the decision is taken from the encoding the
    process started with (DF-283). A folder named with non-ASCII letters and spaces (a
    user name like José) comes back exactly."""
    folder, done = _status_from_a_non_ascii_folder(tmp_path, "cp1252")
    assert done.returncode == 2, done.stderr
    assert done.stderr.isascii(), done.stderr
    error = json.loads(done.stderr.decode("ascii"))
    assert str(folder) in error["error"] and error["exit_code"] == 2


def test_json_through_a_utf8_pipe_keeps_the_text_readable(tmp_path):
    """A macOS or Linux pipe (UTF-8) gets the characters themselves, as before."""
    folder, done = _status_from_a_non_ascii_folder(tmp_path, "utf-8")
    assert done.returncode == 2, done.stderr
    assert "Jösé Ünïcode 日本" in done.stderr.decode("utf-8")
    assert str(folder) in json.loads(done.stderr.decode("utf-8"))["error"]


def test_result_json_on_stdout_through_a_cp1252_pipe_is_exact(tmp_path):
    """DF-354: the same for a result on stdout (not only an error on stderr): a created
    file's path under a non-ASCII folder comes back exactly through a Windows pipe."""
    import shutil

    from cli_helpers import FIXTURES, GIT

    entry = tmp_path / "entry.py"
    entry.write_text(ENTRY, encoding="utf-8")
    repo = tmp_path / "Jösé Ünïcode 日本"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "a.py").write_text("x = 1\n", encoding="utf-8")
    for args in (["init", "-q", "-b", "main"], ["add", "-A"], ["commit", "-qm", "base"]):
        subprocess.run(["git", "-C", str(repo), *GIT, *args], check=True, capture_output=True)
    policy = tmp_path / "policy-repo"
    shutil.copytree(FIXTURES / "policy", policy)
    env = {
        **os.environ,
        "PYTHONIOENCODING": "cp1252",
        "PYTHONUTF8": "0",
        "AGENTIC_HOME": str(tmp_path / "home"),
        "AGENTIC_POLICY_REPO": str(policy),
        "AGENTIC_USER": "dev@example.invalid",
    }
    done = subprocess.run(
        [sys.executable, str(entry), "--json", "pipelines", "new", "docs-only"],
        cwd=repo,
        env=env,
        capture_output=True,
        timeout=120,
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.isascii(), done.stdout
    created = Path(json.loads(done.stdout.decode("ascii"))["path"])
    assert created.is_file() and "Jösé Ünïcode 日本" in str(created)


def _status_from_a_non_ascii_folder(tmp_path, encoding: str):
    entry = tmp_path / "entry.py"
    entry.write_text(ENTRY, encoding="utf-8")
    folder = tmp_path / "Jösé Ünïcode 日本"
    folder.mkdir()
    env = {**os.environ, "PYTHONIOENCODING": encoding, "PYTHONUTF8": "0"}
    done = subprocess.run(
        [sys.executable, str(entry), "--json", "status"],
        cwd=folder,
        env=env,
        capture_output=True,
        timeout=120,
    )
    return folder, done
