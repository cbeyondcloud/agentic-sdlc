"""The release end to end (M3-21 acceptance: "release workflow produces signed wheel +
SBOM; install from index works"): build, sign, publish to a local index, then install the
published CLI into a clean virtualenv and run `agentic --version`, `init --standalone`
and `run --dry-run` with no checkout and no AGENTIC_FRAMEWORK_ROOT.

Third-party wheels come from uv's cache through uv.lock (offline): the test index holds
only what the release publishes, as the org's index would before it proxies PyPI."""

import secrets
import sys
from pathlib import Path

import pytest
from local_index import LocalIndex
from release_support import IDENTITY, allowed, make_release_repo, new_key

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agentic_release import build, publish, signing, smoke  # noqa: E402
from agentic_release.version import read_version  # noqa: E402


@pytest.fixture
def index(tmp_path: Path):
    server = LocalIndex(tmp_path / "index", password=secrets.token_hex(8)).start()
    yield server
    server.stop()


def test_published_release_installs_and_dry_runs(index: LocalIndex, tmp_path: Path, monkeypatch):
    monkeypatch.setenv("AGENTIC_FRAMEWORK_ROOT", str(tmp_path / "must-not-be-used"))
    repo = make_release_repo(tmp_path / "repo")
    version = read_version(repo)
    dist = tmp_path / "dist"
    build.build_packages(repo, dist)
    key, public = new_key(tmp_path)
    signing.sign_dir(dist, signing.load_private_key(key))
    signers = signing.parse_allowed(allowed(public))
    creds = {"UV_PUBLISH_USERNAME": index.username, "UV_PUBLISH_PASSWORD": index.password}
    publish.publish(dist, index.url, allowed=signers, identity=IDENTITY, env=creds)

    result = smoke.smoke(index.simple_url, version, tmp_path / "smoke", deps_from_lock=repo)

    assert result["version"] == version
    assert result["installed_from_wheel"] is True
    plan = result["dry_run"]
    assert plan["pipeline"] == "feature-delivery"
    assert [step["id"] for step in plan["steps"]][:1] == ["requirements"]
    assert result["standalone_policy"].endswith(str(Path("policy") / "standalone"))


def test_smoke_refuses_a_version_mismatch(tmp_path: Path, monkeypatch):
    """The installed CLI must report exactly the version that was released."""

    def fake(cmd, **kwargs):
        import subprocess

        out = "agentic 9.9.9\n" if "--version" in cmd else ""
        return subprocess.CompletedProcess(cmd, 0, out, "")

    monkeypatch.setattr(smoke, "_run", fake)
    with pytest.raises(smoke.SmokeError, match="9.9.9"):
        smoke.check_version(tmp_path / "agentic", "1.0.0")
