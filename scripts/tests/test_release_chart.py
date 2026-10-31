"""The umbrella Helm chart in a release (DF-801, owner decision 2026-10-30): `release.py
build` packages `deploy/helm/agentic-platform` as `agentic-platform-<chart version>.tgz` next
to the wheels, so `release.py sign` signs it with the org's Ed25519 key in SSH signature
format (decision 105) and `ssh-keygen -Y verify` checks it against allowed_signers. The
archive is what `helm package` makes (one top folder, the chart's committed files) and is
reproducible: two builds of one commit are byte-identical. Nothing here calls the network."""

from __future__ import annotations

import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest
from release_support import IDENTITY, REPO, allowed, git, new_key

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agentic_release import chart, publish, signing, version  # noqa: E402
from test_release_signing import _ssh_keygen  # noqa: E402

CHART_DIR = "deploy/helm/agentic-platform"


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A git repo holding the chart, committed once, plus one untracked file."""
    dest = tmp_path / "repo"
    shutil.copytree(REPO / CHART_DIR, dest / CHART_DIR)
    git(dest, "init", "-q", "-b", "main")
    git(dest, "config", "user.email", "release@example.invalid")
    git(dest, "config", "user.name", "Release")
    git(dest, "config", "commit.gpgsign", "false")
    git(dest, "config", "core.autocrlf", "false")
    git(dest, "add", "-A")
    git(dest, "commit", "-q", "-m", "chart")
    (dest / CHART_DIR / "stray.yaml").write_text("not: committed\n", encoding="utf-8")
    return dest


def _members(path: Path) -> dict[str, tarfile.TarInfo]:
    with tarfile.open(path, "r:gz") as tar:
        return {m.name: m for m in tar.getmembers()}


def test_the_chart_is_packaged_as_helm_does(repo, tmp_path):
    out = tmp_path / "dist"
    out.mkdir()
    path = chart.package_chart(repo, out, epoch=1_700_000_000)
    want = version.chart_version(version.read_version(REPO))
    assert path.name == f"agentic-platform-{want}.tgz"
    members = _members(path)
    assert "agentic-platform/Chart.yaml" in members
    assert "agentic-platform/values.yaml" in members
    assert "agentic-platform/environments/prod.yaml" in members
    assert "agentic-platform/charts/tokenlean-lite/Chart.yaml" in members
    assert all(name.startswith("agentic-platform/") for name in members)
    assert "agentic-platform/stray.yaml" not in members, "only committed files ship"
    for info in members.values():
        assert info.mtime == 1_700_000_000 and info.uid == 0 and info.gid == 0
        assert info.uname == "" and info.mode == 0o644


def test_two_builds_are_byte_identical(repo, tmp_path):
    first, second = tmp_path / "a", tmp_path / "b"
    first.mkdir()
    second.mkdir()
    a = chart.package_chart(repo, first, epoch=1_700_000_000)
    b = chart.package_chart(repo, second, epoch=1_700_000_000)
    assert a.read_bytes() == b.read_bytes()
    # the gzip header's MTIME field (bytes 4-8, little-endian) is the commit's date
    assert int.from_bytes(a.read_bytes()[4:8], "little") == 1_700_000_000


def test_a_chart_version_that_disagrees_with_the_release_is_refused(repo, tmp_path):
    chart_yaml = repo / CHART_DIR / "Chart.yaml"
    text = chart_yaml.read_text(encoding="utf-8")
    chart_yaml.write_text(text.replace("\nversion: ", "\nversion: 9.9.9-x\n#", 1), "utf-8")
    git(repo, "commit", "-qam", "drift")
    out = tmp_path / "dist"
    out.mkdir()
    with pytest.raises(chart.ChartError, match="9.9.9-x"):
        chart.package_chart(repo, out, epoch=1, expected="0.1.0")


def test_the_chart_is_signed_and_verified_with_the_release(repo, tmp_path):
    out = tmp_path / "dist"
    out.mkdir()
    path = chart.package_chart(repo, out, epoch=1_700_000_000)
    key_path, public = new_key(tmp_path)
    signing.sign_dir(out, signing.load_private_key(key_path))
    assert (out / (path.name + signing.SIG_SUFFIX)).is_file()
    names = signing.verify_dir(out, signing.parse_allowed(allowed(public)), IDENTITY)
    assert path.name in names


@pytest.mark.skipif(_ssh_keygen() is None, reason="no ssh-keygen on this machine")
def test_ssh_keygen_verifies_the_chart(repo, tmp_path):
    out = tmp_path / "dist"
    out.mkdir()
    path = chart.package_chart(repo, out, epoch=1_700_000_000)
    key_path, public = new_key(tmp_path)
    signing.sign_dir(out, signing.load_private_key(key_path))
    signers = tmp_path / "allowed_signers"
    signers.write_text(allowed(public), encoding="utf-8")
    sig = out / (path.name + signing.SIG_SUFFIX)
    cmd = [_ssh_keygen(), "-Y", "verify", "-f", str(signers), "-I", IDENTITY]
    cmd += ["-n", signing.NAMESPACE, "-s", str(sig)]
    with path.open("rb") as stdin:
        done = subprocess.run(cmd, stdin=stdin, capture_output=True, text=True)
    assert done.returncode == 0, done.stderr


def test_publish_never_uploads_the_chart_to_the_package_index():
    assert not "agentic-platform-0.1.0.tgz".endswith(publish.DIST_SUFFIXES)


def test_release_build_packages_the_chart():
    text = (Path(__file__).resolve().parents[1] / "release.py").read_text(encoding="utf-8")
    assert "chart.package_chart(" in text
