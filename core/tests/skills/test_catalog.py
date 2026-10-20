"""Tests for the skills catalog format and the content digest (M1-37)."""

from pathlib import Path

import pytest
from _skill_helpers import entry, make_dir_link, make_skill, write_catalog

from agentic.skills import (
    CatalogError,
    PreloadRule,
    SkillsError,
    load_catalog,
    skill_digest,
    skill_folder,
)


def test_catalog_loads_and_defaults(library: Path):
    catalog = load_catalog(library)
    tdd = catalog.skills["superpowers/test-driven-development"]
    assert tdd.loading == "pre"
    assert tdd.audience == "agents" and tdd.pack is None
    assert catalog.skills["superpowers/systematic-debugging"].loading == "on-demand"
    assert catalog.skills["superpowers/writing-skills"].audience == "admins"
    assert "MIT" in catalog.licences and "Apache-2.0" in catalog.licences
    assert catalog.limits.max_catalog_tokens == 100
    assert skill_folder(library, "superpowers/test-driven-development") == (
        library / "superpowers" / "test-driven-development"
    )


@pytest.mark.parametrize("key", ["no-source", "Upper/case", "a/b/c", "a/b c", "../x", "a/-b"])
def test_bad_key_refused(tmp_path: Path, key: str):
    write_catalog(tmp_path, {key: entry("sha256:" + "0" * 64)})
    with pytest.raises(CatalogError, match="key"):
        load_catalog(tmp_path)


def test_excluded_skill_refused(tmp_path: Path):
    write_catalog(
        tmp_path,
        {"superpowers/using-superpowers": entry("sha256:" + "0" * 64)},
        excluded={"using-superpowers": "duplicates the orchestrator"},
    )
    with pytest.raises(CatalogError, match="excluded"):
        load_catalog(tmp_path)


def test_unknown_field_refused(tmp_path: Path):
    write_catalog(tmp_path, {"a/b": entry("sha256:" + "0" * 64, colour="blue")})
    with pytest.raises(CatalogError, match="colour"):
        load_catalog(tmp_path)


@pytest.mark.parametrize("reviewer", ["", "   "])
def test_blank_reviewer_refused(tmp_path: Path, reviewer: str):
    # A blank name would waive the human review of a skill's scripts (SP10 review F4).
    write_catalog(tmp_path, {"a/b": entry("sha256:" + "0" * 64, scripts_reviewed_by=reviewer)})
    with pytest.raises(CatalogError, match="scripts_reviewed_by"):
        load_catalog(tmp_path)


def test_bad_digest_refused(tmp_path: Path):
    write_catalog(tmp_path, {"a/b": entry("md5:abc")})
    with pytest.raises(CatalogError, match="sha256"):
        load_catalog(tmp_path)


def test_missing_catalog_is_a_catalog_error(tmp_path: Path):
    with pytest.raises(CatalogError, match="catalog.yaml"):
        load_catalog(tmp_path)


def test_digest_changes_with_content(tmp_path: Path):
    folder = make_skill(tmp_path, "a/b")
    before = skill_digest(folder)
    assert before.startswith("sha256:") and len(before) == len("sha256:") + 64
    (folder / "SKILL.md").write_text("changed", encoding="utf-8")
    assert skill_digest(folder) != before


def test_digest_covers_added_files_and_names(tmp_path: Path):
    folder = make_skill(tmp_path, "a/b")
    before = skill_digest(folder)
    (folder / "extra.md").write_text("", encoding="utf-8")
    added = skill_digest(folder)
    assert added != before
    (folder / "extra.md").rename(folder / "other.md")
    assert skill_digest(folder) not in (before, added)


def test_digest_ignores_line_endings(tmp_path: Path):
    """Review Focus 4: a Windows (CRLF) checkout hashes like a Linux (LF) one."""
    lf = make_skill(tmp_path / "lf", "a/b", body="line one\nline two")
    crlf = make_skill(tmp_path / "crlf", "a/b", body="line one\nline two")
    for path in crlf.rglob("*"):
        if path.is_file():
            path.write_bytes(path.read_bytes().replace(b"\n", b"\r\n"))
    assert skill_digest(lf) == skill_digest(crlf)


def test_digest_is_unambiguous(tmp_path: Path):
    """DF-140: one file holding 'x NUL b NUL y' and two files 'a'='x', 'b'='y' fed the
    same bytes to the v1 hash (path NUL content NUL); length prefixes keep them apart."""
    one = make_skill(tmp_path / "one", "a/b", files={"a": b"1\x00b\x002"})
    two = make_skill(tmp_path / "two", "a/b", files={"a": b"1", "b": b"2"})
    assert skill_digest(one) != skill_digest(two)


def test_digest_refuses_a_directory_link(tmp_path: Path):
    """DF-140: a junction (Windows) or symlinked folder (Linux, macOS) inside a skill is
    refused the same way on every OS, never followed on one and skipped on another."""
    folder = make_skill(tmp_path / "lib", "a/b")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "notes.md").write_text("mine", encoding="utf-8")
    make_dir_link(folder / "refs", outside)
    with pytest.raises(SkillsError, match="refs"):
        skill_digest(folder)


def test_digest_refuses_a_file_link(tmp_path: Path):
    folder = make_skill(tmp_path / "lib", "a/b")
    outside = tmp_path / "outside.md"
    outside.write_text("mine", encoding="utf-8")
    try:
        (folder / "notes.md").symlink_to(outside)
    except OSError:
        pytest.skip("this machine cannot create symlinks")
    with pytest.raises(SkillsError, match="notes.md"):
        skill_digest(folder)


def test_preload_rule_matches_tier_or_model_glob():
    rule = PreloadRule(tiers=["fast"], models=["local/*"])
    assert rule.matches("fast", "any-model")
    assert rule.matches("balanced", "local/qwen-7b")
    assert not rule.matches("balanced", "remote/big-model")
    assert not PreloadRule().matches("fast", "local/qwen-7b")
