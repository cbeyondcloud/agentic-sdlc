"""Publishing a release (M3-21): only verified artefacts, only to the org's internal
index, credentials only through the environment. The index is a local test server."""

import secrets
import subprocess
import sys
from pathlib import Path

import pytest
from local_index import LocalIndex
from release_support import IDENTITY, allowed, new_key

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agentic_release import publish, signing  # noqa: E402

WHEEL = "demo_pkg-1.0.0-py3-none-any.whl"
SDIST = "demo_pkg-1.0.0.tar.gz"


def demo_release(tmp_path: Path) -> tuple[Path, dict, str]:
    """A signed release folder of one tiny real package (built by uv build), its
    allowed_signers and identity."""
    project = tmp_path / "demo-pkg"
    (project / "src" / "demo_pkg").mkdir(parents=True)
    (project / "src" / "demo_pkg" / "__init__.py").write_bytes(b"VALUE = 1\n")
    (project / "pyproject.toml").write_bytes(
        b'[project]\nname = "demo-pkg"\nversion = "1.0.0"\nrequires-python = ">=3.12"\n\n'
        b'[build-system]\nrequires = ["uv_build>=0.12,<0.13"]\nbuild-backend = "uv_build"\n'
    )
    folder = tmp_path / "release"
    subprocess.run(
        ["uv", "build", str(project), "--out-dir", str(folder), "--no-create-gitignore", "-q"],
        check=True,
    )
    (folder / "agentic-sdlc-1.0.0.cdx.json").write_text("{}\n", encoding="utf-8")
    key, public = new_key(tmp_path)
    signing.sign_dir(folder, signing.load_private_key(key))
    return folder, signing.parse_allowed(allowed(public)), IDENTITY


@pytest.fixture
def index(tmp_path: Path):
    server = LocalIndex(tmp_path / "index", password=secrets.token_hex(8)).start()
    yield server
    server.stop()


def creds(index: LocalIndex) -> dict[str, str]:
    return {"UV_PUBLISH_USERNAME": index.username, "UV_PUBLISH_PASSWORD": index.password}


@pytest.mark.parametrize(
    "url",
    [
        "https://upload.pypi.org/legacy/",
        "https://test.pypi.org/legacy/",
        "https://PyPI.org/simple",
        "http://packages.example.internal/upload",  # credentials in clear text
        "ftp://packages.example.internal/",
        # spellings of a public host that resolve to it
        "https://pypi.org./legacy/",
        "https://upload.pypi.org../legacy/",
        "https://test.pypi.org./legacy/",
        "https://files.pythonhosted.org./",
        "https://pypi.org%2e/legacy/",
        "https://%70ypi.org/legacy/",
        "https://ｐｙｐｉ.org/legacy/",  # fullwidth letters, IDNA-mapped
        "https://mirror.files.pythonhosted.org/",
        "https://pypi.org:443/legacy/",
    ],
)
def test_public_or_insecure_index_refused(url: str):
    with pytest.raises(publish.PublishError):
        publish.check_index_url(url)


def test_internal_and_loopback_indexes_accepted():
    publish.check_index_url("https://pypi.example-corp.internal/legacy/")
    publish.check_index_url("http://127.0.0.1:8080/")
    publish.check_index_url("http://localhost:3141/root/dev/")


def test_credentials_in_the_url_are_refused_and_never_echoed():
    """Credentials come only from the environment; a URL carrying them is refused, and the
    refusal (which a CI log keeps) does not repeat them."""
    secret = "pa" + "ss-in-url"
    for url in (
        f"https://user:{secret}@pypi.example-corp.internal/legacy/",
        f"https://user:{secret}@pypi.org/legacy/",
    ):
        with pytest.raises(publish.PublishError) as refused:
            publish.check_index_url(url)
        assert secret not in str(refused.value)
        assert "UV_PUBLISH_" in str(refused.value)


def test_uploads_to_a_local_index(index: LocalIndex, tmp_path: Path):
    folder, signers, identity = demo_release(tmp_path)
    sent = publish.publish(
        folder,
        index.url,
        allowed=signers,
        identity=identity,
        check_url=index.simple_url,
        env=creds(index),
    )
    assert sorted(sent) == [WHEEL, SDIST] == sorted(index.uploads)
    stored = index.files()
    for name in (WHEEL, SDIST):
        assert stored[name] == (folder / name).read_bytes()
    # publishing again is a no-op: the check URL shows the files are there
    publish.publish(
        folder,
        index.url,
        allowed=signers,
        identity=identity,
        check_url=index.simple_url,
        env=creds(index),
    )
    assert sorted(index.uploads) == [WHEEL, SDIST] and index.refused == []


def test_publish_refuses_unverified_artefacts(index: LocalIndex, tmp_path: Path):
    folder, signers, identity = demo_release(tmp_path)
    (folder / WHEEL).write_bytes((folder / WHEEL).read_bytes() + b"tampered")
    with pytest.raises(signing.SignatureError):
        publish.publish(folder, index.url, allowed=signers, identity=identity, env=creds(index))
    assert index.uploads == [] and index.files() == {}


def test_wrong_credentials_fail_loudly(index: LocalIndex, tmp_path: Path):
    folder, signers, identity = demo_release(tmp_path)
    wrong = {"UV_PUBLISH_USERNAME": "publisher", "UV_PUBLISH_PASSWORD": "not-it"}
    with pytest.raises(publish.PublishError) as caught:
        publish.publish(folder, index.url, allowed=signers, identity=identity, env=wrong)
    assert "not-it" not in str(caught.value)
    assert index.uploads == []


def test_credentials_only_in_the_environment(tmp_path: Path):
    folder, signers, identity = demo_release(tmp_path)
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"], seen["env"] = cmd, kwargs["env"]
        return subprocess.CompletedProcess(cmd, 0, "", "")

    password = secrets.token_hex(8)
    env = {"UV_PUBLISH_USERNAME": "ci", "UV_PUBLISH_PASSWORD": password}
    publish.publish(
        folder,
        "https://pypi.example-corp.internal/legacy/",
        allowed=signers,
        identity=identity,
        env=env,
        run=fake_run,
    )
    assert password not in " ".join(seen["cmd"])
    assert seen["env"]["UV_PUBLISH_PASSWORD"] == password
    assert ["--trusted-publishing", "never"] == seen["cmd"][4:6]
    uploaded = [Path(arg).name for arg in seen["cmd"] if arg.endswith((".whl", ".tar.gz"))]
    assert sorted(uploaded) == [WHEEL, SDIST]  # never the SBOM, sums or signatures


def test_uv_is_required(monkeypatch, tmp_path: Path):
    folder, signers, identity = demo_release(tmp_path)
    monkeypatch.setattr(publish.shutil, "which", lambda name: None)
    with pytest.raises(publish.PublishError, match="uv"):
        publish.publish(folder, "http://127.0.0.1:1/", allowed=signers, identity=identity)
