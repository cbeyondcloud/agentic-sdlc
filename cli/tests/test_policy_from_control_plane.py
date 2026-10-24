"""Org policy from the control plane (SP15 Task 10, ND-2; Review Focus 3): the tenant's
validated bundle, digest checked, cached with its version and tenant; a locally edited cache
or one of another tenant is refused."""

from __future__ import annotations

import gzip
import hashlib
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
from agentic_cli.policy_source import follow_active_policy, resolve_policy
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


@pytest.mark.parametrize("name", ["docs/a:b.txt", "docs/notes.", "docs/README.md"])
def test_bundle_with_an_unportable_name_is_refused_naming_the_file(plane, name):
    """Review F4: a name some laptop cannot extract as it is gives a CliError naming the
    file - never a raw traceback, nor the misleading digest-mismatch message."""
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as tar:
        for entry in ("policy/org.yaml", "docs/Readme.md", name):
            info = tarfile.TarInfo(entry)
            info.size = 2
            tar.addfile(info, io.BytesIO(b"{}"))
    plane.bundle_override = gzip.compress(raw.getvalue(), mtime=0)
    with pytest.raises(CliError) as caught:
        _resolve()
    assert caught.value.code == ExitCode.REFUSED
    assert name in str(caught.value)


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


PIPELINE = "name: deliver\nversion: 1.0.0\nsteps:\n  - gate: review\n    approver_role: developer\n"


def test_a_locally_edited_pipeline_is_refused_with_the_approved_hash(plane, policy_repo, repo):
    """D33 (M2-21): the cache records the approved pipeline hashes, so the refusal names
    the edited pipeline and the version that was approved - even with no network."""
    (policy_repo / "pipelines").mkdir()
    (policy_repo / "pipelines" / "deliver.yaml").write_bytes(PIPELINE.encode())
    approved = "sha256:" + hashlib.sha256(PIPELINE.encode()).hexdigest()
    _init_once()
    stamp = json.loads((_cache() / "fetched.json").read_text(encoding="utf-8"))
    assert stamp["pipelines"] == {"pipelines/deliver.yaml": approved}
    cached = _cache() / "repo" / "pipelines" / "deliver.yaml"
    cached.write_bytes(PIPELINE.replace("developer", "anyone").encode())
    plane.down = True
    ws = Workspace.load(repo, env={}, home=agentic_home())
    with pytest.raises(CliError) as caught:
        ws.policy()
    message = str(caught.value)
    assert caught.value.code == ExitCode.REFUSED
    assert "pipelines/deliver.yaml" in message and approved in message


def test_edited_overlay_named_in_the_cache_refusal(plane, policy_repo, repo):
    """M2-23 (D33): an overlay is approved like a pipeline, so a locally edited one is
    named with the hash of its approved version."""
    rel = "teams/payments/pipelines/overlays/app.yaml"
    overlay = policy_repo / rel
    overlay.parent.mkdir(parents=True)
    text = b"pipelines:\n  deliver:\n    max_minutes: 30\n"
    overlay.write_bytes(text)
    approved = "sha256:" + hashlib.sha256(text).hexdigest()
    _init_once()
    stamp = json.loads((_cache() / "fetched.json").read_text(encoding="utf-8"))
    assert stamp["overlays"] == {rel: approved}
    (_cache() / "repo" / rel).write_bytes(text.replace(b"30", b"300"))
    plane.down = True
    ws = Workspace.load(repo, env={}, home=agentic_home())
    with pytest.raises(CliError) as caught:
        ws.policy()
    message = str(caught.value)
    assert caught.value.code == ExitCode.REFUSED
    assert f"{rel} (approved {approved}" in message and "overlays differ" in message


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


# ── an org run follows the tenant's active policy (M2-53) ───────────────────────────────

NEWER = "git:" + "cd" * 20


def _follow():
    return follow_active_policy(
        agentic_home(), {"org": "acme", "control_plane_url": CP_URL}, env={}
    )


def _admin_changes_the_policy(plane, policy_repo: Path) -> None:
    """An admin's commit, synced: a new active version with a changed tier model."""
    tiers = policy_repo / "policy" / "tiers.yaml"
    tiers.write_bytes(tiers.read_bytes().replace(b"claude-sonnet", b"claude-sonnet-next"))
    plane.version = NEWER


def test_a_run_follows_a_newer_active_policy(plane, policy_repo, repo):
    _init_once()
    _admin_changes_the_policy(plane, policy_repo)
    assert _follow() == (VERSION, NEWER)
    stamp = json.loads((_cache() / "fetched.json").read_text(encoding="utf-8"))
    assert stamp["version"] == NEWER and stamp["source_kind"] == "control-plane"
    ws = Workspace.load(repo, env={}, home=agentic_home())
    assert ws.policy().version == NEWER  # the cache verifies: it is the served tree
    assert ws.policy().tiers["balanced"].model == "anthropic/claude-sonnet-next"


def test_a_current_cache_is_not_fetched_again(plane):
    _init_once()
    plane.requests.clear()
    assert _follow() is None
    assert [r.url.path for r in plane.requests] == ["/v1/policy/active"]


def test_an_unreachable_control_plane_leaves_the_cache(plane, policy_repo):
    """The org checks refuse a run that cannot reach the control plane; following the
    policy never swaps or removes the cache on its own."""
    _init_once()
    before = (_cache() / "fetched.json").read_bytes()
    _admin_changes_the_policy(plane, policy_repo)
    plane.down = True
    assert _follow() is None
    assert (_cache() / "fetched.json").read_bytes() == before


def test_a_tampered_newer_bundle_is_refused_and_the_cache_kept(plane, policy_repo, repo):
    _init_once()
    _admin_changes_the_policy(plane, policy_repo)
    plane.tamper_bundle = True
    with pytest.raises(CliError) as caught:
        _follow()
    assert caught.value.code == ExitCode.REFUSED and "digest" in str(caught.value)
    ws = Workspace.load(repo, env={}, home=agentic_home())
    assert ws.policy().version == VERSION  # the old cache, intact


def test_no_control_plane_cache_means_nothing_to_follow(plane):
    """Before `agentic init` there is nothing to refresh: the workspace says to run init."""
    assert _follow() is None
    assert plane.requests == []


def test_standalone_or_git_policy_has_nothing_to_follow(plane):
    home = agentic_home()
    assert follow_active_policy(home, {}, env={}) is None
    assert follow_active_policy(home, {"org": "acme", "policy_repo": "x"}, env={}) is None
    assert plane.requests == []
