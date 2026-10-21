"""The file names `impact(diff)` reads from a diff (DF-91): git's quoted names, pure
renames, mode changes, `--no-prefix` diffs and `diff -u` timestamps."""

from pathlib import Path

from agentic.codeintel.crg import CodeReviewGraph, diff_files


def test_a_plain_git_diff():
    diff = "diff --git a/pkg/a.py b/pkg/a.py\n--- a/pkg/a.py\n+++ b/pkg/a.py\n@@ -1 +1 @@\n"
    assert "pkg/a.py" in diff_files(diff)


def test_a_quoted_non_ascii_name():
    diff = (
        'diff --git "a/pkg/caf\\303\\251.py" "b/pkg/caf\\303\\251.py"\n'
        '--- "a/pkg/caf\\303\\251.py"\n'
        '+++ "b/pkg/caf\\303\\251.py"\n'
    )
    assert "pkg/café.py" in diff_files(diff)


def test_a_quoted_name_with_a_space_quote_and_tab():
    diff = '+++ "b/my dir/say \\"hi\\"\\t.py"\n'
    assert 'my dir/say "hi"\t.py' in diff_files(diff)


def test_a_pure_rename_has_no_hunks():
    diff = (
        "diff --git a/old/name.py b/new/name.py\n"
        "similarity index 100%\n"
        "rename from old/name.py\n"
        "rename to new/name.py\n"
    )
    found = diff_files(diff)
    assert "old/name.py" in found and "new/name.py" in found


def test_a_mode_change_names_its_file():
    diff = "diff --git a/run.sh b/run.sh\nold mode 100644\nnew mode 100755\n"
    assert "run.sh" in diff_files(diff)


def test_a_no_prefix_diff():
    diff = "diff --git pkg/a.py pkg/a.py\n--- pkg/a.py\n+++ pkg/a.py\n"
    assert "pkg/a.py" in diff_files(diff)


def test_a_diff_u_timestamp_is_not_part_of_the_name():
    diff = (
        "--- pkg/a.py\t2026-10-20 10:00:00.000000000 +0100\n"
        "+++ pkg/a.py\t2026-10-21 11:00:00.000000000 +0100\n"
    )
    assert diff_files(diff) == ["pkg/a.py"]


def test_dev_null_is_never_a_file():
    diff = "--- /dev/null\n+++ b/new.py\n"
    assert "/dev/null" not in diff_files(diff)
    assert "new.py" in diff_files(diff)


class _Index:
    def __init__(self, tree: Path) -> None:
        self.tree = tree


def test_impact_finds_the_files_of_a_no_prefix_diff(tmp_path, monkeypatch):
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "x.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "café.py").write_text("x = 1\n", encoding="utf-8")
    intel = CodeReviewGraph(_Index(tmp_path))
    asked = []
    monkeypatch.setattr(intel, "_query", lambda request: asked.append(request) or {"files": []})
    # a folder named `a` keeps its name: the prefix is dropped only when that names a file
    intel.impact("--- a/x.py\n+++ a/x.py\n")
    intel.impact('+++ "b/pkg/caf\\303\\251.py"\n')
    assert asked[0]["files"] == ["a/x.py"]
    assert asked[1]["files"] == ["pkg/café.py"]


def test_a_malformed_escape_above_latin_1_keeps_the_other_files():  # DF-331
    """An agent-supplied diff with one bad quoted name must not lose every file."""
    diff = '+++ "b/pkg/bad\\€.py"\n+++ b/pkg/good.py\n'  # a backslash before a euro sign
    found = diff_files(diff)
    assert "pkg/good.py" in found and any(name.endswith("bad\u20ac.py") for name in found)
