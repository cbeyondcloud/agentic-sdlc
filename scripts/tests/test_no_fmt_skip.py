"""The owner's rule: never a formatter skip/off comment - ruff format wraps every line. This
test enforces it on every tracked Python file, so the rule does not depend on memory."""

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
# built from pieces so this file does not trip its own check
MARKER = re.compile(r"#\s*fmt:\s*(" + "skip" + "|" + "off" + ")\\b")


def markers(paths: list[Path]) -> list[str]:
    """`path:line` for every formatter-suppression comment in `paths`."""
    found = []
    for path in paths:
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if MARKER.search(line):
                found.append(f"{path.relative_to(ROOT).as_posix()}:{number}")
    return found


def test_a_suppression_comment_is_found(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("test_no_fmt_skip.ROOT", tmp_path)
    sample = tmp_path / "a.py"
    sample.write_text("x = [1,\n  2]  # fmt" + ": skip\n", encoding="utf-8")
    assert markers([sample]) == ["a.py:2"]


def test_no_python_file_suppresses_the_formatter():
    tracked = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "*.py"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    assert markers([ROOT / p for p in tracked if (ROOT / p).is_file()]) == []
