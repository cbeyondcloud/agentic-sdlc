"""The editor lookup: VISUAL, then EDITOR, then Notepad (Windows) or vi (M1-55)."""

from agentic_cli.editor import editor_command


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
