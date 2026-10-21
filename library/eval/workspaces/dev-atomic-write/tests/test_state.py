import json
import os

import state


def windows_rename(src, dst):
    """os.rename as it behaves on Windows: it never replaces an existing file."""
    if os.path.exists(dst):
        raise FileExistsError(dst)
    os.replace(src, dst)


def test_first_save_writes_the_file(tmp_path, monkeypatch):
    monkeypatch.setattr(os, "rename", windows_rename)
    target = tmp_path / "state.json"
    state.save_state(target, {"a": 1})
    assert json.loads(target.read_text(encoding="utf-8")) == {"a": 1}


def test_second_save_replaces_the_file_on_windows(tmp_path, monkeypatch):
    monkeypatch.setattr(os, "rename", windows_rename)
    target = tmp_path / "state.json"
    state.save_state(target, {"a": 1})
    state.save_state(target, {"a": 2})
    assert json.loads(target.read_text(encoding="utf-8")) == {"a": 2}
    assert not (tmp_path / "state.json.tmp").exists()
