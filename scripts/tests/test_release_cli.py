"""scripts/release.py, the release command a release manager runs on a laptop and the
org's CI runs from the workflow template (M3-21)."""

import json
import sys
from pathlib import Path

import pytest
from release_support import IDENTITY, allowed, git, make_release_repo, new_key

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
import release  # noqa: E402
from agentic_release.version import read_version  # noqa: E402


@pytest.fixture(scope="module")
def repo(tmp_path_factory) -> Path:
    return make_release_repo(tmp_path_factory.mktemp("cli") / "repo")


def test_build_sign_verify_on_a_committed_tree(repo: Path, tmp_path: Path, capsys):
    version = read_version(repo)
    out = tmp_path / "dist"
    assert release.main(["--repo", str(repo), "build", "--out", str(out), "--no-git-deps"]) == 0
    names = {p.name for p in out.iterdir()}
    assert {
        f"agentic-sdlc-{version}.cdx.json",
        f"third-party-notices-{version}.md",
        f"constraints-{version}.txt",
        "release.json",
    } <= names
    assert len([n for n in names if n.endswith((".whl", ".tar.gz"))]) == 8
    manifest = json.loads((out / "release.json").read_text(encoding="utf-8"))
    assert manifest["version"] == version and manifest["dirty"] is False
    assert manifest["commit"] == git(repo, "rev-parse", "HEAD").strip()
    assert sorted(manifest["files"]) == sorted(names - {"release.json"})

    key, public = new_key(tmp_path)
    signers = tmp_path / "allowed_signers"
    signers.write_text(allowed(public), encoding="utf-8")
    assert release.main(["sign", "--dir", str(out), "--key", str(key)]) == 0
    verify = ["verify", "--dir", str(out), "--allowed-signers", str(signers)]
    assert release.main([*verify, "--identity", IDENTITY]) == 0
    assert release.main([*verify, "--identity", "someone@example.invalid"]) == 1
    assert "no allowed key" in capsys.readouterr().err


def test_the_key_can_come_from_the_environment(repo: Path, tmp_path: Path, monkeypatch):
    out = tmp_path / "dist"
    out.mkdir()
    (out / "file.txt").write_text("x\n", encoding="utf-8")
    key, _ = new_key(tmp_path)
    monkeypatch.setenv("AGENTIC_RELEASE_SIGNING_KEY", str(key))
    assert release.main(["sign", "--dir", str(out)]) == 0
    assert (out / "file.txt.sig").is_file()


def test_a_tag_that_is_not_the_version_is_refused(repo: Path, tmp_path: Path, capsys):
    args = ["--repo", str(repo), "build", "--out", str(tmp_path / "d"), "--no-git-deps"]
    assert release.main([*args, "--tag", "v9.9.9"]) == 1
    assert "v9.9.9" in capsys.readouterr().err
    assert not (tmp_path / "d").exists()


def test_version_check_set_and_auto_bump(repo: Path, capsys):
    assert release.main(["--repo", str(repo), "version", "--check"]) == 0
    assert capsys.readouterr().out.strip() == read_version(repo)
    git(repo, "tag", f"v{read_version(repo)}")
    git(repo, "commit", "-q", "--allow-empty", "-m", "feat(cli): something new")
    args = ["--repo", str(repo), "version", "--bump", "auto", "--no-lock"]
    assert release.main(args) == 0
    major, minor, _ = (int(n) for n in read_version(repo).split("."))
    assert capsys.readouterr().out.strip() == f"{major}.{minor}.0"
    assert release.main(["--repo", str(repo), "version", "--check"]) == 0
    git(repo, "checkout", "-q", "--", ".")


def test_changelog_prints_the_section(repo: Path, capsys):
    git(repo, "commit", "-q", "--allow-empty", "-m", "fix(release): a fix")
    assert release.main(["--repo", str(repo), "changelog", "--version", "9.0.0"]) == 0
    text = capsys.readouterr().out
    assert text.startswith("## 9.0.0 (") and "- **release:** a fix" in text


def test_publish_needs_a_url(tmp_path: Path, capsys, monkeypatch):
    monkeypatch.delenv("AGENTIC_RELEASE_INDEX_URL", raising=False)
    assert release.main(["publish", "--dir", str(tmp_path)]) == 1
    assert "AGENTIC_RELEASE_INDEX_URL" in capsys.readouterr().err
