"""Tests for the one YAML reader every framework file goes through."""

from pathlib import Path

import pytest

from agentic.yamlio import YamlFileError, line_of, load_yaml, read_text, split_front_matter


def test_duplicate_key_rejected_with_its_line():
    with pytest.raises(YamlFileError) as error:
        load_yaml("name: a\ndeny: {shell: [kubectl]}\ndeny: {shell: []}\n")

    assert "duplicate" in str(error.value).lower()
    assert "line 3" in str(error.value)  # the second `deny:`


def test_unsafe_tag_rejected():
    with pytest.raises(YamlFileError):
        load_yaml("x: !!python/object/apply:os.system ['echo hacked']\n")


def test_values_are_plain_python():
    data, _ = load_yaml("a: 1\nb: [x, 'y']\nwhen: 2026-10-16T10:42:00Z\nflag: yes\n")

    assert data == {"a": 1, "b": ["x", "y"], "when": data["when"], "flag": "yes"}
    assert type(data["b"]) is list and type(data["b"][1]) is str
    assert data["when"].tzinfo is not None  # timezone kept
    assert data["flag"] == "yes"  # YAML 1.2: "yes" stays a string


def test_line_map_points_at_keys_and_items():
    _, lines = load_yaml("a: 1\nb:\n  c: 2\n  d: [x, y]\n", first_line=10)

    assert line_of(lines, ("a",)) == 10
    assert line_of(lines, ("b", "c")) == 12
    assert line_of(lines, ("b", "d", 1)) == 13
    assert line_of(lines, ("b", "c", "missing")) == 12  # nearest known parent


def test_syntax_error_reports_the_broken_line():
    with pytest.raises(YamlFileError) as error:
        load_yaml('a: 1\nb: 2\nc: "unclosed\nd: 4\n', first_line=2)

    # The quote opens on file line 4 (first_line=2) and runs to the end of the text:
    # the message must point at line 4, where the fix is needed.
    assert "started at line 4" in str(error.value)


def test_bom_is_ignored(tmp_path: Path):
    path = tmp_path / "f.yaml"
    path.write_bytes("﻿name: a\n".encode())

    assert load_yaml(read_text(path))[0] == {"name": "a"}


def test_non_utf8_file_reported(tmp_path: Path):
    path = tmp_path / "f.yaml"
    path.write_bytes(b"name: caf\xe9\n")  # Latin-1, not UTF-8

    with pytest.raises(YamlFileError) as error:
        read_text(path)

    assert "UTF-8" in str(error.value)


def test_front_matter_split():
    header, body = split_front_matter("---\nname: a\n---\nBody text.\n")

    assert (header, body) == ("name: a", "Body text.")


@pytest.mark.parametrize("text", ["no header\n", "---\nname: a\n"])
def test_front_matter_missing_or_unclosed(text: str):
    with pytest.raises(YamlFileError) as error:
        split_front_matter(text)

    assert "front matter" in str(error.value)
