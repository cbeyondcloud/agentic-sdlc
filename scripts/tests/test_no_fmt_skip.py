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


def python_files(root: Path) -> list[Path]:
    """The repository's Python files: git's view when there is a repository, else (the
    Linux CI container, which gets exactly the tracked files but no .git) every file
    outside hidden folders, virtualenvs and caches."""
    try:
        listed = subprocess.run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard", "*.py"],
            cwd=root,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.split()
        return [root / p for p in listed if (root / p).is_file()]
    except (subprocess.CalledProcessError, FileNotFoundError):
        skip = {"node_modules", "__pycache__"}
        return [
            path
            for path in sorted(root.rglob("*.py"))
            if not any(
                part.startswith(".") or part in skip for part in path.relative_to(root).parts
            )
        ]


def test_without_a_git_repository_the_tree_is_walked(tmp_path: Path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "a.py").write_text("", encoding="utf-8")
    (tmp_path / ".venv" / "lib").mkdir(parents=True)
    (tmp_path / ".venv" / "lib" / "b.py").write_text("", encoding="utf-8")
    assert python_files(tmp_path) == [tmp_path / "pkg" / "a.py"]


def test_no_python_file_suppresses_the_formatter():
    assert markers(python_files(ROOT)) == []
