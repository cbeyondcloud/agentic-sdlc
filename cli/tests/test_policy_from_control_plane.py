"""Org policy from the control plane (SP15 Task 10, ND-2; Review Focus 3): the tenant's
validated bundle, digest checked, cached with its version and tenant; a locally edited cache
or one of another tenant is refused."""

from __future__ import annotations

import gzip
import io
import json
import os
import tarfile
import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import keyring
import pytest
from cli_helpers import git
from cp_fakes import CP_URL, TOKEN, VERSION, FakeControlPlane

from agentic import auth
from agentic_cli import control_plane
from agentic_cli.config import agentic_home
from agentic_cli.control_plane import CONTROL_PLANE_TOKEN, record_login
from agentic_cli.errors import CliError
from agentic_cli.exit_codes import ExitCode
from agentic_cli.policy_source import resolve_policy
from agentic_cli.workspace import Workspace

CONFIG = f"org: acme\ncontrol_plane_url: {CP_URL}\n"


@pytest.fixture
def plane(policy_repo: Path, home: Path, monkeypatch) -> FakeControlPlane:
    """A signed-in user of tenant acme whose control plane serves `policy_repo`."""
    fake = FakeControlPlane(policy_root=policy_repo)
    monkeypatch.setattr(control_plane, "make_transport", lambda: fake.transport)
    monkeypatch.delenv("AGENTIC_CONTROL_PLANE_URL", raising=False)
    monkeypatch.delenv("AGENTIC_CONTROL_PLANE_TOKEN", raising=False)
    config_home = agentic_home()
    config_home.mkdir(parents=True, exist_ok=True)
    (config_home / "config.yaml").write_text(CONFIG, encoding="utf-8")
    auth.Credentials().store(CONTROL_PLANE_TOKEN, TOKEN)
    record_login(config_home, CP_URL, "acme", "dev@acme.example")
    return fake


def _resolve(config: dict | None = None):
    home = agentic_home()
    return resolve_policy(
        standalone=False,
        policy_path=None,
        home=home,
        config=config or {"org": "acme", "control_plane_url": CP_URL},
    )


def _cache() -> Path:
    return agentic_home() / "policy" / "acme"


def _init_once():
    loaded = _resolve()
    loaded.commit()
    return loaded


def test_org_init_fetches_the_active_bundle(plane):
    loaded = _resolve()
    assert loaded.policy.version == VERSION
    assert loaded.me is not None and loaded.me.tenant.slug == "acme"
    loaded.commit()
    stamp = json.loads((_cache() / "fetched.json").read_text(encoding="utf-8"))
    assert stamp["source_kind"] == "control-plane"
    assert stamp["tenant"] == "acme" and stamp["version"] == VERSION
    assert stamp["url"] == CP_URL and stamp["digest"].startswith("sha256:")
    assert (_cache() / "repo" / "policy").is_dir()
    assert all("tenant" not in str(r.url) for r in plane.requests)


def test_bundle_digest_mismatch_refused(plane):
    plane.tamper_bundle = True
    with pytest.raises(CliError) as caught:
        _resolve()
    assert caught.value.code == ExitCode.REFUSED and "digest" in str(caught.value)
    assert not _cache().exists()


def test_tree_digest_mismatch_refused(plane):
    plane.tree_digest_override = "sha256:" + "0" * 64
    with pytest.raises(CliError) as caught:
        _resolve()
    assert caught.value.code == ExitCode.REFUSED
    assert not _cache().exists()


def test_bundle_with_path_traversal_refused(plane, tmp_path, monkeypatch):
    scratch = tmp_path / "temp"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as tar:
        data = b"owned\n"
        info = tarfile.TarInfo("../escaped.txt")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    plane.bundle_override = gzip.compress(raw.getvalue(), mtime=0)
    with pytest.raises(CliError) as caught:
        _resolve()
    assert caught.value.code == ExitCode.REFUSED
    assert not list(tmp_path.rglob("escaped.txt"))
    assert list(scratch.iterdir()) == []  # the scratch folder is removed too


def test_unreachable_control_plane_uses_unexpired_cache_with_warning(plane):
    _init_once()
    plane.down = True
    loaded = _resolve()
    assert loaded.policy.version == VERSION
    assert any("cached org policy" in warning for warning in loaded.warnings)


def test_expired_cache_with_unreachable_control_plane_is_refused(plane):
    _init_once()
    stamp_file = _cache() / "fetched.json"
    stamp = json.loads(stamp_file.read_text(encoding="utf-8"))
    stamp["expires_at"] = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    stamp_file.write_text(json.dumps(stamp), encoding="utf-8")
    plane.down = True
    with pytest.raises(CliError) as caught:
        _resolve()
    assert caught.value.code == ExitCode.REFUSED


def test_cached_policy_of_another_tenant_is_refused(plane, repo):
    _init_once()
    record_login(agentic_home(), CP_URL, "globex", "dev@globex.example")  # re-login elsewhere
    plane.down = True
    with pytest.raises(CliError) as caught:
        _resolve()
    assert caught.value.code == ExitCode.REFUSED and "agentic init" in caught.value.hint
    ws = Workspace.load(repo, env={}, home=agentic_home())
    with pytest.raises(CliError) as caught:
        ws.policy()
    assert caught.value.code == ExitCode.REFUSED


def test_workspace_policy_uses_recorded_version(plane, repo):
    _init_once()
    ws = Workspace.load(repo, env={}, home=agentic_home())
    assert ws.policy().version == VERSION


def test_locally_edited_cached_policy_is_refused(plane, repo):
    _init_once()
    org = _cache() / "repo" / "policy" / "org.yaml"
    org.write_text(org.read_text(encoding="utf-8") + "\n# loosened\n", encoding="utf-8")
    ws = Workspace.load(repo, env={}, home=agentic_home())
    with pytest.raises(CliError) as caught:
        ws.policy()
    assert caught.value.code == ExitCode.REFUSED
    assert "changed locally" in str(caught.value) and caught.value.hint == "run `agentic init`"


def test_not_signed_in_is_refused_with_the_login_hint(plane):
    keyring.get_keyring().stored.clear()  # the test keyring (conftest): signed out
    with pytest.raises(CliError) as caught:
        _resolve()
    assert caught.value.code == ExitCode.REFUSED
    assert "agentic login --for control-plane" in caught.value.hint


def test_git_policy_source_still_works_without_control_plane(policy_repo, home):
    git(policy_repo, "init", "-q", "-b", "main")
    git(policy_repo, "add", "-A")
    git(policy_repo, "commit", "-qm", "policy")
    loaded = _resolve({"org": "acme", "policy_repo": policy_repo.as_posix()})
    assert loaded.policy.version.startswith("git:") and loaded.me is None


def test_stale_swap_leftovers_are_removed(plane):
    """DF-377: leftovers of an earlier, interrupted init are cleared on the next one."""
    _init_once()
    cache = _cache()
    stale = [cache / ".repo.new-99999", cache / ".repo.old-99999"]
    for folder in stale:
        folder.mkdir()
        (folder / "x").write_text("x", encoding="utf-8")
    temp = cache / ".fetched.json.tmp-99999"
    temp.write_text("{}", encoding="utf-8")
    old = time.time() - 3600
    for path in [*stale, temp]:
        os.utime(path, (old, old))
    fresh = cache / ".repo.new-11111"  # another init's, still being written
    fresh.mkdir()
    _init_once()
    assert not any(path.exists() for path in [*stale, temp])
    assert fresh.exists()
