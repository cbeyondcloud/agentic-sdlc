"""The release changelog (M3-21): one section per release, from the conventional-commit
subjects since the previous `v*` tag."""

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agentic_release import changelog as cl  # noqa: E402


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    git(tmp_path, "init", "-q", "-b", "main")
    git(tmp_path, "config", "user.email", "dev@example.invalid")
    git(tmp_path, "config", "user.name", "Dev")
    git(tmp_path, "config", "commit.gpgsign", "false")
    return tmp_path


def commit(repo: Path, message: str) -> None:
    git(repo, "commit", "-q", "--allow-empty", "-m", message)


@pytest.mark.parametrize(
    ("subject", "body", "expected"),
    [
        ("feat(cli): add --version", "", ("feat", "cli", "add --version", False)),
        ("fix: a crash", "", ("fix", None, "a crash", False)),
        ("feat(core)!: drop v0 handoffs", "", ("feat", "core", "drop v0 handoffs", True)),
        ("refactor: x", "BREAKING CHANGE: the y flag is gone", ("refactor", None, "x", True)),
        ("Merge branch 'x'", "", ("other", None, "Merge branch 'x'", False)),
        ("docs: M3-21 plan", "", ("docs", None, "M3-21 plan", False)),
    ],
)
def test_parse_commit(subject, body, expected):
    found = cl.parse_commit("abcdef1234", subject, body)
    assert (found.type, found.scope, found.subject, found.breaking) == expected


def test_render_groups_by_kind_breaking_first():
    commits = [
        cl.parse_commit("1111111aaaa", "fix(cli): exit 64 on a bad flag", ""),
        cl.parse_commit("2222222bbbb", "feat: release script", ""),
        cl.parse_commit("3333333cccc", "feat(core)!: one version source", ""),
        cl.parse_commit("4444444dddd", "docs: runbook", ""),
        cl.parse_commit("5555555eeee", "chore: tidy", ""),
        cl.parse_commit("6666666ffff", "perf(engine): faster ledger", ""),
    ]
    text = cl.render("0.2.0", "2026-10-25", commits)
    assert text.startswith("## 0.2.0 (2026-10-25)\n")
    headings = [line for line in text.splitlines() if line.startswith("### ")]
    assert headings == [
        "### Breaking changes",
        "### Added",
        "### Fixed",
        "### Changed",
        "### Documentation",
        "### Other",
    ]
    assert "- **core:** one version source (3333333)" in text
    assert "- release script (2222222)" in text
    assert "- **engine:** faster ledger (6666666)" in text


def test_commits_since_the_last_tag_without_merges(repo: Path):
    commit(repo, "feat: first")
    git(repo, "tag", "v0.1.0")
    commit(repo, "fix: second")
    git(repo, "switch", "-q", "-c", "side")
    commit(repo, "feat(cli): third")
    git(repo, "switch", "-q", "main")
    git(repo, "merge", "-q", "--no-ff", "-m", "Merge branch 'side'", "side")
    assert cl.last_tag(repo) == "v0.1.0"
    subjects = [c.subject for c in cl.commits_since(repo, cl.last_tag(repo))]
    assert sorted(subjects) == ["second", "third"]
    assert [c.subject for c in cl.commits_since(repo, None)][-1] == "first"


def test_no_tag_means_the_whole_history(repo: Path):
    commit(repo, "feat: only")
    assert cl.last_tag(repo) is None
    assert [c.subject for c in cl.commits_since(repo, None)] == ["only"]


@pytest.mark.parametrize(
    ("subjects", "expected"),
    [
        (["fix: a", "docs: b"], "patch"),
        (["fix: a", "feat: b"], "minor"),
        (["feat!: a"], "major"),
        ([], "patch"),
    ],
)
def test_suggest_bump(subjects, expected):
    commits = [cl.parse_commit("0" * 10, s, "") for s in subjects]
    assert cl.suggest_bump(commits) == expected


def test_prepend_keeps_the_older_sections(tmp_path: Path):
    path = tmp_path / "CHANGELOG.md"
    cl.prepend(path, "## 0.1.0 (2026-10-01)\n\n### Added\n\n- first\n")
    cl.prepend(path, "## 0.2.0 (2026-10-25)\n\n### Fixed\n\n- second\n")
    text = path.read_text(encoding="utf-8")
    assert text.startswith("# Changelog\n")
    assert text.index("## 0.2.0") < text.index("## 0.1.0")
    assert text.count("# Changelog") == 1
    assert b"\r\n" not in path.read_bytes()
