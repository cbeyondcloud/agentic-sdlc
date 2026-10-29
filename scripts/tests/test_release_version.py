"""One version for the four workspace packages (M3-21): the root pyproject is the source,
every member and every internal pin follows it, and a release never goes backwards."""

import re
import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agentic_release import version as v  # noqa: E402

from agentic.policy.model import MIN_VERSION_PATTERN

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """A copy of the repo's five pyproject files, laid out as in the repo."""
    shutil.copy2(REPO / "pyproject.toml", tmp_path / "pyproject.toml")
    for member in v.MEMBERS:
        (tmp_path / member).mkdir()
        shutil.copy2(REPO / member / "pyproject.toml", tmp_path / member / "pyproject.toml")
    return tmp_path


def edit(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    assert old in text, old
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


def test_repo_versions_agree():
    assert v.check_versions(REPO) == []
    assert re.fullmatch(MIN_VERSION_PATTERN, v.read_version(REPO))


def test_check_reports_every_drift(workspace: Path):
    current = v.read_version(workspace)
    edit(workspace / "core" / "pyproject.toml", f'version = "{current}"', 'version = "9.9.9"')
    edit(workspace / "cli" / "pyproject.toml", f'"sdlc-commons=={current}"', '"sdlc-commons"')
    edit(
        workspace / "control-plane" / "pyproject.toml",
        f'"agentic-core=={current}"',
        '"agentic-core>=0.1"',
    )
    edit(workspace / "commons" / "pyproject.toml", '"sdlc-harness==', '"sdlc-harness>=')
    problems = "\n".join(v.check_versions(workspace))
    assert "core/pyproject.toml: version 9.9.9" in problems
    assert "cli/pyproject.toml: sdlc-commons" in problems
    assert "control-plane/pyproject.toml: agentic-core" in problems
    assert "commons/pyproject.toml: sdlc-harness" in problems


def test_check_reports_a_lock_left_at_the_old_version(workspace: Path):
    """The SBOM is exported from uv.lock (--frozen): a bump without `uv lock` would sign an
    SBOM naming the previous release, so the check refuses it."""
    shutil.copy2(REPO / "uv.lock", workspace / "uv.lock")
    current = v.read_version(workspace)
    assert v.check_versions(workspace) == []
    new = v.bump(current, "minor")
    v.set_version(workspace, new)
    problems = v.check_versions(workspace)
    assert f"uv.lock: agentic-core {current} (the release is {new}): run uv lock" in problems
    # the four members and the workspace root
    assert len([p for p in problems if p.startswith("uv.lock: ")]) == len(v.MEMBERS) + 1


def test_set_moves_every_version_and_pin(workspace: Path):
    changed = v.set_version(workspace, "0.2.0")
    assert len(changed) == 5
    assert v.read_version(workspace) == "0.2.0"
    assert v.check_versions(workspace) == []
    cli = (workspace / "cli" / "pyproject.toml").read_text(encoding="utf-8")
    assert '"agentic-core[eval]==0.2.0"' in cli and '"sdlc-commons==0.2.0"' in cli
    # third-party pins and the git-sourced Zyroflo pin are not release versions
    assert '"typer==0.27.3"' in cli
    commons = (workspace / "commons" / "pyproject.toml").read_text(encoding="utf-8")
    assert '"sdlc-harness==0.1.0"' in commons


def test_set_refuses_a_downgrade(workspace: Path):
    v.set_version(workspace, "0.3.0")
    for older in ("0.3.0", "0.2.9", "0.3.0rc1"):
        with pytest.raises(v.VersionError, match="not greater"):
            v.set_version(workspace, older)
    assert v.read_version(workspace) == "0.3.0"


@pytest.mark.parametrize("bad", ["1.0", "1.0.0-rc.1", "v1.0.0", "1.0.0.post1", "1.0.0.dev3", ""])
def test_only_release_versions_an_org_can_require(workspace: Path, bad: str):
    with pytest.raises(v.VersionError):
        v.set_version(workspace, bad)


@pytest.mark.parametrize(
    ("current", "part", "expected"),
    [
        ("0.1.0", "patch", "0.1.1"),
        ("0.1.7", "minor", "0.2.0"),
        ("0.9.3", "major", "1.0.0"),
        ("1.0.0rc2", "patch", "1.0.0"),
        ("1.2.0rc1", "minor", "1.2.0"),
    ],
)
def test_bump_parts(current: str, part: str, expected: str):
    assert v.bump(current, part) == expected


@pytest.mark.parametrize("good", ["0.1.0", "1.0.0", "2.10.3", "1.0.0a1", "1.0.0b2", "1.0.0rc1"])
def test_version_matches_min_cli_version_pattern(good: str):
    """Any release can be an org's min_cli_version (M2-08)."""
    assert str(v.parse(good)) == good
    assert re.fullmatch(MIN_VERSION_PATTERN, good)
