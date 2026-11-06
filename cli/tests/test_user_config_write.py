"""Writing settings into the user config (DF-952): `agentic login --invite` sets
control_plane_url and gateway_url itself. Every other line - comments, order, quoting,
line endings - stays as the person wrote it; a value it replaces is kept as a comment; the
file is replaced in one step; and a file it cannot edit safely is left alone."""

import os
import sys
import tempfile
from pathlib import Path

import pytest

from agentic.yamlio import load_yaml
from agentic_cli.config import CONFIG_FILE, set_user_config

URL = "https://cp.acme.example"
GATEWAY = "https://gateway.acme.example"


def _symlinks_work() -> bool:
    """Whether this user may make symbolic links (on Windows: Developer Mode or admin)."""
    with tempfile.TemporaryDirectory() as folder:
        try:
            os.symlink(os.path.join(folder, "target"), os.path.join(folder, "link"))
        except (OSError, NotImplementedError):
            return False
    return True


#: Symbolic-link tests run wherever links can be made, Windows included (DF-965).
SYMLINKS = pytest.mark.skipif(
    not _symlinks_work(), reason="symbolic links need Developer Mode (or admin rights) on Windows"
)


def config(home: Path) -> Path:
    return home / CONFIG_FILE


def write(home: Path, text: str, newline: str = "\n") -> Path:
    home.mkdir(parents=True, exist_ok=True)
    path = config(home)
    path.write_bytes(text.replace("\n", newline).encode("utf-8"))
    return path


def test_a_missing_file_is_created_with_the_settings(tmp_path):
    change = set_user_config(tmp_path / "h", {"control_plane_url": URL, "gateway_url": GATEWAY})
    assert change.written and change.changed == {"control_plane_url": None, "gateway_url": None}
    data, _ = load_yaml(config(tmp_path / "h").read_text(encoding="utf-8"))
    assert data == {"control_plane_url": URL, "gateway_url": GATEWAY}


def test_settings_are_appended_and_every_other_line_is_kept(tmp_path):
    before = "# my settings\norg: acme   # the org\njira:\n  url: https://j.example\n"
    write(tmp_path, before)
    change = set_user_config(tmp_path, {"control_plane_url": URL})
    text = config(tmp_path).read_text(encoding="utf-8")
    assert text.startswith(before) and text.endswith(f"control_plane_url: {URL}\n")
    assert change.changed == {"control_plane_url": None}


def test_a_file_without_a_final_newline_gets_one_before_the_new_line(tmp_path):
    write(tmp_path, "org: acme")
    set_user_config(tmp_path, {"gateway_url": GATEWAY})
    assert config(tmp_path).read_text(encoding="utf-8") == f"org: acme\ngateway_url: {GATEWAY}\n"


def test_a_different_value_is_replaced_and_the_old_line_kept_as_a_comment(tmp_path):
    write(tmp_path, "gateway_url: https://old.example  # set by hand\norg: acme\n")
    change = set_user_config(tmp_path, {"gateway_url": GATEWAY})
    lines = config(tmp_path).read_text(encoding="utf-8").splitlines()
    assert lines[0].startswith("# ") and "gateway_url: https://old.example" in lines[0]
    assert lines[1:] == [f"gateway_url: {GATEWAY}", "org: acme"]
    assert change.changed == {"gateway_url": "https://old.example"}


def test_an_equal_value_changes_nothing(tmp_path):
    path = write(tmp_path, f"gateway_url: {GATEWAY}\n")
    stamp = path.stat().st_mtime_ns
    change = set_user_config(tmp_path, {"gateway_url": GATEWAY})
    assert change.changed == {} and not change.written
    assert path.stat().st_mtime_ns == stamp


def test_windows_line_endings_and_a_byte_order_mark_are_kept(tmp_path):
    path = config(tmp_path)
    tmp_path.mkdir(exist_ok=True)
    path.write_bytes(b"\xef\xbb\xbforg: acme\r\n")
    set_user_config(tmp_path, {"gateway_url": GATEWAY})
    assert path.read_bytes() == f"﻿org: acme\r\ngateway_url: {GATEWAY}\r\n".encode()


@pytest.mark.parametrize(
    "text",
    [
        "org: [unclosed\n",  # not YAML
        "- a list\n",  # not a mapping
        "gateway_url: |\n  https://old.example\n",  # a block value over several lines
        "gateway_url:\n  nested: 1\n",  # not a plain value
    ],
)
def test_a_file_that_cannot_be_edited_safely_is_left_alone(tmp_path, text):
    path = write(tmp_path, text)
    change = set_user_config(tmp_path, {"gateway_url": GATEWAY})
    assert not change.written and change.reason
    assert path.read_text(encoding="utf-8") == text


def test_a_value_yaml_would_misread_is_quoted(tmp_path):
    set_user_config(tmp_path, {"gateway_url": "https://g.example/#x: y"})
    data, _ = load_yaml(config(tmp_path).read_text(encoding="utf-8"))
    assert data == {"gateway_url": "https://g.example/#x: y"}


def test_nothing_is_left_behind_and_no_partial_file_on_failure(tmp_path, monkeypatch):
    path = write(tmp_path, "org: acme\n")

    def broken(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", broken)
    change = set_user_config(tmp_path, {"gateway_url": GATEWAY})
    assert not change.written and "disk full" in change.reason
    assert path.read_text(encoding="utf-8") == "org: acme\n"
    assert sorted(p.name for p in tmp_path.iterdir()) == [CONFIG_FILE]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
def test_the_file_keeps_its_mode(tmp_path):
    path = write(tmp_path, "org: acme\n")
    path.chmod(0o600)
    set_user_config(tmp_path, {"gateway_url": GATEWAY})
    assert path.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("text", ["org: acme\n...\n", "{org: acme}\n"])
def test_a_file_an_appended_line_would_break_is_left_alone(tmp_path, text):  # review F1
    path = write(tmp_path, text)
    change = set_user_config(tmp_path, {"gateway_url": GATEWAY})
    assert not change.written and change.reason
    assert path.read_text(encoding="utf-8") == text


@SYMLINKS
def test_a_symbolic_link_is_left_alone(tmp_path):  # review F5: a dotfile manager's link
    real = tmp_path / "dotfiles" / "config.yaml"
    real.parent.mkdir()
    real.write_text("org: acme\n", encoding="utf-8")
    tmp_path.joinpath("h").mkdir()
    config(tmp_path / "h").symlink_to(real)
    change = set_user_config(tmp_path / "h", {"gateway_url": GATEWAY})
    assert not change.written and "link" in change.reason
    assert config(tmp_path / "h").is_symlink() and real.read_text(encoding="utf-8") == "org: acme\n"


def test_a_replaced_empty_value_is_a_change_not_an_addition(tmp_path):  # DF-964
    write(tmp_path, "gateway_url:\norg: acme\n")
    change = set_user_config(tmp_path, {"gateway_url": GATEWAY, "control_plane_url": URL})
    assert change.written and change.changed == {"gateway_url": None, "control_plane_url": None}
    assert change.added == ("control_plane_url",)


def test_mixed_line_endings_are_kept_per_line(tmp_path):  # DF-964: never all CRLF
    path = config(tmp_path)
    tmp_path.mkdir(exist_ok=True)
    path.write_bytes(b"org: acme\r\ngateway_url: https://old.example\nteam: payments\r\n")
    set_user_config(tmp_path, {"gateway_url": GATEWAY, "control_plane_url": URL})
    assert path.read_bytes() == (
        b"org: acme\r\n"
        b"# gateway_url: https://old.example  (replaced by agentic)\n"
        + f"gateway_url: {GATEWAY}\n".encode()
        + b"team: payments\r\n"
        + f"control_plane_url: {URL}\r\n".encode()  # new lines: the file's majority ending
    )


def test_a_file_mostly_in_lf_gets_lf_lines_even_after_a_crlf_line(tmp_path):  # DF-964
    path = config(tmp_path)
    tmp_path.mkdir(exist_ok=True)
    path.write_bytes(b"org: acme\r\nteam: payments\nrole: dev\n")
    set_user_config(tmp_path, {"gateway_url": GATEWAY})
    assert path.read_bytes().endswith(f"role: dev\ngateway_url: {GATEWAY}\n".encode())


@pytest.mark.parametrize("separator", [" ", " ", "\x85"])
def test_only_a_newline_ends_a_line(tmp_path, separator):  # DF-964: never splitlines()
    before = f'note: "a{separator}b"\ngateway_url: https://old.example\norg: acme\n'
    write(tmp_path, before)
    change = set_user_config(tmp_path, {"gateway_url": GATEWAY})
    assert change.written, change.reason
    assert config(tmp_path).read_text(encoding="utf-8") == (
        f'note: "a{separator}b"\n'
        "# gateway_url: https://old.example  (replaced by agentic)\n"
        f"gateway_url: {GATEWAY}\norg: acme\n"
    )


def test_a_crlf_file_without_a_final_newline_keeps_crlf_when_its_last_row_is_replaced(
    tmp_path,
):  # DF-985: the replaced row had no ending of its own; its comment takes the file's
    path = write(tmp_path, "org: acme\ngateway_url: https://old.example", newline="\r\n")
    change = set_user_config(tmp_path, {"gateway_url": GATEWAY})
    assert change.written
    raw = path.read_bytes().decode("utf-8")
    assert raw.count("\n") == raw.count("\r\n") == 3, repr(raw)
    assert raw.endswith(f"gateway_url: {GATEWAY}\r\n")


@pytest.mark.parametrize("key", ["gateway_url", "control_plane_url"])  # replaced, or added
def test_a_file_with_lone_cr_endings_is_refused_naming_its_line_endings(tmp_path, key):
    path = write(tmp_path, "org: acme\ngateway_url: https://old.example\n", newline="\r")
    before = path.read_bytes()
    change = set_user_config(tmp_path, {key: GATEWAY})  # DF-985: not 'not a one-line value'
    assert not change.written
    assert change.reason and "line endings are not supported" in change.reason
    assert "one-line" not in change.reason
    assert path.read_bytes() == before
