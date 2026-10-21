"""The editor lookup: VISUAL, then EDITOR, then Notepad (Windows) or vi (M1-55)."""

import os
import sys
from pathlib import Path

import pytest

from agentic_cli.editor import edit_file, editor_command, resolve_program


@pytest.mark.skipif(os.name != "nt", reason="PATHEXT shims (.cmd) are a Windows feature")
def test_a_cmd_shim_on_path_starts_like_code_wait(tmp_path: Path, monkeypatch):  # F3
    shims = tmp_path / "bin"
    shims.mkdir()
    (shims / "fakeedit.cmd").write_bytes(b'@echo off\r\necho edited by the shim>> "%~2"\r\n')
    monkeypatch.setenv("PATH", f"{shims}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.delenv("VISUAL", raising=False)
    monkeypatch.setenv("EDITOR", "fakeedit --wait")
    target = tmp_path / "handoff.md"
    target.write_text("before\n", encoding="utf-8")
    edit_file(target)
    assert "edited by the shim" in target.read_text(encoding="utf-8")


@pytest.mark.skipif(os.name != "nt", reason="the PATHEXT lookup is Windows only")
def test_the_lookup_never_searches_the_current_folder(tmp_path: Path, monkeypatch):
    planted = tmp_path / "repo"
    planted.mkdir()
    (planted / "notepad.cmd").write_bytes(b"@echo off\r\n")
    real = tmp_path / "bin"
    real.mkdir()
    (real / "notepad.exe").write_bytes(b"")
    monkeypatch.chdir(planted)
    env = {"PATH": f".{os.pathsep}{real}", "PATHEXT": ".EXE;.CMD"}
    assert resolve_program("notepad", env).lower() == str(real / "notepad.exe").lower()
    assert resolve_program("notepad", {"PATH": ".", "PATHEXT": ".CMD"}) == "notepad"
    assert resolve_program("C:\\tools\\ed.exe", env) == "C:\\tools\\ed.exe"


def test_an_editor_given_as_a_python_script_path_still_runs(tmp_path: Path, monkeypatch):
    script = tmp_path / "ed.py"
    script.write_text(
        "import sys\nopen(sys.argv[1], 'a', encoding='utf-8').write('more\\n')\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("VISUAL", raising=False)
    monkeypatch.setenv("EDITOR", f'"{sys.executable}" "{script}"')
    target = tmp_path / "handoff.md"
    target.write_text("before\n", encoding="utf-8")
    edit_file(target)
    assert target.read_text(encoding="utf-8").endswith("more\n")


def test_visual_wins_over_editor():
    env = {"VISUAL": "code --wait", "EDITOR": "nano"}
    assert editor_command(env, windows=False) == ["code", "--wait"]


def test_editor_is_used_without_visual():
    assert editor_command({"EDITOR": "nano"}, windows=False) == ["nano"]


def test_the_fallback_is_notepad_on_windows_and_vi_elsewhere():
    assert editor_command({}, windows=True) == ["notepad"]
    assert editor_command({}, windows=False) == ["vi"]


def test_a_windows_path_with_spaces_and_backslashes_stays_whole():
    env = {"EDITOR": '"C:\\Program Files\\Notepad++\\notepad++.exe" -multiInst'}
    assert editor_command(env, windows=True) == [
        "C:\\Program Files\\Notepad++\\notepad++.exe",
        "-multiInst",
    ]


def test_a_posix_path_with_spaces_is_quoted():
    env = {"EDITOR": "'/Applications/My Editor.app/bin/edit' -w"}
    assert editor_command(env, windows=False) == ["/Applications/My Editor.app/bin/edit", "-w"]
