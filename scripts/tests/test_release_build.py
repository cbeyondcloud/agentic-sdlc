"""Release builds (M3-21): staged from the committed files, byte-identical when rebuilt,
each wheel with the licence and notices, the CLI wheel with the standalone template and
default library (DF-138, DF-188, DF-341), and internal packages pinned exactly."""

import email
import sys
import zipfile
from pathlib import Path

import pytest
from release_support import git, make_release_repo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agentic_release import build  # noqa: E402
from agentic_release.version import read_version  # noqa: E402


@pytest.fixture(scope="module")
def repo(tmp_path_factory) -> Path:
    return make_release_repo(tmp_path_factory.mktemp("release") / "repo")


@pytest.fixture(scope="module")
def built(repo: Path, tmp_path_factory) -> list[Path]:
    return build.build_packages(repo, tmp_path_factory.mktemp("dist"))


def wheel(files: list[Path], name: str) -> zipfile.ZipFile:
    found = [f for f in files if f.name.startswith(f"{name}-") and f.suffix == ".whl"]
    assert len(found) == 1, (name, files)
    return zipfile.ZipFile(found[0])


def metadata(archive: zipfile.ZipFile) -> email.message.Message:
    path = next(n for n in archive.namelist() if n.endswith(".dist-info/METADATA"))
    return email.message_from_bytes(archive.read(path))


def test_every_package_gets_a_wheel_and_an_sdist(built: list[Path], repo: Path):
    version = read_version(repo)
    names = sorted(f.name for f in built)
    for dist in ("agentic_cli", "agentic_control_plane", "agentic_core", "sdlc_commons"):
        assert f"{dist}-{version}-py3-none-any.whl" in names
        assert f"{dist}-{version}.tar.gz" in names
    assert len(names) == 8
    assert not any(p.name == ".gitignore" for p in built[0].parent.iterdir())


def test_two_builds_are_byte_identical(repo: Path, built: list[Path], tmp_path: Path):
    again = build.build_packages(repo, tmp_path / "again")
    assert [f.name for f in again] == [f.name for f in built]
    for first, second in zip(built, again, strict=True):
        assert first.read_bytes() == second.read_bytes(), first.name


def test_untracked_files_are_not_shipped(repo: Path, tmp_path: Path):
    stray = repo / "cli" / "src" / "agentic_cli" / "stray_notes.py"
    stray.write_text("SECRET_SCRATCH = 1\n", encoding="utf-8")
    try:
        files = build.build_packages(repo, tmp_path / "out")
    finally:
        stray.unlink()
    assert not [n for n in wheel(files, "agentic_cli").namelist() if "stray_notes" in n]


def test_cli_wheel_carries_the_framework_bundle(built: list[Path]):
    names = set(wheel(built, "agentic_cli").namelist())
    bundle = "agentic_cli/_framework/"
    for needed in (
        "templates/standalone/policy/tiers.yaml",
        "templates/standalone/skills/preload.yaml",
        "templates/standalone/README.md",
        "library/skills/catalog.yaml",
        "library/pipelines/feature-delivery.yaml",
        "library/agents/developer.md",
    ):
        assert bundle + needed in names, needed
    assert not [n for n in names if n.startswith(bundle + "library/eval")]
    for other in ("agentic_core", "sdlc_commons", "agentic_control_plane"):
        assert not [n for n in wheel(built, other).namelist() if "_framework" in n]


def test_every_wheel_carries_the_licence_and_notices(built: list[Path]):
    for name in ("agentic_cli", "agentic_control_plane", "agentic_core", "sdlc_commons"):
        archive = wheel(built, name)
        licences = [n for n in archive.namelist() if ".dist-info/licenses/" in n and n[-1] != "/"]
        assert sorted(Path(n).name for n in licences) == ["LICENSE", "THIRD_PARTY_NOTICES.md"]
        meta = metadata(archive)
        assert meta["License-Expression"] == "MIT"


def test_wheel_metadata_pins_internal_packages(built: list[Path], repo: Path):
    version = read_version(repo)
    cli = metadata(wheel(built, "agentic_cli")).get_all("Requires-Dist")
    assert f"agentic-core[eval]=={version}" in cli and f"sdlc-commons=={version}" in cli
    commons = metadata(wheel(built, "sdlc_commons")).get_all("Requires-Dist")
    assert "sdlc-harness==0.1.0" in commons


def test_dirty_tree_refused_unless_allowed(repo: Path, tmp_path: Path):
    tracked = repo / "core" / "src" / "agentic" / "__init__.py"
    original = tracked.read_bytes()
    tracked.write_bytes(original + b"# local edit\n")
    try:
        with pytest.raises(build.BuildError, match="uncommitted"):
            build.build_packages(repo, tmp_path / "refused")
        assert not (tmp_path / "refused").exists()
        assert build.build_packages(repo, tmp_path / "allowed", allow_dirty=True)
    finally:
        tracked.write_bytes(original)
    assert git(repo, "status", "--porcelain") == ""


def test_version_drift_refused(repo: Path, tmp_path: Path):
    path = repo / "core" / "pyproject.toml"
    original = path.read_bytes()
    path.write_bytes(original.replace(b'version = "', b'version = "9', 1))
    git(repo, "commit", "-qam", "drift")
    try:
        with pytest.raises(build.BuildError, match="core/pyproject.toml: version 9"):
            build.build_packages(repo, tmp_path / "refused")
    finally:
        git(repo, "revert", "--no-edit", "HEAD")
